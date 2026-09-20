# 变声工坊 · VoiceMorph

**把变好的声音直接送进微信和游戏的本地实时变声器。**

不打字生成音频文件，而是改造你正在说的话；不上云，全程本机。

```
你的麦克风 ──▶ 本机 GPU 推理（RVC，p50 50ms）──▶ 目标音色 ──▶ 微信 / 游戏 / 会议
```

和「本地语音工作站」类工具（如 VoiceStudio）的区别一句话：**它们交付一个音频文件，我们交付一句已经发出去的话。**

| | 本地语音工作站 | **变声工坊** |
|---|---|---|
| 你给它 | 一段文字 / 一个视频 | **你的麦克风** |
| 它给你 | 一个音频/视频文件 | **你已发出去的语音条** |
| 等待 | 秒到分钟 | **50ms** |
| 场景 | 配音、有声书、转录 | **开黑、微信语音、直播** |

三条主路径：

- **开麦说话**（实时变声）：RVC via 虚拟声卡 → 注入微信/游戏/Discord/OBS 的麦克风。
- **打字让它说**（TTS）：输入文字 → 用你选的音色（含真声克隆）合成 → **一键发进微信**。
- **训练变声**（自建音色）：丢一段素材 → 自动切片质检、挑出干净片段 → 训练成你自己的音色。

三端一体：**桌面端**（主力，Electron + React，自带 Python 推理后端）· **移动端**（Expo App，手机打字/遥控，PC 在局域网内推理并自动发到微信）· **Web 端**（同一套 React 前端，浏览器直连或由桌面壳托管）。

> 所有推理都在**本机/局域网**完成，不上云，隐私安全。PC 端需 NVIDIA 显卡（本机 RTX 5060 8GB）。

---

## ⚠️ 免责声明（务必先读）

本项目**仅供学习与技术研究**。使用者必须遵守以下红线：

- **仅允许克隆以下声音**：① 你自己的声音；② 你已获得明确授权的他人声音；③ 无真人主体的虚构/AI 配音角色音色。
- **严禁**用于冒充他人、电信诈骗、造谣诽谤或任何违法用途。
- 克隆**配音角色音色**（如某些 AI 二创角色）时，建议标注原始二创出处。
- 因不当使用引发的任何法律与道德责任，由使用者自行承担，项目作者不承担连带责任。

---

## 整体架构

```
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│  桌面端 Electron │   │  Web 浏览器   │   │  移动端 Expo   │
│ (React 19 窗体) │   │ (Vite/局域网) │   │ (手机遥控/打字) │
└──────┬───────┘   └──────┬───────┘   └──────┬───────┘
       │  HTTP 127.0.0.1:8000/api/*          │
       └──────────────┬──────────────────────┘
                      ▼
        ┌─────────────────────────────────────┐
        │   M2 后端  FastAPI (:8000)           │
        │   - 素材流水线 / 切片质检 / 说话人分离  │
        │   - TTS / 离线变声 / 实时变声 / 音色挖掘 │
        │   - 微信发送 / 桌宠内录 / A-B 盲听      │
        │   - 音频设备切换（VB-CABLE）/ 音色包    │
        └──────────────┬──────────────────────┘
                       │ spawn 子进程 / 懒启动
        ┌──────────────┼──────────────────────────────┐
        ▼              ▼                              ▼
  Qwen3-TTS 1.7B   RVC 实时(via VB-CABLE)        Seed-VC / DSP
  (venv312 :8001)  → 注入微信/会议/游戏麦克风      (离线变声备选)
```

三端共用同一个本地 M2 后端；本机回环永远放行，局域网请求可选 `VM_API_TOKEN` 鉴权。
移动端走局域网时带 `api_key` 查询参数过 Token（原生音频播放器带不了自定义 Header）。

---

## 技术栈

| 层 | 选型 |
|----|------|
| 桌面壳 | Electron 32 + electron-builder（NSIS 安装包） |
| 前端 | React 19 + Vite 5 + TypeScript + Tailwind 3 + Zustand 4 + react-router 6 |
| 移动端 | Expo SDK 54 + React Native 0.81 + expo-router（文件路由）+ Zustand |
| 后端 | Python 3.11（FastAPI + uvicorn）；Qwen3-TTS worker 独立 venv312（torch 2.8 + cu129） |
| 变声引擎 | Qwen3-TTS 1.7B（零样本/QLoRA 克隆）、RVC（实时变声）、Seed-VC（离线备选）、demucs、DeepFilterNet、CAM++ 声纹 |
| 音频 | VB-Audio CABLE（虚拟声卡）；`audio_config.ps1` 调 Core Audio IPolicyConfig 切默认播放/录音设备 |

---

## 目录结构

```
变声/
├── web/                  # 桌面端（Electron + React）；构建产物 web/dist；打包 release2/
│   ├── src/              # React 前端（7 个主路由页 + useAppStore 轮询 health/voices）
│   ├── electron/         # 主进程按职责拆 6 模块：main(装配) backend(后端进程/HTTP)
│   │                     #   pet(桌宠) pet-actions(桌宠动作) alt-hint(置顶提示) update-ipc(更新)
│   └── package.json
├── mobile/               # 移动端（Expo）；app/ 文件路由 + api.ts 调 PC 后端
├── m2_server/            # M2 后端（FastAPI，端口 8000）
│   ├── server.py         # 应用装配层：app + 鉴权中间件 + CORS + 路由注册 + SPA 托管
│   ├── runtime.py        # 跨模块共享状态（流水线/挖掘/内录状态、路径常量）
│   ├── system_api.py voices_api.py raw_media_api.py pipeline_api.py clips_api.py
│   ├── tts_api.py mine_api.py capture_api.py ab_api.py audio_api.py
│   ├── media_api.py rvc_dataset_api.py         # 按域拆分的路由模块
│   ├── config.py         # 路径/端口/鉴权/模型目录（环境变量可覆盖）
│   ├── common.py history.py clip_qc.py speaker_sep.py loopback_capture.py ...
│   ├── cascade.py rvc_live.py offline_vc.py seed_vc.py audiobook.py
│   ├── effects.py wechat_voice.py finetune.py history_api.py   # 各能力 APIRouter
│   └── qwen3_tts.py qwen3_tts_service.py   # Qwen3-TTS 客户端 / 常驻 worker(:8001)
├── m1_workshop/          # M1 素材流水线（pipeline.py：提轨→去BGM→切 2.5~10s 片段）
├── tts_models/           # Qwen3-TTS 权重（qwen3-tts-1.7b-base 等）
├── media/                # 素材/切片/音色库/语料（raw_videos, vocals, demucs_out, clips, voicebank, rvc_dataset）
├── outputs/              # 转换/合成产物
├── pretrained_models/    # 其他预训练模型
├── seed_vc/ seed_vc_repo/  # Seed-VC 引擎（离线变声备选）
├── tools/                # setup_env.ps1 / doctor.py / cleanup.ps1 等一键脚本
├── agents/ .workbuddy/ .codebuddy/  # AI 协作约定与技能（junction 指向 agents/skills）
└── AGENTS.md             # AI 协作铁律（改动即 git commit + 补测 + 记 memory）
```

---

## 桌面端框架（你重点问的）

主进程按职责拆为 6 个 CommonJS 模块（`main.cjs` 只留装配层，1258→153 行）：

| 模块 | 职责 |
|---|---|
| `main.cjs` | 装配层：createWindow、Ctrl+Alt+V 热键、退出还原声卡、whenReady 启动链 |
| `backend.cjs` | 后端进程生命周期（resolveProjectRoot/startBackend/stopBackend）、HTTP 工具（backendPost/httpJson）、故障提示与环境体检弹窗、`backend:*` IPC |
| `pet.cjs` | 桌宠窗口/偏好/显隐轮询/页面导览；业务动作经 `createPetWindow(actions)` 依赖注入，无循环 require |
| `pet-actions.cjs` | 桌宠触发的动作：发微信语音/试听/loopback 挖掘/实时变声开关 |
| `alt-hint.cjs` | 微信发送置顶提示横幅 + Alt 倒计时引导 |
| `update-ipc.cjs` | 自动更新 IPC（check/download/install/skip）+ 启动静默检查 |

应用启动链（装配在 `main.cjs`，实现在各模块）：

1. **推导项目根** `resolveProjectRoot()`（backend.cjs）：开发态用 `D:\变声`，打包态用 `resources/backend`，多候选兜底，不再硬编码盘符。
2. **拉起后端** `startBackend()`：`spawn` 启动 `m2_server/server.py`（cwd=m2_server，注入 `PYTHONPATH / VM_MEDIA_DIR / VM_OUTPUTS_DIR / VM_PROJECT_ROOT`）；端口被自己残留进程占用会先清再拉。
3. **开 3 个窗口**：主窗口（React）、桌宠窗口（`pet.html` 透明常驻右下角）、置顶提示窗（`alt-hint.html` 微信发送引导，鼠标穿透）。
4. **注册 IPC**：`backend:*`（启停后端）、`pet:*`（桌宠）、`update:*`（自动更新）；全局热键 `Ctrl+Alt+V` 启停级联变声。
5. **退出清理** `before-quit`：还原音频设备 + 释放热键 + 停后端。

**两条通信通道**：
- 渲染进程 ↔ 主进程：**IPC**（`preload.cjs` 暴露 `window.electron`）—— 用于启停后端、桌宠交互、更新。
- 渲染进程 ↔ 后端：**HTTP `fetch 127.0.0.1:8000/api/*`**（`webSecurity:false` 直连）—— 业务数据走这条。
- 主进程 ↔ 后端：`spawn` + HTTP（探活、微信发送、热键）。

**打包**：`electron-builder` 把 `m2_server` + `tools` + `requirements.txt` 塞进 `resources/backend/`，前端静态放 `resources/backend/web_dist`，输出 `release2/`（**不是旧 README 写的 `release/`**）。

---

## 后端 M2（FastAPI）

`server.py` 是统一应用，通过 `APIRouter` 把各能力挂到 `/api` 前缀（开发走 vite proxy、生产直连共用一套路径）。**26 个独立 router**：

`cascade`（级联变声）、`rvc_live`（实时变声）、`offline_vc`（离线变声）、`seed_vc`、`audiobook`（有声书）、`effects`（音效）、`wechat_voice`（微信发送）、`finetune`（音色微调/QLoRA）、`history_api`（历史）。

原 `server.py` 内联的业务已于 2026-09-03 按域拆为独立路由模块：`system_api`（health/diagnose）、`voices_api`（音色库+音色包）、`raw_media_api`（素材库/上传）、`pipeline_api`（流水线）、`clips_api`（切片/质检/说话人分离）、`tts_api`、`mine_api`（音色挖掘）、`capture_api`（桌宠内录）、`ab_api`（盲听）、`ab_chain`（A/B 链路对比 + 自然度打分）、`audio_api`（设备配置/巡检）、`media_api`（静态音频）、`rvc_dataset_api`（训练集）、`market_api`（模型市场）、`pet_market_api`（桌宠市场）；共享状态收敛在 `runtime.py`。

接口清单（节选）：`GET /api/health`、`/api/diagnose`、`/api/voices`、`/api/raw_videos`、`POST /api/pipeline/run`、`/api/clips`、`POST /api/voicebank`、`POST /api/tts`、`POST /api/mine/run`、`POST /api/capture/loopback`、`POST /api/ab/run`、`/api/rvc/live/*`、`/api/wechat/*`、`/api/audio/*`。

注册走**容错加载**（`m2_server/plugin_loader.py`）：任一模块导入失败只让它自己不可用，
不再拖垮整个后端 —— 缺 torch / 缺权重 / 没装微信都是**正常状态**，不该表现为「软件打不开」。
启动时 stdout 会打一行 `[plugin_loader] 路由模块 N/M 个已加载`，坏掉的逐条列出原因
（**M 会随启用集变化**：关掉的能力不加载，见下面「关掉一个能力」）；
`GET /api/capabilities` 是同一份数据的 HTTP 出口（前端顶部降级横幅读它）。

**挂哪些模块由能力清单决定**，不写在 `server.py` 里：`m2_server/plugins/<id>/plugin.json`
声明每个能力占哪些 router，`plugin_manifest.mount_plan()` 是唯一出口。
改动前请看 `m2_server/tests/test_plugin_loader.py` 与 `tests/test_route_shadowing.py`。

> ⚠️ 早前这里写过「注册顺序是行为，被逐字冻结着」—— **2026-09-20 实测推翻**：
> 139 条真实路由里**没有任何两条 router 路由互相遮蔽**，65 条遮蔽关系全部是
> 「router vs SPA 兜底」且无害。也就是说顺序对 handler 归属**没有影响**，
> 冻结它只会在每次加插件时逼人改一次快照。现在守的是「不许出现重叠」
> （`test_route_shadowing.py`），而不是某个历史顺序。

> 注意：当前环境的 FastAPI 对 `include_router` 采用惰性挂载（路由不展开进 `app.routes`），不要用「枚举路由表」的方式做断言，用 TestClient 真实请求验证（见 `tests/test_server.py`）。

---

## OpenAI 兼容 API（给开发者）

**已经写好的 OpenAI SDK 代码，把 `base_url` 指到本机就能用**（`m2_server/openai_compat.py`，挂 `/v1` 前缀）。

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="example")
resp = client.audio.speech.create(
    model="tts-1",
    voice="kangaroo",      # 本机音色 id；写 "default" 表示用当前选中的音色
    input="你好，这是我本机跑出来的声音。",
    response_format="mp3",  # wav / mp3 / opus / aac / flac / pcm
)
resp.stream_to_file("out.mp3")
```

| 端点 | 说明 |
|---|---|
| `POST /v1/audio/speech` | 文字转语音，返回音频字节流 |
| `GET /v1/models` | 返回 `tts-1` / `tts-1-hd`（本机不区分模型，但列出来避免客户端探测失败） |
| `GET /v1/audio/voices` | 本项目扩展：列出本机可选音色 id |

两点与官方不同的地方，先说清楚免得踩坑：

- **`voice` 只认本机音色 id**。官方的 `alloy`/`echo`/`nova` 等内置音色本机没有，传了会**明确报 400**（而不是悄悄换一个声音——静默替换会让你以为生效了）。不确定用什么就写 `"default"`。
- **错误体是 OpenAI 形状**（`{"error": {"message", "type", "code"}}`），所以 SDK 的异常处理能正常拿到我们的中文提示。

> `base_url` 写 `http://127.0.0.1:8000/v1` —— 前缀是 `/v1` 不是 `/api/v1`，这是 OpenAI SDK 的语义要求（SDK 会自动拼 `/audio/speech`）。
> 未提供 `/v1/audio/transcriptions`：本项目有 ASR 能力但走的是 RVC 子进程链路，未做稳定的文件转写端点，宁可 404 也不给一个「有时能用」的假接口。

---

## 移动端（Expo）

Expo SDK 54 + RN 0.81 + expo-router 文件路由 + Zustand。`mobile/src/api.ts` 调 PC 后端实现**微信语音遥控**：手机打字 → PC 用 Qwen3-TTS 合成 → 自动发到微信（走 `wechat_voice` 接口 + 桌宠置顶引导点击发送）。当前需手动填 PC 局域网 IP（后续补 mDNS 自动发现）。

---

## 环境搭建（一次性）

> 前提：NVIDIA 显卡（本机 RTX 5060），已装 git、ffmpeg。

```powershell
# 1. 创建虚拟环境（建议 Python 3.11）
python -m venv .venv
.\.venv\Scripts\activate

# 2. 装 PyTorch —— 5060 是 Blackwell 新架构，必须装 cu128！
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128

# 3. 安装其他依赖
pip install numpy librosa soundfile pydub wavmark eng_to_ipa inflect unidecode pypinyin cn2an jieba langid
pip install -r requirements.txt

# 4. Qwen3-TTS 需要独立环境 venv312（torch2.8+cu129），见 tts_trial/venv312，当前环境内已就绪
```

### 自检（改完代码 / 依赖后）

```powershell
python tools\check.py                 # 全量：licenses + requires + electron + ps1lint + ruff + nodetest + pytest + (tsc + vitest)
python tools\check.py --fast          # 提交前（pre-commit 钩子跑的就是它）
python tools\check.py --ci-fidelity   # 复刻 CI：在只装 requirements-dev.txt 的干净 venv 里跑 CI 那条命令
```

**改了依赖、或加了新测试，一定跑 `--ci-fidelity` 再推。** 本机 `.venv` 全绿不代表 CI 绿：
本机多装的包（包括别的包顺手带进来的**传递依赖**）会把"依赖没声明"这类问题整个挡住。
它自动从 `.github/workflows/ci.yml` 读版本号、建 `.venv-ci/`、跑 CI 同一条命令，
并在结尾诚实列出没被复刻的差异（runner 镜像 / Linux 大小写 / `npm ci` 全新安装）。
来龙去脉见 `docs/犯错指南.md` §3.9、§3.36。

前端单测：`cd web && npm run test:run`（vitest + jsdom）。
**它的门禁位置是 `tools/check.py` 的 `web` 步**（tsc 之后），不在 `--fast` 里；
CI 的 `web` job 同样跑 `npx vitest run` —— 两边保持一致，别只在本机跑。

git 钩子由 `python tools/install_hooks.py` 安装（`core.hooksPath=.githooks`）：
pre-commit 跑 `--fast`，pre-push 跑全量。

---

## 新机器部署（换电脑 / 给别人装）

前端只是界面，推理全在本地 Python 后端里，所以**必须先准备一次后端环境**：

```powershell
# 1) 一键准备环境（建 .venv → 装核心依赖 → 按「启用集」装各能力的 extras）
powershell -ExecutionPolicy Bypass -File .\tools\setup_env.ps1

# 2) 体检：逐项列出缺什么、缺了怎么补
.\.venv\Scripts\python.exe tools\doctor.py
```

- **默认只装「标准」套餐**（核心 + 训练变声 / 输字变声 / 实时变声 / 工具箱 / 试音间）。
  以前是"一刀全装"，`torch` 那 2GB 无论你用不用都得下；现在按需装。
- 换套餐：`-Preset light`（只要一个能变声的，硬盘紧张）/ `-Preset full`（全开）/
  `-Preset core`（只装核心，跑测试用）；也可 `-Plugins sound.tts,sound.rvc-live` 精确指定。
- **关掉的能力不装它的重包**：在设置页关掉「输字变声」后重跑本脚本，`qwen-tts` 就不会再装。
- `-SkipTts`：已有独立 TTS 环境（`tts_trial\venv312`）时用，跳过 `qwen-tts`。
- `-DryRun`：只打印要执行的命令，不真装（换机器前先看一眼）。
- 默认装 `cu128`。RTX 50 系（Blackwell）若报 kernel 不兼容，改试 `-CudaTag cu129`。
  ⚠️ `torch` / `torchaudio` **必须从 CUDA 索引装**（脚本已内置）——直接 `pip install torch`
  会装成 CPU 版，症状是"能跑但不用显卡"（静默失效，不报错）。
- 应用启动后若后端没起来，会弹窗说明原因并指向 `tools\` 下的脚本，不会只给你一个空界面。
- 后端运行日志：`%APPDATA%\<应用名>\backend.log`。

可选能力各自独立，缺了不影响其它功能：

| 能力 | 依赖 | 缺了会怎样 |
|------|------|-----------|
| 文字转语音 | `qwen-tts` + `tts_models/` 权重 | 「输字变声」页不可用 |
| 实时变声 | RVC 整合包（`VM_RVC_ROOT`）+ VB-Audio CABLE | 「实时变声」页不可用 |
| 微信发送 | 微信 PC 版 + 桌宠置顶引导 | 「微信发送」不可用，其余正常 |

> 这三行只是摘要。**机器可读的那份在 `m2_server/plugins/<id>/plugin.json`**：
> 每个能力声明自己占哪些 router、需要哪些「重可选依赖」（另外几个 GB 的 Python 包与模型）、
> 对应的前端页面与侧边栏项、以及缺了会怎样。当前共 19 个能力（7 个核心 + 12 个可选），
> 可用 `GET /api/plugins` 看实时状态（`ok` / `broken` / `disabled` 三态 + 开关结果 `enabled`）。
> 改这里的表格前先看 `docs/插件化设计.md` —— 那份清单才是真相源，这张表是给人快速扫的。
>
> 想先看看"这台机器按当前启用集会装什么"：`python tools\plugin_extras.py`
> （加 `--check` 逐项对账"装了没"，加 `--json` 给脚本消费）。

**关掉一个能力**（设置 → 能力管理）会发生四件事，全部**重启应用后生效**：

1. 后端不加载它的路由与启动钩子 —— 不拉 torch/CUDA 上下文、不占显存
   （关掉「输字变声」= 不再起那个加载 4.9G 模型的预热线程）
2. 界面上不再出现它的页面与侧边栏入口
3. `tools\setup_env.ps1` 下次运行时跳过它的重包
4. `/api/diagnose` 不再为它报红

守卫有两条：**核心能力不可关**；**还有别的能力依赖它时不许关**
（会提示"先关掉依赖它的那个"）。被依赖的会自动保留 —— 不会出现"关了 A 结果 B 变砖"。
套餐预设（轻量 / 标准 / 全能）与逐项开关共用一份定义，在 `plugin_manifest.PRESETS`。

### 目录清理

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\cleanup.ps1           # 预演，不删任何东西
powershell -ExecutionPolicy Bypass -File .\tools\cleanup.ps1 -Apply    # 真的删
```

---

## 当前进度（2026-09-03）

✅ **环境**：Python 3.11 venv、PyTorch 2.9.1+cu128（RTX 5060 CUDA 正常）、ffmpeg、Qwen3-TTS 权重与 venv312 已就位
✅ **M1 素材流水线**：视频→提音轨→demucs 分离→归一化→切片(2.5~10s)→参考音频（含追加聚合），全部实测通过
✅ **切片质检 + 说话人分离**：clip_qc 多维打分（时长/响度/削波/底噪/SNR/说话人一致性）；CAM++ 自动挑主说话人
✅ **M2 文字转语音**：Qwen3-TTS 零样本/ICL 克隆（venv312 常驻 worker :8001）稳定输出
✅ **RVC 训练式音色**：Qwen3-TTS 批量生成语料 → 导出 RVC 整合包离线训练 → 权重随音色包导入导出
✅ **实时变声**：`/rvc/live/start` 一键切虚拟声卡（录音→CABLE Output）→ 实时变声 → 退出/停止自动还原 + 异常残留巡检
✅ **桌宠 + 全局热键**：透明常驻桌宠、`Ctrl+Alt+V` 启停级联变声、loopback 内录系统声音自动挖掘音色
✅ **微信发送 / 移动端遥控**：手机打字 → PC 合成 → 微信自动发送（桌宠置顶引导）；Expo App 已成型
✅ **A/B 盲听 + 音色挖掘**：同句双音色合成 + 声纹相似度评分盲听；上传视频自动聚类挖音色试听
✅ **桌面端联调**：Electron + React 七页（实时/工坊/发现/音色库/TTS/微调/离线变声），打包 `变声工坊.exe`（release2/）可运行，含自动更新

---

## 桌面端应用（`web/`）

### 直接运行（已打包）

```
web/release2/win-unpacked/变声工坊.exe
```

启动后自动完成：拉起 Python 后端（8000 端口）→ 打开桌面窗口。
（需先保证 Python 后端环境就绪，详见上方「新机器部署」。）

### 开发模式

```powershell
cd web
npm install
# 终端1：启动后端
..\.venv\Scripts\python ..\m2_server\server.py
# 终端2：前端开发
npm run dev            # http://localhost:5173
# 或直接起桌面壳（vite + electron 并行）
npm run electron:dev
```

### 打包 exe

```powershell
cd web
npm run electron:build    # 产出 release2/win-unpacked/ 和 NSIS 安装包
```

安装包内含前端与**后端源码**（`resources/backend/`），但不含 Python 与依赖 ——
那部分是几个 GB，只能由 `tools\setup_env.ps1` 在目标机器上装一次。

### 移动端开发

```powershell
cd mobile
npm install
# 改 mobile/src/store.ts 里的 PC 后端地址为 本机局域网IP:8000（默认 http://192.168.1.10:8000）
npx expo start          # 扫码或连模拟器；真机需同一局域网
```

---

## 使用流程（以「袋鼠骑士」为例）

| 步骤 | 操作 |
|------|------|
| ① 存素材 | 把目标配音视频/音频放进 `media/raw_videos/`（桌面端可拖拽上传） |
| ② 跑流水线 | 提音轨+去BGM+切片段（桌面端「音色工坊」页或 `POST /api/pipeline/run`） |
| ③ 质检/说话人分离 | 切片打分 + CAM++ 挑主说话人，剔除 BGM/他人声片段 |
| ④ 建音色 | 勾选片段/自动优选 → 参考音频；或「音色挖掘」自动聚类挖候选试听 |
| ⑤ 文字转语音 | 用该音色做 TTS（零样本/ICL 克隆免训练，秒级出结果） |
| ⑥ 训练实时变声 | 批量生成 RVC 语料 → 导出整合包离线训练 → 实时变声可用 |
| ⑦ 微信/移动遥控 | 手机打字 → PC 合成 → 微信自动发送 |

### 启动转换服务

```powershell
python m2_server/server.py
# 桌面端会自动拉起；手动启动供局域网手机/浏览器访问 http://<电脑IP>:8000
# 接口前缀统一 /api，详见「后端 M2」一节
```

---

## 素材量决定档位

- **文字转语音（Qwen3-TTS 零样本/ICL）**：一段 3~10s 纯净参考音频即可直接克隆、免训练，秒级出结果。适合素材稀缺场景。
- **实时变声（RVC 训练式）**：需 1 分钟+纯净语料，离线训练后相似度更高、可实时麦克风变声（游戏/会议/微信通用）。

---

## 环境变量（config.py 可覆盖）

| 变量 | 作用 | 默认 |
|------|------|------|
| `VM_SERVER_HOST` | 监听地址 | `0.0.0.0`（暴露 LAN） |
| `VM_SERVER_PORT` | 后端端口 | `8000` |
| `VM_API_TOKEN` | 启用局域网 Token 鉴权（不设则本机回环放行、LAN 无鉴权） | 空 |
| `VM_CORS_ORIGINS` | 允许的前端来源（逗号分隔） | 本地+file:// |
| `VM_MEDIA_DIR` / `VM_OUTPUTS_DIR` / `VM_PROJECT_ROOT` | 素材/产物/项目根 | 自动推导 |
| `VM_RVC_ROOT` | RVC 整合包根目录（实时变声依赖） | `D:\RVC` |
| `VM_QWEN_MODEL_DIR` / `VM_QWEN_TOKENIZER_DIR` | Qwen3-TTS 权重/分词器 | `tts_models/` |

> 安全：以 `0.0.0.0` 监听且未设 `VM_API_TOKEN` 时，后端启动会告警——暴露到 LAN 前务必设 Token 并收紧 CORS。

---

## 常见坑

| 症状 | 解法 |
|------|------|
| `no kernel image is available` | torch 装错版本，用 cu128 那行重装 |
| demucs 首次卡住 | 在下载 ~300MB 模型，等它下完 |
| 分离后人声仍带 BGM（发闷发混） | `pipeline.py` 里 `DEMUCS_MODEL` 换成 `htdemucs_ft` |
| 切片全是噪音 | `pipeline.py` 里 `SILENCE_THRESH` 调到 `-38` |
| 实时变声没声音 | 检查 VB-CABLE 已装；`/api/audio/apply` 一键设录音=CABLE Output |
| 换网络手机连不上 PC | 改 `mobile/src/store.ts` 的 PC 局域网 IP（后续补 mDNS 自动发现） |
| 转换「像但不太像」 | 零样本正常水平；素材凑到 30s+ 会明显提升；要逼真上 GPT-SoVITS/RVC 训练 |

---

## 日志与排障

后端**刻意不做日志级别管理**——这一点容易被当成缺陷，先讲清楚为什么：

- **日志即 `stdout`**。`m2_server` 顶层 66 个模块里，9 个用 `logging`、16 个直接 `print()`，
  全仓**没有** `logging.basicConfig`。原因是后端以**子进程**形态被 Electron 托管：
  `backend.cjs` 捕获它的 stdout/stderr，转发到主进程控制台并落盘到
  `%APPDATA%\变声工坊\backend.log`（`app.getPath("userData")` 下）。再加一层级别开关
  只有配置成本、没有收益。
- 因此直接 `python m2_server/server.py` 时，日志就是你的终端输出，**没有 `--log-level` 可调**。
- 子任务的独立日志落在 `outputs/` 下：级联变声 `outputs/cascade_run.log`、
  微调 `outputs/<...>/train_run.log`。
- 排障优先看 `/api/doctor`（`tools/doctor.py`）与前端「设置 → 诊断」面板，而不是翻日志。

---

## 已确认的技术决策

- 克隆方式：**VC 语音转换**（不做 ASR+TTS），保留你的语气/停顿/情绪
- 素材入口：**收视频/音频文件**（不做抖音链接解析）
- BGM 处理：流水线内置 demucs 人声分离
- 多人混入：MVP 用人工勾选 + 说话人分离辅助，不上全自动 diarization 流水线
- 推理位置：**本机/局域网**，不上云
- 移动端定位：**PC 推理 + 手机遥控**的 tether 架构（非手机本地推理）

## 明确不做（v1）

- ❌ 抖音链接自动下载解析
- ❌ 云端推理服务（纯局域网，隐私安全）
- ❌ iOS（先吃透安卓）

---

## 架构演进建议（待办，非当前阻塞）

1. ~~**后端拆包（最推荐）**~~ **✅ 已完成（2026-09-03）**：`server.py`（原 1734 行）已拆为 `runtime.py`（共享状态）+ 12 个按域路由模块（`system/voices/raw_media/pipeline/clips/tts/mine/capture/ab/audio/media/rvc_dataset_api`），server.py 只留装配层；90 个测试全过、真实后端冒烟通过，行为零变更。
2. **移动端 mDNS 自动发现**：当前手机手动填 PC 局域网 IP；加 `@react-native-community/zeroconf`，PC 后端广播 `_voicemorph._tcp.local`，手机自动发现，纯增量改动，体验提升明显。
3. **共享 API 客户端**：`web/src` 与 `mobile/src/api.ts` 各写一份 fetch，可从 FastAPI `/openapi.json` 生成共享 client 防漂移（当前规模低优先级）。
4. **桌面壳保持 Electron**：不迁 Tauri——本项目需 spawn Python 子进程 + 原生音频设备切换 + 自动更新 + 透明桌宠窗，Electron 现成且稳妥；Tauri 更适合无 Python 后端的轻量工具（如另开的「写作伴侣」）。
5. ~~**桌面壳主进程拆分**~~ **✅ 已完成（2026-09-03）**：`main.cjs`（原 1258 行）已拆为 backend / pet / pet-actions / alt-hint / update-ipc 5 个职责模块，main.cjs 只留装配层（153 行）；IPC 通道与启动顺序零变更，并顺带修复了启动更新推送因未 await 而静默失效的问题。
