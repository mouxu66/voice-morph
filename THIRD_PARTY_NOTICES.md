# 第三方组件与许可（THIRD_PARTY_NOTICES）

适用范围：**本仓库源码** + **「变声工坊」Windows 安装包**（`web/release2/VoiceMorph-Setup-*.exe`）。
事实核对日期：**2026-09-14**。每条要么写出查证来源，要么显式标 **「未核」** —— 不许用"应该是"糊过去。

> 本文件由 `tools/audit_licenses.py` 机器守护（离线，进 CI）：
> 声明的依赖集与下面的机器块对不上（漏登记 / 残留条目 / 缺义务行）直接判红。
> **加依赖请同步改机器块**，否则 CI 会挡住 —— 这是刻意的（许可漏了是**事后补不回来**的）。

## 0 本仓库自身

MIT License，© 2026 mouxu（见根目录 `LICENSE`）。

## 1 随安装包分发 → **必须随分发携带许可**

| 组件 | 许可 | 现状 / 义务 |
|---|---|---|
| Electron 32 + Chromium | Electron MIT；Chromium 为 BSD 等多许可 | ✅ 已覆盖：electron-builder 自动在安装目录生成 `LICENSE.electron.txt` + `LICENSES.chromium.html`（实测 `web/release2/win-unpacked/` 下存在）。**打包时不要删这两个文件** |
| 前端生产依赖 10 个（见机器块 `npm:`） | MIT / ISC / OFL-1.1 | 随 `web/dist` 打包进安装包。表见 §1.1 |
| **字体** `@fontsource/inter` `@fontsource/jetbrains-mono` `@fontsource/outfit` | **OFL-1.1** | ⚠️ **缺口**：目前只打包 woff2，**未随发行物附许可原文**。OFL-1.1 §1 要求再分发字体时附带版权声明与许可。版权行（取自各包内 `LICENSE` 原文）：`Copyright 2016 The Inter Project Authors` / `Copyright 2020 The JetBrains Mono Project Authors` / `Copyright 2021 The Outfit Project Authors`。**待修**：把三份 LICENSE 放进安装包 `resources/licenses/` |
| 市场占位图（OpenMoji 图标） | **CC BY-SA 4.0**，© hfg-gmuend/openmoji | ⚠️ **缺口**：署名目前只写在 `m2_server/market_manifest.py` 的注释里，**未随分发可见**。CC BY-SA 要求署名 + 相同方式共享 |
| 宠物皮肤 `furina`（`m2_server/assets/pet-skins/furina/`） | MIT，来源 `Ice-teapop/desktop-pet`（原创 SVG） | ✅ **合规范例**：目录内已附 `LICENSE`，`skin.json` 带 `license` / `attribution` 字段。新增皮肤照此办理 |
| 项目自制资产 | 同仓库 MIT | `market_imgs` 中的自绘/自生成封面；**不含** §5 记的角色形象图 |

### 1.1 前端生产依赖

| 包 | 版本 | 许可 |
|---|---|---|
| `react` / `react-dom` | 19.2.8 | MIT |
| `react-router-dom` | 6.30.6 | MIT |
| `zustand` | 4.5.7 | MIT |
| `clsx` / `tailwind-merge` | 2.1.1 / 2.6.1 | MIT |
| `lucide-react` | 1.34.0 | ISC |
| `@fontsource/{inter,outfit,jetbrains-mono}` | 5.3.0 | OFL-1.1 |

> 许可取自 `web/node_modules/<包>/package.json` 与各包内 `LICENSE` 原文（2026-09-14 实读）。
> `devDependencies`（vite / tailwind / electron-builder / typescript…）不进发行物，不在此表。

## 2 运行时不随分发（由用户自装）—— 文档义务，非分发义务

| 组件 | 许可 / 条款 | 本项目怎么用 |
|---|---|---|
| **ffmpeg** | LGPL-2.1+ 或 GPL（**取决于构建**，如 gyan essentials=LGPL） | 仅以**子进程**调用（提轨 / 宠物皮肤 atlas·gif / 预览生成）。不随包分发、不静态链接 → 不构成衍生作品。**不要**把 ffmpeg.exe 塞进安装包 |
| **VB-CABLE**（`vb-cable.com`） | **Donationware**，分发条款见下 | 本仓库**没有内置**：`find` 无安装包、`grep` 无静默安装代码（2026-09-14 实测）。README 把它列为前置依赖 → **现状合规** |
| **RVC 整合包**（`D:\RVC`，`VM_RVC_ROOT`） | `RVC-Project/Retrieval-based-Voice-Conversion-WebUI` = **MIT**（GitHub API 实读，2026-09-14） | 外部目录，用户自备。其随包预训练底模（`pretrained_v2` / `hubert_base.pt` / `rmvpe`）**未单独核** |
| **Python 运行时依赖 22 个** | 全为宽松许可，**无 GPL**（见 §2.1） | `pip install -r requirements.txt`（CUDA 索引另装 torch） |

### 2.1 Python 依赖（许可取自本机 `site-packages/*.dist-info/METADATA` 实读，2026-09-14）

torch / torchaudio / scipy / soundfile / uvicorn / httpx / python-dotenv — **BSD**；
numpy — `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0`（复合，全部宽松）；
fastapi / pydantic / pydub / demucs / sounddevice / webrtcvad / comtypes / pytest / ruff — **MIT**；
librosa — **ISC**；Pillow — **HPND**；requests / python-multipart / pyaudiowpatch — **Apache-2.0**。

- `demucs` 仓库 license = **MIT**（GitHub API 实读；该仓库已归档）。⚠️ 它的**预训练权重**未见单独许可声明 → **未核**，若要随发行物分发权重需再核。
- 独立环境（`tts_trial/venv312`、`D:\RVC\.venv`）的依赖不在本表：用户自建，不随分发。

### 2.2 VB-CABLE 分发条款（**不是"禁止再分发"，也不是"随便用"**）

来源：`vb-audio.com/Services/licensing.htm`（2026-09-14 实读，原文摘录）：

> "We allow to distribute VB-CABLE package ... with your Application (free or commercial) and possibly embed it in your installation package (VB-CABLE silent installation) **if our donationware model is applicable**."

"donationware 模型成立"的三条硬要求 —— 内置者必须至少做到：

1. 用户能**看见并识别** VB-CABLE 是 VB-Audio 的产品；
2. 用户**处于可以捐助/付费的位置**（故必须给出捐助通道）；
3. 明示：来源 `www.vb-cable.com`，并写明 "VB-CABLE is a donationware, all participations are welcome."

其他约束：

- **只有 `vb-cable.com` 的经典 VB-CABLE 可以内置**；原文："**Other VB-CABLEs like VB-CABLE A+B or C+D cannot be distributed or bundled with other product.**"；`Voicemeeter Potato` 同样禁止（"cannot be distributed or bundled"）。
- **B2B/机构场景**（用户/员工不能自行付费的场合）→ 必须按量购买许可。
- VB-Audio 建议的替代做法：不内置，而是**引导用户去官网自行安装**（"invite the user to install it as a prerequisite"）。

> 教训（与 IndexTTS2 同一课）：**许可不是二元的**。别用"能不能分发 / 能不能商用"这种标签判断 ——
> 这里的真相是"**允许内置，但有条件**"，IndexTTS2 是"**免版税可用，但有义务**"，
> 两者同属"有附加条件的宽松许可"。绕开这一课会有两种代价：要么误判成"禁止"而做无谓的技术妥协
> （例如硬要用户自己装），要么误判成"随便用"而把义务漏掉。

## 3 模型权重（运行时获取，不入仓库）

| 权重 | 许可 | 说明 |
|---|---|---|
| Qwen3-TTS 1.7B（`tts_models/`） | Apache-2.0 | 本机加载，不入库 |
| **音色市场 30 款精选** | 逐条 `license` 字段：HF 源 = "社区自训·仅供个人使用，勿商用"；魔搭源 = "未标注·仅供个人学习研究，勿商用" | 来源 `chaye741/RVC-Voice-Models`(HF, 13 款) / `hudddd/Retrieval-based-Voice`(魔搭, 17 款)。前端已展示提醒（`market_manifest.py`）。⚠️ **缺口**：未回读 HF/魔搭模型卡的 license 字段，仅用兜底文案 |
| 袋鼠音色 / 自训模型（`logs/kangaroo_v2*`） | 项目自有，素材为**自录** | 与 §4.3 一致 |
| 未接入的选型候选 | 见 `docs/product/research/m1e_语音模型选型调研_2026-09-14.md` §4 | IndexTTS2 = bilibili Model Use License（免版税可用；**§3.4(c) 不得用于改进其他 AI 模型**） |

## 4 红线（违反即违规，改动前先看）

1. **不得把 NC / Research License 权重打进任何发行物**（F5-TTS 权重 CC-BY-NC-4.0、MaskGCT / Llasa / E2、OpenAudio S1-mini NC-SA、Spark-TTS BY-NC-SA、XTTS-v2 CPML…）。清单见 m1e 报告 §4。「代码 MIT」≠「权重能用」。
2. **内置 VB-CABLE 必须带 §2.2 的三行声明**；`A+B` / `C+D` / `Voicemeeter Potato` **永不内置**。
3. **第三方素材名与本体不入库**（issue #3）：具体标题只存在于本机**不入库**的 `.audit-materials.txt`。
4. **市场选品红线**：真人政治人物 / 真人网红名条目不上架（`market_manifest.py` 注释）。
5. **不得分发密钥**：`.env` / `certs/*.pfx` 已在 `.gitignore`（含私钥的签名证书）。

## 5 已知缺口（待修 —— 别当"已经解决"）

| # | 缺口 | 修法 |
|---|---|---|
| G1 | OFL 字体许可原文未随安装包（§1） | 三份 `LICENSE` 放进安装包 `resources/licenses/`，UI 加"开源许可"入口 |
| G2 | OpenMoji 署名未随分发（§1） | 同上入口里列出 `CC BY-SA 4.0 © hfg-gmuend/openmoji` |
| G3 | ~~4 张**角色形象配图**为网络搜集（懒羊羊 / 曼波 / 孙悟空 / 派大星），**无授权链**~~ | ✅ **已解决（2026-09-14）**：全部替换为自生成原创卡通插画（`market_imgs/{lanyangyang,katoong_lanyangyang,katoong_manbo,sunwukong,paidaxing}`），并删除孤儿 `manbo.png`。需同步重推远程图库 `mouxu66/voice-market-assets` 清掉 CDN 旧图，客户端 TTL 6h 内拉新 |
| G4 | 市场条目许可只写兜底文案，未回读模型卡 | 安装时抓 HF/魔搭 `license` 字段写入 `source.json`，前端展示 |
| G5 | `demucs` 预训练权重许可未核 | 若将来随发行物分发权重，先核 |
| G6 | RVC 整合包随附底模（`pretrained_v2` / `hubert_base`）未核 | 同上；目前只作为前置依赖由用户自备，风险低 |

<!-- audit:deps:begin -->
python:comtypes
python:demucs
python:fastapi
python:httpx
python:librosa
python:numpy
python:pillow
python:pyaudiowpatch
python:pydub
python:pydantic
python:pytest
python:python-dotenv
python:python-multipart
python:requests
python:ruff
python:scipy
python:sounddevice
python:soundfile
python:torch
python:torchaudio
python:uvicorn
python:webrtcvad
npm:@fontsource/inter
npm:@fontsource/jetbrains-mono
npm:@fontsource/outfit
npm:clsx
npm:lucide-react
npm:react
npm:react-dom
npm:react-router-dom
npm:tailwind-merge
npm:zustand
<!-- audit:deps:end -->

<!-- audit:obligations:begin -->
vb-cable-terms = 内置/分发 VB-CABLE 须带来源 + donationware 声明，且只能是经典版（§2.2）
ffmpeg-external = ffmpeg 仅子进程调用，不随包分发（§2）
no-nc-weights = 任何 NC / Research License 权重不得进发行物（§4.1）
third-party-asset-names = 第三方素材名与本体不入库，清单只在本地 .audit-materials.txt（§4.3）
market-voice-disclaimer = 市场下载的社区自训音色须带"仅供学习研究，勿商用"提示（§3）
ofl-font-notice = 随发行物附带 @fontsource/* 的 OFL-1.1 许可原文与版权行（§1）
<!-- audit:obligations:end -->
