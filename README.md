# 变声 · 音色克隆工作台

开源 AI 变声项目。目标：从任意视频克隆音色 → 在你的录音里用这个音色说话。
当前版本聚焦 **PC 端全流程跑通**（扒素材 → 克隆音色 → 转换你的语音）。

---

## ⚠️ 免责声明（务必先读）

本项目**仅供学习与技术研究**。使用者必须遵守以下红线：

- **仅允许克隆以下声音**：① 你自己的声音；② 你已获得明确授权的他人声音；③ 无真人主体的虚构/AI 配音角色音色。
- **严禁**用于冒充他人、电信诈骗、造谣诽谤或任何违法用途。
- 克隆**配音角色音色**（如某些 AI 二创角色）时，建议标注原始二创出处。
- 因不当使用引发的任何法律与道德责任，由使用者自行承担，项目作者不承担连带责任。

---

## 项目架构

```
变声/
├── web/                  # M1 音色工坊桌面端（Electron + React）
│   ├── src/              # React 前端（素材→流水线→勾选→音色库→转换）
│   ├── electron/         # Electron 主进程（桌面壳，自动拉起后端）
│   ├── dist/             # 前端构建产物
│   └── release/          # 打包产物（变声工坊.exe）
├── m1_workshop/          # M1 音色工坊核心（Python）
│   ├── pipeline.py       # 视频 → 提音轨 → 去BGM → 切 3~10s 片段
│   ├── build_reference.py# 勾选片段 → 参考音频（支持追加素材聚合）
├── m2_server/            # M2 服务（核心发动机）
│   ├── server.py         # FastAPI 常驻服务（端口 8000）
│   ├── qwen3_tts.py      # Qwen3-TTS 客户端（懒启动子进程 worker）
│   └── qwen3_tts_service.py  # Qwen3-TTS 常驻 worker（venv312，端口 8001）
├── tts_models/           # Qwen3-TTS 权重（qwen3-tts-1.7b-base 等）
├── media/
│   ├── raw_videos/       # ← 素材视频放这里
│   ├── vocals/           # 提取的音轨
│   ├── demucs_out/       # demucs 分离输出
│   ├── clips/            # 切好的片段（人工勾选区）
│   └── voicebank/        # 音色库（每个音色一个子目录）
└── outputs/              # 转换结果
```

---

## 环境搭建（一次性）

> 前提：NVIDIA 显卡（本机为 RTX 5060），已装 git、ffmpeg。

```powershell
# 1. 创建虚拟环境（建议 Python 3.11）
python -m venv .venv
.\.venv\Scripts\activate

# 2. 装 PyTorch —— 5060 是 Blackwell 新架构，必须装 cu128！
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128

# 3. 安装其他依赖
pip install numpy librosa soundfile pydub wavmark eng_to_ipa inflect unidecode pypinyin cn2an jieba langid
pip install -r requirements.txt

# 4. Qwen3-TTS 需要独立环境 venv312（torch2.8+cu129，见 tts_trial/venv312），当前环境内已就绪

# 5. 验证 GPU 可用 —— 必须打印 True
python -c "import torch; print(torch.cuda.is_available())"
```

---

## 当前进度（2026-08-26）

✅ **环境**：Python 3.11 venv、PyTorch 2.9.1+cu128（RTX 5060 CUDA 正常）、ffmpeg、Qwen3-TTS 权重与 venv312 已就位
✅ **M1 素材流水线已验证**：视频→提音轨→demucs 分离→归一化→切片→参考音频（含追加聚合），全部实测通过
✅ **M2 文字转语音已验证**：Qwen3-TTS 零样本克隆袋鼠音色（venv312 常驻 worker，端口 8001）可稳定输出
✅ **M2 服务接口**：`/health`、`/voices`、`/pipeline/run`、`/clips`、`/voicebank`、`/tts`、`/rvc/dataset` 均实测可用
✅ **桌面端已打通**：Electron + React 界面（音色工坊/音色库/文字转语音/袋鼠语音四页），`变声工坊.exe` 可运行
✅ **RVC 袋鼠模型已训练完成**：Qwen3-TTS 批量生成 21 条语料 → 导出到 RVC 整合包离线训练 40 epoch（48k/v2/rmvpe），`meituan_rat.pth` + index 已就绪
✅ **RVC 实时变声已验证**：`/rvc/live/start` 一键启动 → 自动切虚拟声卡（录音→CABLE Output）→ realtime_gui 实时变声 → 退出/停止自动还原声卡
✅ **音频设备兜底**：`/rvc/live/reset` 一键强制恢复真实扬声器/麦克风；服务器启动自动清理上次异常残留的声卡切换
⏳ **真实验收**：跑通「美团老鼠音色说你的话」全流程（素材→克隆→TTS→实时变声均已单项通过，待整机联调）

---

## 桌面端应用（`web/`）

### 直接运行（已打包）

```
web/release/win-unpacked/变声工坊.exe
```

启动后自动完成：拉起 Python 后端（8000 端口）→ 打开桌面窗口。
（需先保证 Python 后端环境就绪，详见下方「启动转换服务」。）

### 开发模式

```powershell
cd web
npm install
# 终端1：启动后端
..\.venv\Scripts\python ..\m2_server\server.py
# 终端2：前端开发
npm run dev        # http://localhost:5173
# 或直接起桌面壳
npm run electron:dev
```

### 打包 exe

```powershell
cd web
npm run electron:build    # 产出 release/win-unpacked/ 和安装包
```

---

## 使用流程（以「美团老鼠」为例）

| 步骤 | 操作 | 命令 |
|------|------|------|
| ① 存素材 | 把美团老鼠配音视频存进 `media/raw_videos/`（2~3 个不同二创更准） | - |
| ② 跑流水线 | 提音轨+去BGM+切片段 | `python m1_workshop/pipeline.py`（或桌面端「音色工坊」页） |
| ③ 试听勾选 | 听 `media/clips/`，挑纯美团老鼠音色片段（剔除 BGM/其他角色） | - |
| ④ 生成参考 | 勾选片段→音色档案 | `python m1_workshop/build_reference.py meituan_rat clip_001 clip_004` |
| ⑤ 追加素材 | 素材不够时继续加 | `python m1_workshop/build_reference.py --append meituan_rat clip_009` |
| ⑥ 生成台词 | 用袋鼠音色做文字转语音（免训练克隆） | 桌面端「文字转语音」页 |
| ⑦ 生训练集 | 批量生成 RVC 训练语料 | 桌面端「文字转语音」→ RVC 训练集 |
| ⑧ 训练 RVC | 语料导出到 RVC 整合包离线训练底模 | 桌面端导出 / `POST /rvc/dataset/export`（→ `D:\RVC`） |

### 启动转换服务

```powershell
python m2_server/server.py
# 桌面端会自动拉起；手动启动供局域网手机/浏览器访问 http://<电脑IP>:8000
# 接口：GET /health, GET /voices, GET /raw_videos, POST /pipeline/run, GET /clips,
#      POST /voicebank, DELETE /voicebank/{id}, POST /tts, POST /rvc/dataset/generate 等
```

---

## 素材量决定档位

- **文字转语音（Qwen3-TTS 零样本）**：一段 3~10s 纯净参考音频即可直接克隆、免训练，秒级出结果。适合素材稀缺场景（如美团老鼠）。
- **实时变声（RVC 训练式）**：需 1 分钟+纯净语料，离线训练后相似度更高、可实时麦克风变声。

---

## 常见坑

| 症状 | 解法 |
|------|------|
| `no kernel image is available` | torch 装错版本，用 cu128 那行重装 |
| demucs 首次卡住 | 在下载 ~300MB 模型，等它下完 |
| 分离后人声仍带 BGM（发闷发混） | `pipeline.py` 里 `DEMUCS_MODEL` 换成 `htdemucs_ft` |
| 切片全是噪音 | `pipeline.py` 里 `SILENCE_THRESH` 调到 `-38` |
| 转换「像但不太像」 | 零样本正常水平；素材凑到 30s+ 会明显提升；要逼真上 GPT-SoVITS |

---

## 已确认的技术决策

- 克隆方式：**VC 语音转换**（不做 ASR+TTS），保留你的语气/停顿/情绪
- 素材入口：**只收视频文件**（不做抖音链接解析）
- BGM 处理：流水线内置 demucs 人声分离
- 多人混入：MVP 用人工勾选片段，不上说话人分离
- M1 形态：Web 页面（后续），当前先命令行跑通

## 明确不做（v1）

- ❌ 抖音链接自动下载解析
- ❌ 游戏内实时变声
- ❌ 云端服务（纯局域网，隐私安全）
- ❌ iOS（先吃透安卓）
