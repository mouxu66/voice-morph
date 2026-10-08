# pilotC · 公开许可语料上的三人链路与趋势验证

> **一句话：这是真人数据，但不是被试数据。**
> 3 名说话人来自 **AISHELL-3（Apache-2.0）**，是**公开许可的语料**，
> 没有知情同意流程、不是职业用嗓人群、参考音是**同一说话人同一次录制的多条拼接**。
> 能支撑「链路在 3 个真人嗓音上跑通 + 趋势方向」；**不能**支撑任何总体性结论。

生成时间：2026-10-08 ｜ 引擎：`tools/voicebank_fetch_public.py` + `voicebank_synth.py`（新）
+ `voicebank_eval.py` ｜ 合成：Qwen3-TTS worker（纯声纹 x-vector，无 ref_text）

---

## 1. 来源与合规（★ 谁引用谁负责把这段抄过去）

| 项 | 值 |
|---|---|
| 语料 | **AISHELL-3**（218 人中文 TTS 语料 / 85h / emotion-neutral） |
| 许可 | **Apache-2.0**（允许商用与演绎，署名即可） |
| 许可一手出处 | <https://www.openslr.org/93/> 原文：`License: Apache License v.2.0`（2026-10-08 核对） |
| 获取通道 | 官方 HF 数据集的镜像仓库 `AISHELL/AISHELL-3` 的 `train` 划分 |
| 署名 | AISHELL-3: A Multi-speaker Mandarin TTS Corpus and the Baselines (Shi et al., 2020), Beijing Shell Shell Technology Co.,Ltd. |
| 逐条哈希与剔除记录 | `media/vocals/public_pilot/SOURCES.json` |

选它而不是别的：ST-CMDS 是 `CC BY-NC-ND 4.0` —— **NC 禁商用 + ND 禁演绎**，
而本项目要用音频做"切档 + 拼接"（就是演绎），两条全踩；
THCHS-30 / AISHELL-1 许可同为 Apache-2.0 但为短句 ASR 语料。
抓取工具里**许可白名单是机器门禁**（`tools/voicebank_fetch_public.py::check_license`），
写 `CC-BY-NC-ND` 直接拒绝执行、一条都不下。

## 2. 三个说话人

| 说话人 | 年龄/性别/口音 | 可用条数 | 拼后时长 |
|---|---|---|---|
| SSB0033 | B(14–25) / female / south | 25/26（剔 1） | 85.46s |
| SSB0603 | B(14–25) / male / north | 26/26 | 83.34s |
| SSB0737 | D(>41) / female / north | 23/26（剔 3） | 81.72s |

剔除的 4 条是削波 / 近乎静音 / 读不出（原因逐条记在 `SOURCES.json`）——
**一条爆音会把 reference 的 HNR/jitter 整个带偏**，而报告上看不出原因，所以不静默拼进去。

## 3. 跑出来的结果（★ 引用时必须带 §1 的许可与 §4 的边界）

```
fetch  → 3 说话人 × 26 条 → 拼接成 82–85s「连续」信号（同一说话人/同一次录制）
split  → 前缀切四档，对齐句末：如 SSB0033 实际 3.00 / 8.82 / 28.39 / 57.86s
synth  → 48 格全成功，0 失败（3m30s，纯声纹模式；临时音色零残留）
measure→ 48/48 测出，0 失败（emb_sim 活着；asr_overlap 需 truth，本轮跳过）
listen → 盲听材料已生成：48 条全部只能评 MOS（无 truth 对照）
analyze→ report.md / report.json
```

### 3.1 RQ1（时长 → 质量）

| 档位 | 3s | 10s | 30s | 60s | 判据 |
|---|---|---|---|---|---|
| `emb_sim` | 0.9815 | 0.9843 | **0.9854** | 0.9854 | **饱和档 30s**（该档增量占总增益 0.0%） |
| `hnr` | 13.031 | 12.4225 | 12.3125 | 12.241 | 数据不足以判定 |
| `h1_h2` / `spectral_tilt` / `jitter_local` | — | — | — | — | 非单调，不可判定 |

**怎么读**：`emb_sim` 在 10s 前有明显增益、30s 后归零 —— 与"秒级小样本"这条线一致。
其余指标在 3 人规模下判据一律返回"不可判定"（这正是引擎该有的诚实行为）：
**3 人是 pilot，协议 §3.3 要求 ≥8 人才谈 RQ1 主效应**。

### 3.2 RQ2 在**本轮不成立**（必须明说）

`same` 条件的定义是"复现文本就是参考音里念过的句子"，而本轮参考音来自语料，
**不含本项目复现文本** —— 所以 `same` 与 `cross` 都是跨文，
两者的差异**不能**解读为"同文 vs 跨文的泛化差距"。RQ2 等真人脚本录制再谈。

### 3.3 RQ3 为空

主观评分尚未收集（`listen/sheet.csv` 待真人听测）。所以在**补上听测之前**，
不能引用任何"客观指标可替代人工听测"的说法。

## 4. 边界（照抄进申报书，不要藏）

- **不是被试数据**：无知情同意流程；说话人不是职业用嗓人群；语料面向 TTS 训练而非嗓音保存。
- **参考音是拼接的**：协议 §4.2 要求"一次连续录完"，本轮是同一说话人/同一次录制的多段拼接
  —— 保住同一说话人 / 同一麦 / 同一房间，**丢掉"同一条 take"**。
- **无 ABX**：语料里没有本项目复现文本的真实录音，缺 `truth/`，只能评 MOS。
- **不再分发**：原始音频在本机 `media/`（gitignore），不进任何训练集（袋鼠音素材铁律）、
  不得再分发；对外只引用**统计结果**。
- **样本量**：n=3 说话人、单机单卡、中文 —— 不作外推。

## 5. ⚠️ 本轮暴露的一个工具缺陷（当时已修）

`split.json` 是**单说话人**的（实验级路径）：第二个说话人切档时就把第一个覆盖掉了。
实测现象：三个人跑完后，只剩 **SSB0737** 的切点与静音观测，前两人的丢了
—— 而协议 §4.3 要求"切点留档"。

影响范围有限（**每档的实际秒数没丢**：`provenance.json` 里逐格记了 `ref_actual_s`，
见本目录），丢的是"切点多在句末 / 当时探测到的静音在哪"这类追溯信息。

已修（`tools/voicebank_eval.py::split`）：**逐说话人另存 `split/<说话人>.json`**，
实验级那个文件保持原样不破旧消费方；补了一条单测钉住（
`test_per_speaker_split_log_is_kept`）。真人试跑时不会再碰到。

## 6. 复现

```bash
# ① 抓取（许可门禁 → 逐条质检 → 拼接；可中断续跑）
.venv/Scripts/python.exe tools/voicebank_fetch_public.py \
    --manifest tools/voicebank_public.aishell3.json --out media/vocals/public_pilot
# ② 切档 + 合成 + 测量 + 听测材料 + 报告
.venv/Scripts/python.exe tools/voicebank_eval.py plan --exp pilotC --speakers SSB0033,SSB0603,SSB0737
for s in SSB0033 SSB0603 SSB0737; do
  .venv/Scripts/python.exe tools/voicebank_eval.py split --exp pilotC --speakers $s \
      --source media/vocals/public_pilot/$s/source.wav
done
.venv/Scripts/python.exe tools/voicebank_synth.py --exp pilotC --api http://127.0.0.1:8000
.venv/Scripts/python.exe tools/voicebank_eval.py measure --exp pilotC --with-emb
.venv/Scripts/python.exe tools/voicebank_eval.py listen --exp pilotC
```
