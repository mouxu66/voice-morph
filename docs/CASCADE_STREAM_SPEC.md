# 级联变声（录音 → ASR → TTS）流式实施方案

> 交接说明：本文档是自足的实施规格，面向接手的实施者。所有路径、接口、参数均已实测确认。
> 背景与已完成的前置工作见 `docs/TTS_CUDA_GRAPH_PLAN.md`。

## 一、目标与非目标

### 目标

用户说一句话，系统**持续地**用目标音色（美团鼠鼠）重新合成并输出到虚拟声卡，
供微信/QQ 等把「系统录音设备」指向 CABLE Output 的应用直接使用。

核心价值：**走文字中转，源说话人的口音/发音习惯完全不进入输出**。
这是 RVC 直接转换做不到的（RVC 会保留源发音特征）。

### 非目标

- 不替代 RVC 实时链路。RVC（延迟 ~0.3s）仍在，级联是**多出来的一条路**
- 不追求「边说边出声」的同声传译级体验。级联有固有 ~1.5~2s 滞后
- 本期不做多路并发

## 二、可复用组件（均已实测可用）

| 能力 | 位置 | 接口 |
|---|---|---|
| ASR | TTS worker `127.0.0.1:8001` | `POST /transcribe` `{path}` → `{text, quality}` |
| TTS（已加速） | 同上 | `POST /tts` `{text, language, ref_audio, ref_text, fast:true}` → wav bytes，响应头 `X-Fast-TTS: 1/0` |
| 声纹/健康检查 | 同上 | `POST /emb`、`GET /health` → `{status, version, fast_tts}` |
| 声卡切换 | `m2_server/audio_config.ps1` | `powershell -NoProfile -ExecutionPolicy Bypass -File <ps1> -action apply\|restore\|reset` |
| 音色参考音频 | `D:/变声/tts_models/ref/meituan_rat_002.wav` | 22.05kHz 单声道，3.08s |

**实测性能基线（2026-08-30）**

| 环节 | 实时率 |
|---|---|
| ASR（faster-whisper small @ CUDA） | **11.3x**（6s 音频 → 0.53s） |
| TTS（fast / CUDA Graph） | **~2.0x**（生成 1s 音频约 0.5s） |
| TTS（原版，回退路径） | 0.40x |

TTS 输出：**24kHz 单声道** wav。

## 三、延迟预算：为什么流式可行

关键：**TTS 生成快于播放（RTF ~2）**，所以按句切片后处理时间不会累积。

以 2.5s 的一句话为例：

```
说话        2.5s   （采集并行进行）
VAD 判停    0.3s
ASR         0.2s   （RTF 11.3）
TTS         1.2s   （RTF 2.0，2.5s 音频）
--------------------------------
处理合计    1.7s  <  2.5s（句子时长）  → 跟得上，不累积
```

结论：**稳定滞后约 1.5~2s**，且不会随时间增长。这个量级相当于网络不佳的通话，可用。

对比：整句模式（等用户说完一整段再处理）滞后约 3.8s，适合发语音消息但不适合通话。

## 四、架构

```
┌──────────────────────────────────────────────────────┐
│ 8000 后端 (D:\变声\.venv)                             │
│   /api/cascade/start | /stop | /status                │
│   启动前：确保 8001 worker 就绪 + 切声卡(apply)        │
└───────────────┬──────────────────────────────────────┘
                │ 子进程（必须用 D:\RVC\.venv，理由见坑 #1）
                v
┌──────────────────────────────────────────────────────┐
│ cascade_stream.py   (D:\RVC\.venv\Scripts\python.exe) │
│                                                       │
│  麦克风(真麦) ──sounddevice──> VAD 分块 ──> 临时 wav   │
│        │                                              │
│        ├─ POST 8001 /transcribe ──> text              │
│        ├─ POST 8001 /tts (fast)  ──> wav bytes        │
│        └─ sounddevice 播放 ──> CABLE Input             │
│                                                       │
│  状态回写：outputs/cascade_state.json（前端轮询）      │
└──────────────────────────────────────────────────────┘
                                                       
信号流： 真麦 → [ASR→TTS] → CABLE Input → CABLE Output → 微信(录音设备=CABLE Output)
```

**与 RVC 实时的关系**：信号出口完全相同（都是 CABLE Input），
所以微信侧无需任何改动，两种方案可无缝切换。

## 五、后端接口设计（`m2_server/cascade.py`）

新增 router，挂载到 `/api`，参照现有 `offline_vc.py` / `rvc_live.py` 的写法。

```
POST /api/cascade/start
    body: { ref_audio?: str, chunk_max_s?: float, mode?: "stream"|"whole" }
    -> { ok, pid, output_device }
    前置校验：
      1. 8001 worker 就绪（未起则触发一次，见坑 #3）
      2. 与 RVC 实时互斥（rvc_live._live_proc_alive() 为真则 409）
      3. audio_config.ps1 apply（首次自动备份原设备）
    启动子进程，守护线程等待退出后自动 restore（见坑 #6）

POST /api/cascade/stop
    -> { ok, restored }
    终止子进程 + audio_config.ps1 restore（失败走 reset 兜底）

GET  /api/cascade/status
    -> { running, stage, last_text, last_audio_s, chunks, avg_latency_s, error }
    stage: idle | capturing | asr | tts | playing
```

状态用模块级 dict（与 `OFFLINEVC_STATE`、`MINE_STATE` 同风格），
子进程把状态写入 `outputs/cascade_state.json`，后端读取后返回。

## 六、子进程设计（`m2_server/cascade_stream.py`）

运行在 `D:\RVC\.venv\Scripts\python.exe`（**唯一有 sounddevice 的环境**）。

### 主循环

```
1. 打开输入流：sounddevice.InputStream(device=真麦, samplerate=16000, channels=1)
2. 打开输出流：sounddevice.OutputStream(device=CABLE Input, samplerate=24000, channels=1)
3. 预热：先用参考音频跑一次 /tts（极短文本），把 prompt 缓存建好（见坑 #4）
4. 循环：
     a. 从输入流取音频，喂 VAD
     b. VAD 判停（或达到 chunk_max_s 上限）→ 得到一段完整语音
     c. 写临时 wav → POST /transcribe → text
     d. text 为空则丢弃，回到 a
     e. POST /tts (fast) → wav bytes（24kHz）
     f. 推入输出流播放队列
     g. 更新 outputs/cascade_state.json
5. 收到终止信号 → 关闭流 → restore 声卡
```

### 分块策略（关键设计点）

**双判据切句，宁长勿短**：

- **VAD 静音判停**：连续静音 ≥ 400ms 认为一句结束
- **上限强制切**：单块最长 `chunk_max_s`（默认 6s）强制切出，防止长句把延迟撑大
- **下限丢弃**：短于 0.5s 的块直接丢弃（多为喘息/噪声）

为什么宁长勿短：切得太碎会让 TTS 逐块重建韵律，听起来一顿一顿；
切得长则延迟线性上升。`chunk_max_s=6s` 是延迟与韵律的折中，做成可调参数。

**不要用 whisper 的 `vad_filter` 来分块** —— 它是 ASR 内部过滤器，会把太短的片段整体丢掉，
导致你无法区分「用户真的没说话」和「被过滤器吃掉了」。分块必须在 ASR 之前独立完成。

### 播放平滑

- 输出流用 **24kHz**（与 TTS 输出一致，避免重采样引入音质损失）
- 块与块之间做 **20~30ms 交叉淡化**，避免拼接处的咔哒声
- 播放队列要有**最小缓冲**（建议 ≥ 0.5s 音频量）再开始播放，
  防止 ASR/TTS 抖动导致播放断流

## 七、前端设计（`web/src/pages/Cascade/`）

参照现有 `web/src/pages/Live/` 与 `OfflineVc/` 的结构（Page + index + use* hook）。

- **大录音按钮**：开始 / 停止
- **状态条**：`聆听中 → 识别中 → 合成中 → 播放中`，带各阶段耗时
- **实时文字流**：显示 ASR 识别出的文字（便于用户发现识别错误）
- **延迟指示**：显示当前端到端滞后秒数
- **参数区**：`chunk_max_s` 滑块、整句/流式切换、目标音色选择
- **提示**：明确告知「微信需把录音设备设为 CABLE Output」，与 Live 页一致

## 八、必须避开的坑（按重要性排序）

### 坑 1：采集必须跑在 RVC venv

主环境 `D:\变声\.venv` **没有** sounddevice / pyaudio。
`D:\RVC\.venv` 有 sounddevice。
→ 子进程必须用 `D:\RVC\.venv\Scripts\python.exe`。
（这与 `offline_vc.py` 已有的做法一致，照抄即可。）

### 坑 2：必须从真麦采集，不能从 CABLE Output 采

否则 CABLE Output → 采集 → 合成 → CABLE Input → CABLE Output 形成回环啸叫。
输入设备固定用真麦（默认 `麦克风阵列 (Senary Audio)`），输出固定 CABLE Input。

### 坑 3：worker 是懒启动的，必须先预热

8001 worker 首次拉起要加载 4GB 模型，**耗时约 26~35s**。
若不加处理，用户点开始后第一句话会卡半分钟。
→ `start` 时先调一次 8001 `/health`，不通则触发一次 `/api/tts` 预热，
**就绪后再切声卡并返回**，前端显示「模型加载中」。

### 坑 4：TTS 的 prompt 缓存要在启动时预热

`/tts` 首次对某个 `(ref_audio, ref_text)` 组合会编码参考音频（额外开销）。
→ 进程启动后先用一个极短文本跑一次 `/tts`，把 prompt 建好再进入主循环。

### 坑 5：`/tts` 是 `async def` 但内部是阻塞推理 —— 会堵死事件循环

```python
@app.post("/tts")
async def tts(req: Request):        # async，但里面是同步 GPU 推理
    ...
    wavs, sr = MODEL.generate_voice_clone(...)   # 阻塞数秒
```

这会**阻塞整个 uvicorn 事件循环**，期间 `/health`、`/transcribe` 全部无法响应。
流式场景下会导致状态查询卡死、后续 ASR 排队。

**修法（二选一）**：
- 把 `/tts`、`/tts_speaker` 改成同步 `def`（FastAPI 会自动放进线程池，不堵循环）
- 或在 async 内用 `await run_in_executor(None, ...)` 包裹阻塞调用

*注意：改同步后 `await req.json()` 要相应调整，FastAPI 的同步端点仍可注入 `Request`。*

### 坑 6：声卡切换必须能自动还原

`audio_config.ps1 apply` 会把系统录音设备切到 CABLE Output 并备份原设备到
`%LOCALAPPDATA%\rvc_audio_backup.txt`。**进程异常退出会留着备份不还原**。
→ 照抄 `rvc_live.py` 的三层保障：
  1. 守护线程在子进程退出后自动 `restore`
  2. `restore` 失败则用 `reset` 兜底
  3. 服务器启动时 `_auto_clean` 检查残留备份并还原

### 坑 7：与 RVC 实时互斥

两者抢 GPU（RTX 5060 Laptop 8GB）且都要占 CABLE。
→ `start` 前检查 `rvc_live._live_proc_alive()`，为真则返回 409 并提示先停止实时变声。

### 坑 8：GPU 被第三方程序挤占会放大延迟

已确认 Epic Games Launcher / EOS Overlay 会占 GPU 导致推理卡顿甚至驱动挂起。
→ 状态里暴露 `avg_latency_s`，异常升高时前端提示检查 GPU 占用。

### 坑 9：采样率别搞混

- 采集：16kHz（ASR 要求）
- TTS 输出：24kHz
- 参考音频：22.05kHz（不影响，worker 内部处理）
- 播放：24kHz（与 TTS 输出一致，不要重采样）

### 坑 10：并发限制

8001 单进程、无并发批处理。流式是逐块串行请求，正常情况够用，
但**不要在前端开多个会话**。若后续需要，再考虑请求队列。

## 九、验收标准

| 项 | 标准 | 方法 |
|---|---|---|
| 端到端滞后 | **≤ 2.5s**（2.5s 句子） | 状态里的 `avg_latency_s`，取 10 句中位数 |
| 滞后是否累积 | 连续说 60s，滞后**不增长** | 对比第 1 句与第 20 句的滞后 |
| 播放连续性 | 无明显断流/咔哒 | 人工听 + 检查块间静音 < 50ms |
| 内容正确 | ASR 回听输出，与原话逐字一致 | 用 `/transcribe` 回听合成音频 |
| 音色正确 | 输出与参考音频声纹相似度 **≥ 0.95** | `POST /emb` 算余弦（原版为 0.983） |
| 无回环啸叫 | 麦克风不采 CABLE Output | 代码审查 + 实测 |
| 停止后声卡还原 | 备份文件消失，系统录音设备回到真麦 | 停止后检查 `%LOCALAPPDATA%\rvc_audio_backup.txt` |
| 异常退出兜底 | 强杀子进程后声卡仍还原 | kill 进程后验证 |
| 与 RVC 互斥 | 实时运行中启动级联返回 409 | 接口测试 |

**性能回归基线**（若实施后低于此值说明有问题）：
ASR RTF 11.3、TTS RTF ~2.0。若 TTS 掉到 1.0 以下，检查是否回退到了原版路径（看 `X-Fast-TTS` 头）。

## 十、交付物清单

| 文件 | 说明 |
|---|---|
| `m2_server/cascade.py` | 后端 router：start / stop / status、声卡切换、互斥检查 |
| `m2_server/cascade_stream.py` | 子进程：采集 + VAD + ASR + TTS + 播放（跑在 RVC venv） |
| `m2_server/qwen3_tts_service.py` | **修改**：修坑 5（`/tts` 改同步或 executor） |
| `m2_server/server.py` | **修改**：挂载 cascade router |
| `web/src/pages/Cascade/` | 前端页面（Page + index + useCascade） |
| `web/src/api/client.ts` | **修改**：新增级联接口与类型 |
| `tools/` | 验收脚本：延迟统计、声纹校验、回环检查 |

## 十一、建议实施顺序

1. **先只做后端子进程**，用命令行直接跑 `cascade_stream.py`，验证采集→ASR→TTS→播放全链路通
2. 测出真实滞后，据此调 `chunk_max_s` 与静音判停阈值
3. 修坑 5（事件循环阻塞）——这一步不做，状态查询和并发都会有问题
4. 接后端 router（含声卡切换与互斥）
5. 最后做前端页面

**不要一上来就写前端**：核心不确定性在「分块策略 + 实际滞后」，
这两个只有跑起来才知道，先用一个命令行原型把参数调准，能省掉大量返工。
