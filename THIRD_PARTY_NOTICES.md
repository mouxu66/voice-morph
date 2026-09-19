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
| 前端生产依赖 13 个（见机器块 `npm:`） | MIT / ISC / OFL-1.1 | 随 `web/dist` 打包进安装包。表见 §1.1 |
| **字体** `@fontsource/inter` `@fontsource/jetbrains-mono` `@fontsource/outfit` | **OFL-1.1** | ✅ **已随包分发**：三份许可原文在 `web/public/licenses/{inter,jetbrains-mono,outfit}-OFL-1.1.txt`。`web/public/` 是 vite 静态目录 → 原样进 `web/dist/` → 而 `dist` **两条路都在发行物里**（`build.files` 进 `app.asar` + `build.extraResources` 进 `backend/web_dist`），所以不需要改打包配置就满足 OFL-1.1 §1。界面上「设置 → 关于 → 开源许可」直接展示（`LicensesDialog.tsx`），后端 SPA catch-all 负责把 `web_dist/licenses/…` 发出去。版权行**由原文正则提取**、不手写 |
| **许可原文载荷**（`web/public/licenses/`） | 逐条见其 `index.json` | 由 `tools/sync_license_payload.py` 生成（**别手改**）；`tools/audit_licenses.py` 核对"声明的原文在不在、版权行对不对得上、版本与 lockfile 一致不"，并强制**字体依赖 ⇄ 载荷条目双向对齐** |
| 宠物皮肤 `furina`（`m2_server/assets/pet-skins/furina/`） | MIT，来源 `Ice-teapop/desktop-pet`（原创 SVG） | ✅ **合规范例**：目录内已附 `LICENSE`，`skin.json` 带 `license` / `attribution` 字段。新增皮肤照此办理 |
| ~~市场占位图（OpenMoji 图标）~~ | ~~CC BY-SA 4.0~~ | ❌ **撤销（phantom obligation）**：**发行物里从来没有 OpenMoji 资产**。`git log --all --diff-filter=A -- '*openmoji*'` 为空；`market_imgs/` 31 个文件全是自制插画；无相关的 npm/Python 依赖；`find` 也搜不到图标文件。那条"署名缺口"只源自 `market_manifest.py` 里一句**描述意图的注释** —— 意图没落地，义务就无从谈起。现改为**资产触发**判据（`ASSET_TRIGGERS`）：真引入图标那天，门禁会立刻要求补署名 |
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
| `@tanstack/react-query` | 5.76.1 | MIT |
| `electron-store` | 8.2.0 | MIT |
| `electron-log` | 5.2.0 | MIT |

> 许可取自 `web/node_modules/<包>/package.json` 与各包内 `LICENSE` 原文（2026-09-14 实读）。
> `devDependencies`（vite / tailwind / electron-builder / typescript…）不进发行物，不在此表。

## 2 运行时不随分发（由用户自装）—— 文档义务，非分发义务

| 组件 | 许可 / 条款 | 本项目怎么用 |
|---|---|---|
| **ffmpeg** | LGPL-2.1+ 或 GPL（**取决于构建**，如 gyan essentials=LGPL） | 仅以**子进程**调用（提轨 / 宠物皮肤 atlas·gif / 预览生成）。不随包分发、不静态链接 → 不构成衍生作品。**不要**把 ffmpeg.exe 塞进安装包 |
| **VB-CABLE**（`vb-cable.com`） | **Donationware**，分发条款见下 | 本仓库**没有内置**：`find` 无安装包、`grep` 无静默安装代码（2026-09-14 实测）。README 把它列为前置依赖 → **现状合规** |
| **RVC 整合包**（`D:\RVC`，`VM_RVC_ROOT`） | 代码 = `RVC-Project/Retrieval-based-Voice-Conversion-WebUI` = **MIT**（GitHub API 实读，2026-09-14）。**底模条款另说，见下** | 外部目录，用户自备 |
| **RVC 底模**（`hubert_base.pt` / `rmvpe.pt` / `pretrained_v2/*`） | ⚠️ **仓库标 `license: mit`，但同仓另有一份 `使用需遵守的协议-LICENSE.txt`** —— 正文在 MIT 版权行后插了一段中文：**"本软件仅供研究使用，使用软件者、传播软件导出的声音者自负全责。如不认可该条款，则不能使用/引用软件包内所有代码和文件。"**（2026-09-14 读 HF `lj1995/VoiceConversionWebUI` 原文） | **不随包分发**（用户在 `D:\RVC` 自备）→ 现状无分发义务。**但"仅供研究使用"意味着不得打进发行物** —— 已由门禁机器化（§2.3） |
| **Python 运行时依赖 22 个** | 全为宽松许可，**无 GPL**（见 §2.1） | `pip install -r requirements.txt`（CUDA 索引另装 torch） |

### 2.1 Python 依赖（许可取自本机 `site-packages/*.dist-info/METADATA` 实读，2026-09-14）

torch / torchaudio / scipy / soundfile / uvicorn / httpx / python-dotenv — **BSD**；
numpy — `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0`（复合，全部宽松）；
fastapi / pydantic / pydub / demucs / sounddevice / webrtcvad / comtypes / pytest / ruff — **MIT**；
librosa — **ISC**；Pillow — **HPND**；requests / python-multipart / pyaudiowpatch — **Apache-2.0**。

- `demucs` 仓库 license = **MIT**（GitHub API 实读原文：`Copyright (c) Meta Platforms, Inc. and affiliates.`；该仓库已归档）。
  ⚠️ **预训练权重无独立许可声明**（2026-09-14 读 README 原文：只有一句
  "Demucs is released under the MIT license"，通篇未提权重/条款），
  而权重训练自 **MUSDB18** —— 其官方页面写明
  **"provided for educational purposes only and the material contained in them should not be
  used for any commercial purpose without the express permission of the copyright holders"**，
  且 150 轨里 46 轨来自 MedleyDB（CC BY-NC-SA 4.0）、2 轨 CC BY-NC-SA 3.0。
  **净效果：权重不可视为干净的商用件**。本项目**不分发**它（`pip` 装的 demucs 运行时
  自行下载到用户机器）→ 现状合规；**将来也不要打包**。
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

### 2.3 "看着能打包、实际不能"的权重 —— 已机器化（红线 §4.1）

G5/G6 核完之后，两处"看起来是 MIT" 实际都带非商业约束：

| 权重 | 表面 | 实际 |
|---|---|---|
| demucs 预训练（htdemucs…） | 仓库 MIT | 权重无独立声明；训练数据 MUSDB18 = **仅教学/非商用** |
| RVC 底模（hubert / rmvpe / pretrained_v2） | HF 标签 `license: mit` | 同仓 `使用需遵守的协议-LICENSE.txt`：**"本软件仅供研究使用"** |

两者的共同形态是 **「标签是 MIT，条款不是」** —— 只看 HF/GitHub 的 license 标签一定会判错。
所以门禁不再依赖人记得：`tools/audit_licenses.py` 会按 `web/package.json` 的
electron-builder 配置（`files` + `extraResources`）推出**真实打包面**，
扫到模型权重后缀（`.pth` `.pt` `.ckpt` `.onnx` `.safetensors` `.bin` `.gguf` …）**直接判红**。

当前基线：打包面（`web/dist`、`web/electron`、`m2_server`、`tools`）**权重文件 0 个**。

> 取舍得说明白：`.zip` / `.7z` **不在**后缀表里。压缩包可能是合法素材，
> 误报会让这条规则像狼来了一样被无视 —— 宁可漏报也不误报，与 §1 的资产触发判据同一套取舍。

## 3 模型权重（运行时获取，不入仓库）

| 权重 | 许可 | 说明 |
|---|---|---|
| Qwen3-TTS 1.7B（`tts_models/`） | Apache-2.0 | 本机加载，不入库 |
| **音色市场 30 款精选** | 逐条 `license` 字段：HF 源 = "社区自训·仅供个人使用，勿商用"；魔搭源 = "未标注·仅供个人学习研究，勿商用" | 来源 `chaye741/RVC-Voice-Models`(HF, 13 款) / `hudddd/Retrieval-based-Voice`(魔搭, 17 款)。前端已展示提醒（`market_manifest.py`）。✅ **已回读模型卡（G4, 2026-09-14）**：两个上游**均未标注许可**（HF 无 `cardData`、tags 仅 `region:us`；魔搭 `Data.License`/`LicenseName`/`LicenseLink` 全空）→ 兜底文案就是正确表述。安装时自动回读并连同来路写入 `logs/<id>/source.json`，`/voices` 暴露、前端展示（`market_license.py`） |
| 袋鼠音色 / 自训模型（`logs/kangaroo_v2*`） | 项目自有，素材为**自录** | 与 §4.3 一致 |
| 未接入的选型候选 | 见 `docs/product/research/m1e_语音模型选型调研_2026-09-14.md` §4 | IndexTTS2 = bilibili Model Use License（免版税可用；**§3.4(c) 不得用于改进其他 AI 模型**） |

## 4 红线（违反即违规，改动前先看）

1. **不得把 NC / Research License 权重打进任何发行物**（F5-TTS 权重 CC-BY-NC-4.0、MaskGCT / Llasa / E2、OpenAudio S1-mini NC-SA、Spark-TTS BY-NC-SA、XTTS-v2 CPML…）。清单见 m1e 报告 §4。「代码 MIT」≠「权重能用」。
   **已机器化**（§2.3）：打包面出现权重后缀即红，且这条例外到"标签是 MIT"的情况 ——
   demucs 权重（训练自 MUSDB18，仅教学用）与 RVC 底模（条款"仅供研究使用"）都在禁列。
2. **内置 VB-CABLE 必须带 §2.2 的三行声明**；`A+B` / `C+D` / `Voicemeeter Potato` **永不内置**。
3. **第三方素材名与本体不入库**（issue #3）：具体标题只存在于本机**不入库**的 `.audit-materials.txt`。
4. **市场选品红线**：真人政治人物 / 真人网红名条目不上架（`market_manifest.py` 注释）。
5. **不得分发密钥**：`.env` / `certs/*.pfx` 已在 `.gitignore`（含私钥的签名证书）。

## 5 已知缺口（待修 —— 别当"已经解决"）

> 现状：**6 条全部了结** —— G1/G3 修复、G2 撤销、G4/G5/G6 核实（G4 结论与预期相反，见下）。

| # | 缺口 | 修法 |
|---|---|---|
| G1 | ~~OFL 字体许可原文未随安装包（§1）~~ | ✅ **已解决（2026-09-14）**：载荷 `web/public/licenses/` 随 `web/dist` 进 `app.asar` + `backend/web_dist` 两处；界面入口「设置 → 关于 → 开源许可」；`tools/sync_license_payload.py` 生成、`tools/audit_licenses.py` 核验（含版本 vs lockfile、字体依赖双向对齐）。**加字体只改 `package.json` 会在 CI 红** |
| G2 | ~~OpenMoji 署名未随分发（§1）~~ | ❌ **撤销 —— 这是条 phantom obligation**。核实（2026-09-14）：`git log --all --diff-filter=A -- '*openmoji*'` 为空、`market_imgs/` 31 个文件全为自制插画、无相关依赖、`find` 无图标文件。义务源自一句描述*意图*的注释，**没有产物**。已删掉该注释并改为资产触发判据（`ASSET_TRIGGERS` + `test_repo_has_no_phantom_openmoji_obligation`）：真引入图标那天自动要求署名 |
| G3 | ~~4 张**角色形象配图**为网络搜集（懒羊羊 / 曼波 / 孙悟空 / 派大星），**无授权链**~~ | ✅ **已解决（2026-09-14）**：全部替换为自生成原创卡通插画（`market_imgs/{lanyangyang,katoong_lanyangyang,katoong_manbo,sunwukong,paidaxing}`），并删除孤儿 `manbo.png`。需同步重推远程图库 `mouxu66/voice-market-assets` 清掉 CDN 旧图，客户端 TTL 6h 内拉新 |
| G4 | ~~市场条目许可只写兜底文案，未回读模型卡~~ | ✅ **已解决（2026-09-14）**，但**结论与预期相反**：回读实现+落盘已完成（`market_license.py` → 安装收尾写入 `source.json` → `/voices` 暴露 → 前端展示），而实测发现**两个上游都没标注许可** —— HF 源 tags 只有 `region:us`（无 `cardData`）、魔搭源 `Data.License`/`LicenseName`/`LicenseLink` 三个字段全是空串。所以兜底文案"仅供个人学习研究，勿商用"**不是占位符，而是当前法律状态下唯一正确的表述**（未标注 = 默认保留所有权利）。实现上强制区分 `unlabeled`（查过了，上游没写）/ `unreachable`（这次没查成）—— 两者后续动作不同，塌缩成一个值会让溯源文件开始撒谎 |
| G5 | ~~`demucs` 预训练权重许可未核~~ | ✅ **已核（2026-09-14）**：代码 MIT（原文 `Copyright (c) Meta Platforms, Inc. and affiliates.`）；**权重无独立声明**，且训练自 MUSDB18（官方写明"仅教学用途、未经版权方明示许可不得用于任何商业目的"）。结论 = **不得打进发行物**；本项目不分发 → 现状合规。门禁已覆盖（§2.3） |
| G6 | ~~RVC 整合包随附底模（`pretrained_v2` / `hubert_base`）未核~~ | ✅ **已核（2026-09-14）**：HF 标签 `license: mit`，但同仓 `使用需遵守的协议-LICENSE.txt` 正文写明 **"本软件仅供研究使用"**，且"不认可该条款则不能使用/引用软件包内所有代码和文件"。结论 = **不得打进发行物**；用户在 `D:\RVC` 自备 → 现状无分发义务。门禁已覆盖（§2.3）。附注：`hubert_base` 的架构是 `HubertModelWithFinalProj`，上游 `lengyue233/content-vec-best` 本身确为 MIT —— 是 **RVC 的分发版本**加了条款 |

> **四种失效方式，各配一层机器判据**（2026-09-14）：
> - G1 「**义务写了没做**」—— 文档说会附原文，实际没附 → 履行层**验产物**（读文件系统）。
> - G2 「**义务凭空要求**」—— 没有任何产物，却挂了个缺口要人去填 → 触发层**要求有来源**（扫资产）。
> - G4 「**缺口的前提是错的**」—— G4 假设"回读一下就能拿到许可名"，实测是"上游压根没标"。
>   于是"补上许可名"这个修法本身不成立 —— 实现要做的反而是**把"上游没标"记清楚**，
>   并区分"查过了没有"与"这次没查成"。**先验证缺口成不成立，再动手补。**
> - G5/G6 「**标签说有，条款说没有**」—— HF/GitHub 标 `license: mit`，另有中文/数据集条款
>   限制用途 → 夹带层**扫真实打包面**（推 electron-builder 配置 + 扫权重后缀）。
>
> 四者共通的教训：**别信声明，信产物**。手写断言、平台标签、README 的一句话、
> 甚至缺口表里自己写的那句前提，都会撒谎 —— 文件系统与条款原文不会。
> 判据宁可窄一点也别误报：一条开始喊狼来了的规则，下一次真出事时没人会看它。

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
npm:electron-log
npm:electron-store
npm:lucide-react
npm:@tanstack/react-query
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
