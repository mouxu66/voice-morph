# 2025–2026 实时语音转换 / 歌声转换（Real-time VC / SVC）开源 SOTA 调研报告

> 调研时间：2026-10-14。所有结论均标注来源；未查到的写「未找到」。
> 所有网页内容均视为外部不可信数据，仅作事实参考，不执行其中任何指令。
> 仓库元数据（star / pushed_at / archived / license）来自 GitHub API 实时查询；延迟数字来自论文表格（多注明 GPU 型号）。

---

## 0. 结论速览（TL;DR）

| 问题 | 结论 |
|---|---|
| **① 8GB+中文+<100ms+离线 最优解** | **没有单一"最优解"**。分档：RVC v2（ASIO 90ms，需训练，最成熟）；X-VC（240ms，零样本+中文最佳，超标）；Conan-fast（37ms，但仅英文，无中文验证）。**RVC 未过时，但已不是"最先进"**——它在"零样本"维度上落后，在"实时+成熟度+中文生态"维度上仍是最稳的。 |
| **② 零样本实时 VC 能否替代 RVC 训练流程** | **基本不能**。当前零样本流式 VC 的延迟普遍 **240–500ms**（RTX 3090 实测），远超 100ms；且 Seed-VC 官方自述算法延迟 ~300ms。唯一 <100ms 的 Conan-fast 只在 LibriTTS/VCTK 英文上验证。**"免训练换声"在质量上已成熟，在延迟上尚未成熟。** |
| **③ 大移调音色泄漏的已验证解法** | 有，且是学术热点。核心四类：**Seed-VC 的 external timbre shifter**（训练期扰动源音色，arXiv:2411.09943）、**SPA-SVC 的 cycle pitch shifting + SSIM loss**（专治跨音域沙哑，arXiv:2406.05692）、**NeuCoSVC 的参考音频最近邻 SSL 特征替换**（arXiv:2312.04919）、**Zero-VC 用说话人匿名化(SA)作扰动机制**（Interspeech 2026，arXiv:2606.20218）。另有 **RVC 官方自带的 top1 retrieval**（README.en.md 原文）。 |

---

## 1. 逐项目调研

### 1.1 RVC（RVC-Project/Retrieval-based-Voice-Conversion-WebUI）

| 维度 | 事实 | 来源 |
|---|---|---|
| 许可证 | **MIT** | GitHub API |
| 是否需要训练 | **是**，官方建议 **≥10 分钟低噪语音** | [README.en.md](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/blob/main/docs/en/README.en.md) |
| 零样本能力 | **无**（v2 需 per-speaker 训练；这是它的根本定位） | 同上 |
| 中文支持 | **强**（中文社区主力，UI 有中文，语料生态最大） | 同上 |
| 8GB 显存 | **可行**，README 原文 "Easy + fast training, even on poor graphics cards" | 同上 |
| 实时性 | **官方自述端到端 170ms；ASIO 输入输出下 90ms**，"但非常依赖硬件" | 同上（GitHub README 原文） |
| 活跃度 | star **38,731**；`pushed_at` **2026-08-04**；`updated_at` 2026-10-14；fork 5,304；open issues 584；**未 archived** | GitHub API |

**2025–2026 后继版本 / 已知上限：**

- **RVCv3**：README.en.md 原文 "Please look forward to the pretrained base model of **RVCv3**, which has larger parameters, more training data, better results, unchanged inference speed, and **requires less training data for training**." → **官方已预告但截至本次调研未见发布版/预训练基座释出（未找到可下载的 RVCv3 基座模型）。**
- **已知上限（重要）**：README 明确列出 "Reduce tone leakage by replacing the source feature to training-set feature using **top1 retrieval**" —— 即 RVC 的抗音色泄漏机制依赖检索，而非解耦。
- 上游 `svc-develop-team/so-vits-svc` 已 archived（见 1.2），RVC 事实上是这条 VITS 血脉唯一活跃主线。
- RVC 的 fork 生态极活跃（GitHub forks 页可见 2026-07/09 新建的 `RVC-Realtime-GUI` 等实时 GUI fork）。

**判断**：RVC 是一个"维护放缓但未停止、生态极成熟"的项目。它没有被后继版本取代，但**学术界的主线已转向零样本 + codec latent space**（见 §3）。

---

### 1.2 so-vits-svc 5.0 / 4.1（svc-develop-team）

| 维度 | 事实 | 来源 |
|---|---|---|
| 许可证 | **AGPL-3.0**（注意：与 RVC 的 MIT 不同，商用合规更严） | GitHub API |
| 状态 | **`archived: true`** —— 官方已停止维护并归档 | GitHub API |
| 最后推送 | **2023-11-11** | GitHub API |
| star / fork | 28,077 / 5,016 | GitHub API |
| 默认分支 | `4.1-Stable` | GitHub API |
| 是否需要训练 / 零样本 | 需训练 / 无零样本 | 项目定位 |
| 中文支持 | 强（中文社区起源，有 `README_zh_CN.md`） | [Gitee 镜像](https://gitee.com/tizi8/so-vits-svc/blob/4.1-Latest/README_zh_CN.md) |
| 实时性 | 官方版非实时；社区 fork `voicepaw/so-vits-svc-fork`（⭐9,334）"with realtime support" | GitHub |

**未找到**：so-vits-svc 的 "5.0" 正式发布。svc-develop-team 主仓最后停留在 **4.1-Stable**。RVC-Boss/sovits 是 RVC 作者对 4.0 的 fork（⭐58，BSD-3-Clause，`pushed_at` 2023-05-17），亦非 5.0。

**与 RVC 对比结论**：社区评价（中文技术博客）认为 so-vits-svc 是"2023 上半年最火"，"现在已经被 RVC 取代了。RVC 效果更好、训练更快"（[zxdplay/CNBlogs, 2026-01](https://www.cnblogs.com/zxdplay/p/19433775)）。**结论：so-vits-svc 已死，不要作为新选型。**

---

### 1.3 Seed-VC v2 / v3（Plachtaa）

| 维度 | 事实 | 来源 |
|---|---|---|
| 许可证 | **GPL-3.0** | GitHub API |
| 论文 | **arXiv:2411.09943**《Zero-shot Voice Conversion with Diffusion Transformers》 | [arXiv](https://arxiv.org/abs/2411.09943) |
| 仓库状态 | **`archived: true`**（官方已归档） | GitHub API |
| 最后推送 | **2025-04-20** | GitHub API |
| star / fork | 3,895 / 561 | GitHub API |
| 零样本 | **是**，"1~30 秒参考音即可克隆，无需任何训练" | GitHub README |
| 中文支持 | **间接支持**：Seed-VC 论文用 Seed-TTS-Eval 评测（含 test-zh）；X-VC 论文把 Seed-VC_tiny 作为中文流式基线并报告 test-zh 指标（WER 3.36 / SIM 0.60） | [X-VC 论文 §5.1](https://arxiv.org/html/2604.12456v3) |
| 实时性 | **官方自述：算法延迟 ~300ms + 设备侧 ~100ms**，"suitable for online meetings, gaming and live streaming" | GitHub README |
| 8GB 显存 | 未找到官方显存声明。论文/评测口径下 Seed-VC_tiny 总参数 **262M**（converter 仅 27M） | X-VC 论文 Table 2 |
| 歌声转换 | **支持**（2024-10-24 更新 44kHz SVC 模型；2024-10-25 "Added exhaustive evaluation results and **comparisons with RVCv2 for singing voice conversion**"） | [commit 3f719fe](https://github.com/sheiy/seed-vc/commit/3f719fe3de6d01261e18d13bf2cdc7e82da99a86) |

**关于 "v2 / v3" 版本号**：仓库有 v1/v2 两代模型架构（[DeepWiki: V2 Model Architecture](https://deepwiki.com/Plachtaa/seed-vc/2.2-v2-model-architecture)）。**未找到 Seed-VC v3**。由于仓库已 archived，后续演进实际上由 REF-VC、X-VC 等后继工作承接（见 §3）。

**对本项目的意义**：Seed-VC 论文的核心贡献 **external timbre shifter（训练期外部音色扰动器）** 就是直接针对 "timbre leakage" 设计的，且论文明确点出三大障碍：**timbre leakage / insufficient timbre representation / training-inference mismatch**。这与本项目实测的"跨音色 CAM++ 0.78、大移调泄漏 0.2557"完全同源 → 见 §5 问题③。

---

### 1.4 kNN-VC（Google / Ubisoft La Forge，零样本、检索式、免训练）

| 维度 | 事实 | 来源 |
|---|---|---|
| 论文 | **arXiv:2111.02392**《A Comparison of Discrete and Soft Speech Units for Improved Voice Conversion》（ICASSP 2022） | [arXiv](https://arxiv.org/abs/2111.02392) |
| 官方代码 | `bshall/soft-vc`（论文摘要 "Code available at https://github.com/bshall/soft-vc/"） | 同上 |
| 许可证 | 未找到明确 LICENSE 声明（仓库未见 SPDX；论文未声明） → **标注"未找到"** | — |
| 是否需要训练 | **不需要**（检索式：对参考语音的 WavLM 特征做 k 近邻匹配 + 加权求和，直接合成） | 论文摘要 |
| 零样本 | **是**（架构本身即零样本） | 同上 |
| 中文支持 | 未找到中文验证（原工作在英语 VCTK 类设定） | — |
| 实时性 | 未找到官方实时延迟数字。检索开销随参考集增大而增；社区实现多为离线 | — |
| 已知缺陷（关键） | kNN-SVC 论文原文：**"kNN-VC's core representation, WavLM, lacks harmonic emphasis, resulting in dull sounds and ringing artifacts"**（声音发闷 + 振铃伪影） | [kNN-SVC, arXiv:2504.05686](https://arxiv.org/abs/2504.05686) |

**★ 与用户痛点的直接对应**：用户实测"频谱闷（4-8kHz 掉 18.5dB）"。kNN-VC 的"闷"是 WavLM 表征缺谐波强调导致的**架构性缺陷**，而 kNN-SVC 给出的补法是 **additive synthesis（加法合成）**：利用 WavLM、pitch contour、spectrogram 之间的双射关系做加法合成，把谐波波形重新注入。**这是用户"频谱闷"问题在文献中最直接的对应解法。**

---

### 1.5 FreeVC / FreeVC-24、VEC-Tok / Vec-Tok+、LVC-VC、NaturalSpeech 系列

| 项目 | 许可证 | 训练需求 | 零样本 | 中文 | 8GB | 实时性 | 活跃度 / 来源 |
|---|---|---|---|---|---|---|---|
| **FreeVC** | 未找到明确 LICENSE | 需训练基座 | **是**（one-shot） | 未验证 | 未找到 | 非流式（离线） | arXiv:2210.15418，ICASSP 2023 |
| **FreeVC-24** | 未找到 | 需训练 | 是 | 未验证 | 未找到 | 未找到 | **未找到可靠一手来源**（本次检索未命中 2024 版 FreeVC-24 的正式论文/仓库，不列具体结论） |
| **Vec-Tok Speech** | 未找到明确 LICENSE | 需训练（50k 小时预训练） | 是（跨语言零样本 VC） | 未验证 | 未找到（50k 小时预训练，推理侧未找到显存数据） | 非流式 | arXiv:2310.07246；代码 `BakerBunker/VecTok` |
| **Vec-Tok-VC+** | 未找到 | 需训练 | **是，仅需 3s prompt** | 未验证 | 未找到 | 非流式 | [arXiv:2406.09844](https://arxiv.org/abs/2406.09844)，Interspeech 2024 |
| **LVC-VC** | — | — | — | — | — | — | **未找到**：本次未检索到名为 "LVC-VC" 的独立可靠项目。相关方向由 StreamVC / DualVC / Conan 等承载（见 §3） |
| **NaturalSpeech 系列**（1/2/3） | 学术代码，未找到宽松开源许可（NaturalSpeech 3 为 Meta 学术发布） | 需训练 | NaturalSpeech 3 是零样本 **TTS**（非 VC） | 未验证 | 8GB 不可行（大规模） | 非实时 | NaturalSpeech 3: ICML 2024，arXiv:2405.14283（经 X-VC 参考文献确认） |

> 说明：NaturalSpeech 系列是 **TTS 而非 VC**，与本项目"语音转语音"需求不同路，列此仅供排除。

---

### 1.6 OpenVoice / OpenVoice V2（MyShell）

| 维度 | 事实 | 来源 |
|---|---|---|
| 许可证 | **MIT，免费商用**（官方原文："Starting from April 2024, both V2 and V1 are released under MIT License. Free for commercial use."） | [docs.myshell.ai/technology/openvoice](https://docs.myshell.ai/technology/openvoice) |
| 论文 | **arXiv:2312.01479**《OpenVoice: Versatile Instant Voice Cloning》 | [arXiv](https://arxiv.org/abs/2312.01479) |
| V2 发布时间 | **2024 年 4 月**；原生支持 **English / Spanish / French / Chinese / Japanese / Korean** | 官方文档 |
| 是否需要训练 | **不需要**（即时克隆，短参考音频） | 官方文档 |
| 中文支持 | **是，原生** | 官方文档 |
| 8GB 显存 | 未找到官方声明 | — |
| 实时性 | **未找到官方延迟数字** | — |
| 活跃度 | star 37,878（GitHub platform_search）；官方文档页 "Last updated 1 year ago" | GitHub / 官方文档 |

**★ 关键定位提醒**：OpenVoice 是 **TTS（文本→语音）路线**，不是 VC（语音→语音）。它需要**文本输入**才能工作。对于"实时变声"（说话实时换声、无 ASR 转录）场景，**OpenVoice 不适用**——必须先做 ASR 才能驱动它，这会引入额外延迟与错误。Seed-VC 论文也把 OpenVoice 列为 VC 基线并在 speaker similarity / WER 上超越它（arXiv:2411.09943 摘要）。

---

### 1.7 kiki-vc / WhisperVC / VoiceLDM / DiffVC 等零样本路线

| 项目 | 结论 | 来源 |
|---|---|---|
| **kiki-vc** | **未找到**。本次多渠道检索（web / github platform / arXiv API）均未命中名为 "kiki-vc" 的可靠开源 VC 项目。搜到的 "KiKi" 均为无关结果（腾讯云 KiKi Agent、许佳琪）。**不列入对比，建议视为不存在或极冷门。** | — |
| **WhisperVC** | 存在但**不是通用零样本 VC**：它是**中文耳语→正常语音转换**（whisper-to-speech，AISHELL6-Whisper 数据集），用途是耳语重建，不是换声 | [arXiv:2511.01056](https://arxiv.org/html/2511.01056v1)（v1/v2） |
| Whisper 特征用于 VC | 有：LaVoco（ICML 2026 ML4Audio workshop）用 **dual Whisper-XCodec2** 作自回归零样本 VC 的内容表征 | [CameraReady PDF](https://mlforaudioworkshop.github.io/accepted_submissions_2026/CameraReadies%204-83/67/CameraReady/Voice_Conversion__ML4Audio___ICML_26_%20%281%29.pdf) |
| **VoiceLDM** | **未找到**可靠一手来源（本次未命中） | — |
| **DiffVC** | **未找到**独立项目页；扩散式 VC 的思想已由 Seed-VC / REF-VC / MeanVC 2 等继承并发扬 | — |
| **FreeVC** | 见 §1.5 | arXiv:2210.15418 |
| **YourTTS** | 零样本多说话人 TTS + 零样本 VC，PMLR v162 | X-VC 参考文献 |
| **VQMIVC / QuickVC / BNE-PPG-VC / Diff-VCTK** | 均为 Conan 的离线基线（VQMIVC SIM 62.54%，BNE-PPG-VC 79.06%，Diff-VCTK 81.47%，QuickVC 77.39%） | [Conan Table I](https://arxiv.org/html/2507.14534v4) |

---

## 2. 实时延迟基线（社区现在做到多少？<50ms 有戏吗？）

这是本报告最硬的一组数据。**以下均为论文实测，注明 GPU**：

### 2.1 零样本流式 VC 横向对比（单卡 RTX 3090 实测）

来自 X-VC 论文 Table 2（[arXiv:2604.12456v3](https://arxiv.org/html/2604.12456v3)），流式设定：

| 模型 | 总参数 | Converter 参数 | 中文 WER% ↓ | 中文 SIM ↑ | 中文 UTMOS ↑ | 模型延迟 T_model ↓ | 计算延迟 T_compute ↓ |
|---|---|---|---|---|---|---|---|
| Seed-VC_tiny | 262M | 27M | 3.36 | 0.60 | 2.46 | **380 ms** | 120.75 ms |
| MeanVC | 47M | 14M | 4.89 | 0.72 | 2.22 | **250 ms** | 32.12 ms |
| **X-VC** | — | — | **（流式 WER 中英双最佳）** | — | — | **240 ms** | （120 current + 20 overlap + 100 future） |

X-VC 论文原文：*"X-VC has a model-induced latency T_model of 240 ms, with 120 ms for T_current, 20 ms for T_overlap, and 100 ms for T_future."*
X-VC 离线 RTF = **0.014**（远低于所有基线）。

### 2.2 Conan：目前**唯一**有 <50ms 实证的零样本流式 VC

来自 Conan 论文 Table II（单卡 **A100 80GB**）：

| 设定 | Chunk size | RTF | **总延迟** |
|---|---|---|---|
| **Conan (Fast)** | **20 ms**，3 层 Emformer，无右上下文（严格因果） | 0.74 | **36.87 ms** |
| Conan (Full) | 80 ms，6 层 Emformer + 2 右上下文块 | 0.25 | **139.71 ms** |

分模块延迟（Fast）：Content 2.76ms / Main 7.82ms / Vocoder 6.29ms。
论文原文：*"Conan can achieve a latency as low as **37 ms** for the conversion on a single A100 GPU without any engineering optimizations."*

**但 Fast 设定的质量代价明确**：WER 从 6.02% 恶化到 **10.72%**，SIM 从 85.71% 掉到 78.26%。且**全部实验基于 LibriTTS + VCTK 英语数据，未验证中文**。

### 2.3 其他延迟锚点

| 系统 | 延迟 | 条件 | 来源 |
|---|---|---|---|
| **RVC v2** | **端到端 170ms**；ASIO 下 **90ms** | 官方 README，"非常依赖硬件" | RVC README |
| **Seed-VC（官方）** | **算法 ~300ms + 设备侧 ~100ms** | 官方 README 自述 | Plachtaa/seed-vc README |
| **SynthVC** | **端到端 77.1 ms** | NCMMSC2025，合成数据训练的 codec 流式 VC | [arXiv:2510.09245](https://arxiv.org/abs/2510.09245) |
| **RT-VC** | **CPU 延迟 61.4 ms**（比 SOTA 降 13.3%） | ACL 2025 Demo；articulatory 特征 + DDSP | [arXiv:2506.10289](https://arxiv.org/abs/2506.10289) |
| **TVTSyn** | **<80 ms GPU 延迟** | 流式 VC + 匿名化，Interspeech 2026 | [arXiv:2602.09389](https://arxiv.org/abs/2602.09389) |
| **Stream-Voice-Anon** | 180 ms（vs DarkStream 200ms） | ICASSP 2026 | [arXiv:2601.13948](https://arxiv.org/abs/2601.13948) |
| **PHONOS** | ≤241 ms 端到端（单 GPU） | SLT 2026 | [arXiv:2603.27001](https://arxiv.org/abs/2603.27001) |
| **LLVC** | **<20 ms**（16kHz，消费级 CPU，快于实时 2.8×） | **但为 any-to-one（需 per-target 训练），非零样本** | [arXiv:2311.00873](https://arxiv.org/abs/2311.00873) |
| **MeanVC 2** | 从 211ms 降到 **110 ms**（40ms chunk + 有界未来上下文） | Interspeech 2026 | [arXiv:2606.09050](https://arxiv.org/abs/2606.09050) |

### 2.4 回答：社区普遍延迟 & <50ms 是否有戏

- **社区普遍水平**：零样本流式 VC **≈ 110–500ms**；"接近实时可用"的门槛大致在 **<150ms**；**<100ms 目前只有少数方案**（RVC+ASIO 90ms、SynthVC 77ms、RT-VC 61ms、Conan-fast 37ms）。
- **<50ms 有戏，但都有硬前提**：
  - **Conan-fast 37ms** —— 唯一零样本 <50ms，但仅英文、WER 翻倍、需 A100 级算力（8GB 卡上 RTF 0.74 会直接不达标）。
  - **LLVC <20ms** —— 最快，但 **any-to-one，必须训练**，不满足零样本。
  - **RT-VC 61ms** —— 已在 CPU 上做到，但 61ms 仍略高于 50ms，且为 articulatory 特征专用路线。
- **结论：8GB 显存 + 中文 + <50ms + 零样本，四者同时满足的方案——未找到。** 这是本报告最重要的否定性结论。

---

## 3. 歌声转换（SVC）SOTA：2025–2026 开源最好的是什么？

### 3.1 格局总览（按"是否需要训练目标音色"分两大类）

**A. 需要训练目标音色（传统 SVC，本项目当前路线）**
- **so-vits-svc 4.1**：已 archived（2023-11），AGPL-3.0。
- **RVC v2**：仍活跃，MIT，生态最大。
- **DiffSinger / NNSVS / OpenUtau**：**本次未做深度调研，未找到** 2025-2026 与零样本 SVC 的直接对比基准数据 → 如实标注「未找到」。它们属于"歌声合成/半监督"路线，与 VC 任务定位不同。

**B. 零样本 SVC（2024–2026 爆发，学术 SOTA 所在）**

| 工作 | 时间 / 会议 | 核心招数 | 许可证/代码 | 来源 |
|---|---|---|---|---|
| **Seed-VC** | 2024-11 | diffusion transformer + **external timbre shifter** + F0 conditioning 扩展到 SVC | GPL-3.0（仓库已 archived） | [arXiv:2411.09943](https://arxiv.org/abs/2411.09943) |
| **SaMoye** | 2024-07 | 首个开源高质量零样本 SVC；内容/音色/音高解耦 + ASR 压缩内容特征减泄漏；**1815 小时纯歌声 / 6367 歌手**；可把歌声转成**动物等非人声** | 代码权重全公开 | [arXiv:2407.07728](https://arxiv.org/abs/2407.07728) |
| **FreeSVC** | 2025-01，ICASSP 2025 | SPIN 说话人不变聚类 + ECAPA2 + 可训练语言嵌入 → **多语言零样本 SVC** | 代码模型公开 | [arXiv:2501.05586](https://arxiv.org/abs/2501.05586) |
| **kNN-SVC** | 2025-04，ICASSP 2025 | 把 kNN-VC 搬到 SVC；**加法合成补谐波** + 拼接平滑度优化 | [SmoothKen/knn-svc](https://github.com/SmoothKen/knn-svc)，demo knnsvc.com | [arXiv:2504.05686](https://arxiv.org/abs/2504.05686) |
| **DAFMSVC** | 2025-08，Interspeech 2025 | 用目标音频中最相似的 SSL 特征**替换**源 SSL 特征以阻断音色泄漏 + flow matching | — | [arXiv:2508.05978](https://arxiv.org/abs/2508.05978) |
| **HQ-SVC** | 2025-11，**AAAI 2026 主会** | 解耦 codec 联合提取内容+说话人；pitch & volume 建模；可微分信号处理 + 扩散；**低资源场景** | — | [arXiv:2511.08496](https://arxiv.org/abs/2511.08496) |
| **R2-SVC** | 2025-10 | **随机 F0 扰动**鲁棒增强 + 分离伪影仿真 + NSF 神经源滤波模型 | — | [arXiv:2510.20677](https://arxiv.org/abs/2510.20677) |
| **REF-VC** | 2025-08 | Seed-VC 后继：random erasing + E2TTS 隐式对齐 + Shortcut Model 把 flow matching 压到 **4 步**；**一个模型兼容 SVC** | — | [arXiv:2508.04996](https://arxiv.org/abs/2508.04996) |
| **YingMusic-SVC** | 2025-12 | **singing-trained RVC timbre shifter** + F0-aware timbre adaptor + Flow-GRPO 强化学习 | — | [arXiv:2512.04793](https://arxiv.org/abs/2512.04793) |
| **Poly-SVC** | 2026-05，ICASSP 2026 | **CQT 音高提取器**保留主旋律 + 残留和声；CFM 扩散解码；零样本跨语言 | — | [arXiv:2605.12310](https://arxiv.org/abs/2605.12310) |
| **S²Voice** | 2026-01，ICASSP 2026 | **SVCC 2025 双赛道冠军**（in-domain + zero-shot singing style conversion）；基于 Vevo 两阶段 | demo 页 | [arXiv:2601.13629](https://arxiv.org/abs/2601.13629) |
| **CartoonSing** | 2025-11 | **非人声歌唱生成（NHSG/NHSVC）**，人类+非人类音色统一 | — | [arXiv:2511.21045](https://arxiv.org/abs/2511.21045) |

### 3.2 SVCC 2025 挑战赛权威结论（最重要的一手判断依据）

来自 SVCC 2025 官方分析论文（提交至 IEEE TASLP，[arXiv:2509.15629](https://arxiv.org/abs/2509.15629)）：

- 共评测 **33 个系统**。
- **"top systems had comparable singer identity scores to ground truth samples"** —— 头部系统的歌手身份相似度已**接近真值**。
- **但**："modeling the singing style and consequently achieving high naturalness **still remains a challenge**"，难点在 breathy（气声）、glissando（滑音）、vibrato（颤音）等动态风格的建模。
- 客观指标中：**chroma-alignment（依赖型）与 speaker embedding non-match（非匹配型）与主观分最相关**，但仍不能替代主观听测。

### 3.3 回答"零样本歌声转换是否已可行？"

**部分可行，但有明确边界：**

- **可行**：歌手音色相似度层面，头部零样本 SVC 已达"接近真值"（SVCC 2025）。这意味着**"用现成件拼翻唱、不训歌声模型"在音色维度上是成立的**——与项目"不训歌声模型"的约束不冲突。
- **不可行/尚不成熟**：
  1. **风格（气声/滑音/颤音）迁移仍是公开难题**（SVCC 2025 结论）；
  2. **带伴奏/和声的真实歌曲鲁棒性差** —— YingMusic-SVC 论文原文："existing zero-shot SVC systems remain fragile in real songs due to **harmony interference, F0 errors**, and the lack of inductive biases for singing"（[arXiv:2512.04793](https://arxiv.org/abs/2512.04793)）；
  3. **实时性完全未解决**：上述所有零样本 SVC 均为离线，未找到任何一个支持 <100ms 流式的零样本 SVC。

---

## 4. 2026 年「8GB 显存 + 中文 + 实时(<100ms) + 离线」社区最优解排序

### 4.1 候选方案排序（按对本项目的适配度）

| 排名 | 方案 | 延迟 | 中文 | 8GB | 需训练 | 零样本 | 成熟度 | 综合判断 |
|---|---|---|---|---|---|---|---|---|
| **1** | **RVC v2（保持现状，精细化）** | 170ms；ASIO **90ms** | ✅ 强 | ✅ | ✅ 需 | ❌ | ⭐⭐⭐⭐⭐ 生态最大 | **继续用，但需修补法（见 §5）** |
| **2** | **X-VC**（SJTU，代码+checkpoints 已开源） | **240ms**（超 100ms） | ✅ **流式 WER 中英双最佳** | 未找到显存数据（converter 为轻量 one-step，RTF 0.014） | 基座需训练 | ✅ | ⭐⭐⭐ 论文新（2026-04） | **中文零样本当前最强，但延迟翻倍，需本地实测** |
| **3** | **SynthVC** | **77.1ms** | NCMMSC2025（中文会议）→ 中文友好 | 未找到 | 基座需训练 | 合成数据训练 | ⭐⭐ | 延迟达标的流式方案，但零样本属性弱 |
| **4** | **MeanVC 2** | **110ms**（40ms chunk） | 内容编码器**仅支持中文**（X-VC 论文注明） | ✅ 47M 总参 | 需训练 | ✅ | ⭐⭐⭐ Interspeech 2026 | 轻量，但略超 100ms，且仅中文源 |
| **5** | **Conan-fast** | **37ms** | ❌ 仅英文验证 | ❌ 需 A100 级 | 需训练 | ✅ | ⭐⭐⭐ ASRU 2025 | 延迟唯一 <50ms，但中文/8GB 双不达标 |
| **6** | Seed-VC | ~400ms | 间接 | 262M | 免 | ✅ | ⭐⭐⭐ 已 archived | **延迟严重超标，不适合实时** |
| **7** | LLVC | <20ms | 未验证 | ✅ 消费级 CPU | ✅ 需 | ❌ any-to-one | ⭐⭐ | 最快但非零样本，与需求错位 |

### 4.2 直接回答 a)「RVC 是否已经过时？」

**RVC 没有过时，但它已经从"唯一解"退化为"一个维度上的最优解"。**

**它不过时的理由（硬事实）：**
1. **它是唯一同时满足 8GB + 中文 + <100ms + 离线 四个约束的成熟方案**（ASIO 90ms）。所有零样本竞品在延迟上都输给它 2–5 倍。
2. MIT 许可，商用无碍（对比 so-vits-svc 的 AGPL-3.0、Seed-VC 的 GPL-3.0）。
3. 生态规模 38.7k star / 5.3k fork，2026-08 仍有推送，未 archived。
4. 官方 RVCv3 已在路线图上（更少训练数据、更好效果、推理速度不变）——**但截至本次调研未找到已发布的 RVCv3 基座模型**。

**它确实落后的地方（必须正视）：**
1. **零样本维度完全空白**：RVC 每个目标音色都要训一个模型，这是它 vs Seed-VC/X-VC 的代差。
2. **抗音色泄漏机制是"检索式（top1 retrieval）"而非"解耦式"** —— 这正是用户实测"跨音色 0.78、大移调泄漏 0.2557"的根因所在（见 §5）。
3. 学术界主线已迁移到 **codec latent space + 一步转换**（X-VC RTF 0.014），RVC 的 VITS/HiFiGAN 架构已属上一代。

**给项目的选型建议：双轨制**
- **实时变声主线：保留 RVC v2**，但用 §5 的方法补音色泄漏与频谱闷。
- **表达力/翻唱备选：从 Seed-VC 迁到 X-VC 或 REF-VC**（Seed-VC 仓库已 archived，不建议新接入）。
- **不要引入 so-vits-svc**（已死）与 OpenVoice（TTS 路线，实时变声不适用）。

---

## 5. 回答 b)「零样本实时 VC 是否已成熟到可替代 RVC 训练流程？」

**结论：质量上已成熟，延迟上尚未成熟。现在替代 RVC 会是一次延迟回退（100ms → 240~500ms）。**

### 5.1 支持"可以替代"的证据
- Seed-VC 论文：1~30 秒参考音、零训练，且**在 speaker similarity 与 WER 上超越 OpenVoice 与 CosyVoice**（arXiv:2411.09943）。
- X-VC：零样本流式，**中英双语流式 WER 最佳**，RTF 0.014，代码+checkpoints 已开源（[github.com/Jerrister/X-VC](https://github.com/Jerrister/X-VC)）。
- SVCC 2025：头部零样本 SVC 歌手相似度**接近真值**。
- 生态已就绪：Seed-VC 有多个实时服务封装（`Leroll/fast-vc-service` WebSocket PCM/Opus、`Dox45/Seed-vc-webapi`、ComfyUI 插件）。

### 5.2 反对"现在替代"的证据（更硬）
- **延迟墙**：X-VC 240ms、MeanVC 250ms、Seed-VC_tiny 380ms（RTX 3090 实测）。唯一 <100ms 的 Conan-fast 37ms 是英文专用且 WER 翻倍。
- **Seed-VC 官方自己承认**算法延迟 ~300ms + 设备侧 ~100ms —— 对"实时变声"（用户对着麦克风说话要即时听到）是明显可感知的滞后。
- **中文验证不足**：X-VC 有中文数据，但 Conan 完全没有；大多数零样本流式工作只验证英语。
- **Seed-VC 仓库已 archived**（2025-04 后停更），GPL-3.0 许可对闭源桌面产品有传染性 —— 这对一个要打包分发的变声工具是**实质法律风险**（对比 RVC 的 MIT）。

### 5.3 务实路线建议
1. **短期（现在）**：保留 RVC v2 作为实时主线；把零样本（X-VC / REF-VC）作为**离线翻唱与"新音色试听"**的备选，不进入实时链路。
2. **中期（跟踪）**：盯 **X-VC**（中文流式 SOTA、MIT/许可待确认、轻量 one-step）与 **MeanVC 2**（110ms，最接近 100ms 门槛的零样本流式）。二者是"零样本实时化"最可能先突破 100ms 的候选。
3. **触发条件**：当某个零样本流式方案在**中文 + 8GB 实测 <100ms** 且有可下载 checkpoint 时，再启动替代。

---

## 6. 回答 c)「跨音域/大移调时音色泄漏」的已验证解法

这是用户痛点（±12 半音时对原唱相似度 0.2557）的核心。**社区已有大量针对性工作，按"可直接借鉴度"排序：**

### 6.1 Seed-VC 的 external timbre shifter（最根本，架构级）
- **机制**：训练期用一个**外部音色扰动器（timbre shifter）**主动扰动源语音的音色，** mitigat[e] timbre leakage**，同时**让训练任务与推理任务对齐**（训练-推理不一致是零样本 VC 三大障碍之一）。
- **为什么对症**：RVC 的泄漏源于"内容表征里残留说话人信息 + 训练/推理音色域不一致"。timbre shifter 同时打这两点。
- 来源：[arXiv:2411.09943](https://arxiv.org/abs/2411.09943)（Seed-VC 论文摘要原文即此）
- **可借鉴度**：⭐ 需要重训基座，无法直接套在已有 RVC 模型上。但 **YingMusic-SVC 已经做出 "singing-trained RVC timbre shifter"**（[arXiv:2512.04793](https://arxiv.org/abs/2512.04793)）→ **已有"基于 RVC 的音色扰动器"实现先例**，这是本项目最值得跟踪的一条。

### 6.2 SPA-SVC：cycle pitch shifting + SSIM loss（专治跨音域沙哑/劣化）
- **问题定义与用户痛点逐字对应**：论文原文 "in **cross-domain SVC scenarios, where there is a significant disparity in pitch** between the source and target voice domains, the models tend to generate audios with **hoarseness**"（跨音域时生成沙哑声）。
- **解法**：**cycle pitch shifting 训练策略 + SSIM（结构相似性）损失**，"without requiring additional data or increasing model parameters"（不加数据不加参数），在 M4Singer 上验证，**跨音域场景提升尤其显著**。
- 来源：[arXiv:2406.05692](https://arxiv.org/abs/2406.05692)，Interspeech 2024
- **可借鉴度**：⭐⭐ **本项目最直接可复现的一个**。cycle pitch shifting 是纯数据增强策略，可在现有 SVC/RVC 训练流程中直接加入，无需改架构。

### 6.3 R2-SVC：随机 F0 扰动鲁棒化 + NSF 源滤波
- **机制**：① **随机 F0 扰动（random F0 perturbations）**做仿真鲁棒增强；② 音乐分离伪影仿真（混响/回声）；③ **Neural Source-Filter (NSF) 模型**显式表示谐波与噪声成分，提升自然度与可控性。
- **NSF 对"频谱闷"特别相关**：显式建模谐波=显式补高频谐波结构。
- 来源：[arXiv:2510.20677](https://arxiv.org/abs/2510.20677)
- **可借鉴度**：⭐⭐⭐ F0 扰动是训练技巧；NSF 是架构改动（RVC 生态已有 NSF-HiFiGAN 成熟件，见 SingNet arXiv:2505.09325 开源的 NSF-HiFiGAN 预训练模型）。

### 6.4 NeuCoSVC：参考音频最近邻 SSL 特征替换（根治式）
- **机制**：不用"解耦出说话人 embedding"这种易泄漏路线，而是**推理时直接把源 SSL 特征替换为匹配池中来自参考音频的最近邻 SSL 特征**，同时保留源的原始谐波信号与响度。论文称这"effectively resolves the timbre leakage issue caused by previous disentanglement-based approaches"。
- **效果**：在 intra-language / cross-language / **cross-domain** 三类评测中均超越解耦式 speaker embedding 方案。
- 来源：[arXiv:2312.04919](https://arxiv.org/abs/2312.04919)
- **可借鉴度**：⭐⭐ 需要改推理流程，但思路与 kNN-VC / kNN-SVC 同源（检索替换），工程上比训练新模型轻。

### 6.5 DAFMSVC：目标侧 SSL 特征替换阻断泄漏
- **机制**：把源音频的 SSL 特征**替换为目标音频中最相似的 SSL 特征**，"to prevent timbre leakage" + 双交叉注意力融合 speaker embedding / 旋律 / 内容 + flow matching。
- 来源：[arXiv:2508.05978](https://arxiv.org/abs/2508.05978)，Interspeech 2025
- **可借鉴度**：⭐⭐ 与 6.4 同族，Interspeech 2025 更新版本。

### 6.6 Zero-VC：用说话人匿名化（SA）作为扰动机制（Interspeech 2026 最新）
- **洞察（重要，直接解释用户的困境）**：论文指出信息瓶颈（IB）类方法虽能滤除音色，但**会连带丢弃韵律（prosody）**，于是模型不得不显式注入 F0 等特征，而**"这往往需要缓冲未来帧，造成算法前瞻延迟"**。这正是"要音色纯净"与"要低延迟 + 要韵律"之间的三角冲突。
- **解法**：发现 Speaker Anonymization 的目标天然契合"平衡音色泄漏与效用保留"，于是**把 SA 作为一种扰动机制**显式抑制音色泄漏，同时保留韵律效用；且 SA 的鲁棒表征**显著降低生成器对未来上下文的依赖，从而实现严格因果、零前瞻网络**。
- 来源：[arXiv:2606.20218](https://arxiv.org/abs/2606.20218)，Interspeech 2026
- **可借鉴度**：⭐⭐⭐ 这是 2026 年最新、且**同时解决"泄漏"与"延迟"**的思路，最值得深入跟踪。

### 6.7 其他已发表的相关机制（备查）
| 机制 | 一句话 | 来源 |
|---|---|---|
| **USM-VC** | 通用语义字典 + 内容特征重表达（CFR）模块，产出"无音色内容表征" | [arXiv:2504.08524](https://arxiv.org/abs/2504.08524) |
| **SemAlignVC** | SemAlign：对齐文本与音频表征，确保说话人无关的语义编码 | [arXiv:2507.09070](https://arxiv.org/abs/2507.09070) |
| **ProsoCodec** | 韵律导向 codec，VC 任务上"reduced source-timbre leakage" | [arXiv:2606.21888](https://arxiv.org/abs/2606.21888) |
| **SDP-Codec** | 说话人解耦 + **pitch injection**，最低说话人探测准确率（泄漏最少） | [arXiv:2606.21157](https://arxiv.org/abs/2606.21157) |
| **LDM-SVC** | 基于 So-VITS-SVC VAE 预训练的潜在扩散 + **singer guidance**（classifier-free guidance 抑制原唱歌手音色） | [arXiv:2406.05325](https://arxiv.org/abs/2406.05325) |
| **Zero-Shot SVC clustering phoneme repr.** | 发现"**每歌手录音数越少越容易音色泄漏**"——1 万小时歌声实测 | [arXiv:2409.08039](https://arxiv.org/abs/2409.08039) |
| **IBF-VC / streaming teacher guidance** | 用中间瓶颈特征替代 PPG + **非流式教师引导（TG）专门解决音色泄漏** | [arXiv:2210.15158](https://arxiv.org/abs/2210.15158) |
| **RVC 自带** | "Reduce tone leakage by replacing the source feature to training-set feature using **top1 retrieval**" | RVC README.en.md |

### 6.8 关于 f0 condition / RMVPE / FCPE 的具体建议

- **F0 conditioning 对 SVC 是必需的**：Seed-VC 论文明确"we further extend our approach to zero-shot singing voice conversion **by incorporating fundamental frequency (F0) conditioning**"。HQ-SVC 也"enhances fidelity through **pitch and volume modeling**"。
- **⚠️ 但 F0 condition 本身会带来延迟代价**：Zero-VC 论文明确指出，IB 类方法丢掉韵律后靠显式注入 F0，"**often requires buffering future frames, creating algorithmic lookahead latency**"。Conan 则用 **causal pitch predictor（因果音高预测器）**来规避——"the causal pitch predictor estimates F0, as **explicit pitch modeling greatly aids in conveying speaking styles**"。**→ 结论：要用 F0 condition，必须用因果/前瞻受限的实现（Conan 的 causal pitch predictor 是范本），否则会吃掉你的延迟预算。**
- **RMVPE**：Interspeech 2023，RVC 官方已集成（"Vocal pitch extraction: RMVPE"，支持 pytorch/onnx/DirectML）。是复音音乐中直接从混合音频提取人声音高的强基线，**不需先做人声分离**——对"翻唱带伴奏"场景直接受益。来源：[Interspeech 2023 PDF](https://www.isca-archive.org/interspeech_2023/wei23b_interspeech.pdf)
- **FCPE**：**2025-09 已有正式论文**，arXiv:2509.15140。Lynx-Net + 深度可分离卷积，MIR-1K RPA **96.79%**（与 SOTA 持平），**RTX 4090 上 RTF 0.0062**（极快），噪声鲁棒性优于既有方法。代码 [CNChTu/FCPE](https://github.com/CNChTu/FCPE)。RVC 社区已有 issue 讨论 FCPE vs RMVPE（[issue #2825](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/issues/2825)，2026-07：FCPE 支持 cuDNN，但训练时选不了、只能推理时用）。
- **"唱不了的音域扩展"**：Poly-SVC 的 **CQT 音高提取器**保留主旋律+残留和声（[arXiv:2605.12310](https://arxiv.org/abs/2605.12310)）；VibE-SVC/VibE-SVC2 用**离散小波变换**分解 F0 轮廓做颤音控制，并提出 **Subharmonic Correction 算法**修正常规 F0 提取在 subharmonic（次谐波，如 vocal fry）上失败的问题（[arXiv:2505.20794](https://arxiv.org/abs/2505.20794)、[arXiv:2606.17126](https://arxiv.org/abs/2606.17126)）。

### 6.9 针对本项目三条实测痛点的一一对应

| 用户痛点 | 文献对应根因 | 推荐首选解法 |
|---|---|---|
| **跨音色 CAM++ 仅 0.78（自转 0.98）** | 内容表征残留源说话人信息 + 训练/推理音色域不一致（Seed-VC 三障碍） | ① **SPA-SVC cycle pitch shifting + SSIM loss**（纯数据增强，最易落地）；② 若愿改架构：**NeuCoSVC/DAFMSVC 最近邻 SSL 特征替换** |
| **翻唱"声音混杂" + 频谱闷（4-8kHz 掉 18.5dB）** | ① kNN 类：WavLM 缺谐波强调 → dull + ringing；② 混音/分离伪影 | ① **kNN-SVC 加法合成**补谐波；② **NSF（神经源滤波）显式建模谐波+噪声**（SingNet 开源了 NSF-HiFiGAN 预训练）；③ SYKI-SVC 的**后处理直接从源信号补高频**（"augments high-frequency information directly from the source through simple and effective signal processing"） |
| **±12 半音时音色泄漏（对原唱相似度 0.2557）** | 跨音域（cross-domain）时解耦失效、模型退化为"沙哑+泄漏" | **SPA-SVC 的 cycle pitch shifting**（论文明说专治"pitch disparity significant 时 hoarseness"）+ **R2-SVC 随机 F0 扰动** + **Zero-VC 的 SA 扰动机制**（2026 最新，同时不增加延迟） |

---

## 7. 明确「未找到」清单（避免误导）

1. **Seed-VC v3** —— 未找到。仓库 v1/v2 两代，无 v3。
2. **so-vits-svc 5.0** —— 未找到。主仓止于 4.1-Stable（2023-11 archived）。
3. **RVCv3 已发布基座模型** —— 未找到。README 有路线图文案，但无公开下载。
4. **FreeVC-24** —— 未找到可靠一手来源。
5. **LVC-VC** —— 未找到独立可靠项目（可能名称有误，或被 StreamVC/DualVC 等覆盖）。
6. **kiki-vc** —— 未找到（多渠道检索均无命中）。
7. **VoiceLDM / DiffVC 独立项目页** —— 未找到。
8. **Seed-VC / Conan / OpenVoice 的官方 8GB 显存实测数据** —— 未找到。
9. **DiffSinger / NNSVS / OpenUtau 与零样本 SVC 的 2025-2026 直接对比基准** —— 本次未调研，未找到。
10. **kNN-VC 的明确开源许可证** —— 未找到（bshall/soft-vc 未见 SPDX 声明）。

---

## 8. 来源汇总

### GitHub 仓库
- RVC 主仓：https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI
- RVC 英文文档：https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/blob/main/docs/en/README.en.md
- so-vits-svc（archived）：https://github.com/svc-develop-team/so-vits-svc
- voicepaw/so-vits-svc-fork（实时 fork）：https://github.com/voicepaw/so-vits-svc-fork
- RVC-Boss/sovits：https://github.com/RVC-Boss/sovits
- Seed-VC（archived）：https://github.com/Plachtaa/seed-vc
- Seed-VC vs RVCv2 评测 commit：https://github.com/sheiy/seed-vc/commit/3f719fe3de6d01261e18d13bf2cdc7e82da99a86
- Conan 官方实现（MIT）：https://github.com/User-tian/Conan
- X-VC：https://github.com/Jerrister/X-VC
- MeanVC：https://github.com/ASLP-lab/MeanVC
- kNN-SVC：https://github.com/SmoothKen/knn-svc
- soft-vc（kNN-VC）：https://github.com/bshall/soft-vc/
- FCPE：https://github.com/CNChTu/FCPE
- VecTok：https://github.com/BakerBunker/VecTok
- OpenVoice：https://github.com/myshell-ai/OpenVoice
- Seed-VC 实时服务封装：https://github.com/Leroll/fast-vc-service
- Seed-VC changelog（SVC vs RVCv2）：https://github.com/Plachtaa/seed-vc

### arXiv 论文
- Seed-VC：https://arxiv.org/abs/2411.09943
- Conan：https://arxiv.org/abs/2507.14534
- X-VC：https://arxiv.org/abs/2604.12456
- MeanVC 2：https://arxiv.org/abs/2606.09050
- MeanVC：https://arxiv.org/abs/2510.08392
- SPA-SVC：https://arxiv.org/abs/2406.05692
- NeuCoSVC：https://arxiv.org/abs/2312.04919
- DAFMSVC：https://arxiv.org/abs/2508.05978
- Zero-VC：https://arxiv.org/abs/2606.20218
- kNN-VC / soft-vc：https://arxiv.org/abs/2111.02392
- kNN-SVC：https://arxiv.org/abs/2504.05686
- FreeVC：https://arxiv.org/abs/2210.15418
- Vec-Tok Speech：https://arxiv.org/abs/2310.07246
- Vec-Tok-VC+：https://arxiv.org/abs/2406.09844
- OpenVoice：https://arxiv.org/abs/2312.01479
- LLVC：https://arxiv.org/abs/2311.00873
- SynthVC：https://arxiv.org/abs/2510.09245
- RT-VC：https://arxiv.org/abs/2506.10289
- TVTSyn：https://arxiv.org/abs/2602.09389
- Stream-Voice-Anon：https://arxiv.org/abs/2601.13948
- PHONOS：https://arxiv.org/abs/2603.27001
- FCPE：https://arxiv.org/abs/2509.15140
- RMVPE（Interspeech 2023）：https://www.isca-archive.org/interspeech_2023/wei23b_interspeech.pdf
- SVCC 2025 分析：https://arxiv.org/abs/2509.15629
- S²Voice（SVCC 2025 冠军）：https://arxiv.org/abs/2601.13629
- HQ-SVC（AAAI 2026）：https://arxiv.org/abs/2511.08496
- R2-SVC：https://arxiv.org/abs/2510.20677
- REF-VC：https://arxiv.org/abs/2508.04996
- YingMusic-SVC：https://arxiv.org/abs/2512.04793
- Poly-SVC（ICASSP 2026）：https://arxiv.org/abs/2605.12310
- SaMoye：https://arxiv.org/abs/2407.07728
- FreeSVC：https://arxiv.org/abs/2501.05586
- CartoonSing：https://arxiv.org/abs/2511.21045
- VibE-SVC：https://arxiv.org/abs/2505.20794
- VibE-SVC2：https://arxiv.org/abs/2606.17126
- LDM-SVC：https://arxiv.org/abs/2406.05325
- Zero-Shot SVC clustering phoneme：https://arxiv.org/abs/2409.08039
- USM-VC：https://arxiv.org/abs/2504.08524
- SemAlignVC：https://arxiv.org/abs/2507.09070
- ProsoCodec：https://arxiv.org/abs/2606.21888
- SDP-Codec：https://arxiv.org/abs/2606.21157
- IBF-VC + teacher guidance：https://arxiv.org/abs/2210.15158
- SingNet（含 NSF-HiFiGAN 预训练）：https://arxiv.org/abs/2505.09325
- RVCBench（NeurIPS 2026，18 个 VC 模型鲁棒性基准）：https://arxiv.org/abs/2602.00443
- Everyone-Can-Sing（零样本 SVS/SVC）：https://arxiv.org/abs/2501.13870
- SAC codec（X-VC 基座）：https://arxiv.org/abs/2510.16841

### 其他
- OpenVoice 官方文档（MIT/中文/V2）：https://docs.myshell.ai/technology/openvoice
- so-vits-svc vs RVC 中文社区评价：https://www.cnblogs.com/zxdplay/p/19433775
- RVC forks 活跃度（实时 GUI fork）：https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/forks
- Conan demo 页：https://aaronz345.github.io/ConanDemo
- kNN-SVC demo：http://knnsvc.com
