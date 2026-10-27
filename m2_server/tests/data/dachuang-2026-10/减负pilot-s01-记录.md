# 减负 pilot · s01 记录（2026-10-09）

> **n=1 预实验**。目的：打通"发声负荷测量 → 旁白合成 → 客观门禁"整条链路，
> 并产出 A 侧真人基线。口径与红线以 [`减负实测协议.md`](../../docs/减负实测协议.md) 为准。
> **本记录不是统计结论，不入申报书正文，只作为 pilot 链路验证**（申报书照 §9 模板等正式数据）。

---

## 1. 一句话结论

A 侧（本人全程开口讲完 19 页答辩）**发声 11 分 09 秒**已实测入库；
B 侧（同一份讲稿转旁白）**旁白 11 分 55 秒全部由本人音色合成、SECS 0.99**，
**本人开口待补录计数** —— 这是全实验唯一还需要"人"来完成的一步。

**没有编造任何数字**：A 侧是量出来的，B 侧合成时长是量出来的，节省率**有意留空**
（分母 A 已实测，分子 = 补录总时长，尚未产生）。

---

## 2. 素材与场景

| 项 | 值 |
|---|---|
| 场景 | **答辩/汇报**（对着 PPT 自然讲），**非录课** —— 口径上按协议 §3.1 的"本人全程开口讲完"处理 |
| A 侧来源 | `大创申报PPT.mp4`（OBS，1920×1080，767.2s）→ 抽音轨 `media/voice_load/s01/lesson_A.wav`（16k 单声道） |
| 讲稿 | `答辩讲稿-19页.md`（19 页，与 PPT 逐页对应） |
| B 侧产物 | `media/voice_load/s01/synth.wav`（19 页旁白合轨）+ `outputs/voice_load/s01_synth/page_%03d.wav` |
| 参考音 | `media/voice_load/s01/ref_voice.wav`（从 A 侧录音截 10s，**同一人/同一麦/同一房间**，协议 §3.3） |

---

## 3. A 侧测量（真人基线，实测）

`tools/voice_load.py measure`（能量法，ffmpeg silencedetect 自适应门限；口径见协议 §3.2）

| 指标 | 值 |
|---|---|
| 全程 | 766.4 s（12:46） |
| **发声总时长** | **669.4 s（11:09）** |
| 发声占空比 | 87.4% |
| 最长连续发声 | 23.8 s |
| 发声段数 | 154 |
| 自适应门限 | −42.2 dB |
| 峰值 | **−1.8 dBFS** |

> **电平复核**（比协议 §3.2 的"先看 peak_dbfs"更进一步）：−1.8 dBFS 超出 −3 警戒线，
> 属"热信号"。逐样本核过 **无削波**（|x|≥0.95 的样本 0 个，原始立体声轨复核），
> 是干净的热电平、不是失真 → **数据可用**。下次录音把增益降 6 dB 更规范。

## 4. 内容红线审查（转写后逐词扫）

faster-whisper small 转写 → 2966 字（`outputs/voice_load/s01_A_transcript.json`）。

| 红线词 | 命中 | 判定 |
|---|---|---|
| 疗效 | "**不承诺**任何医**疗效**果" | ✅ 否定句，合规 |
| 保证 | "嗓音档**案库**"被 ASR 听成"保**证**" | ✅ ASR 误识，非声称 |
| 首次/领先/唯一/已减轻/治疗/治愈/显著降低… | 未命中 | ✅ |

**结论**：讲稿口径守得住 —— 明确说了"四个关键问题目前都还没有数据""不宣传任何医疗或
诊疗效果""不做产品商业叙事""仅克隆本人或已授权声音"，与申报书 §2.1 一致。
（转写有小模型误识，仅用于关键词扫描，不作为发言逐字稿。）

## 5. B 侧旁白合成与客观门禁

合成链路：`qwen3_tts`（Qwen3-TTS 1.7B，worker:8001）按页逐句克隆，句间 250ms ——
与 `m2_server/course_api.py` 的讲稿录课**同一条链路**。

| 指标 | 值 |
|---|---|
| 旁白全程 | 714.7 s（11:55）= A 侧的 **93.3%** |
| 合成发声 | 606.0 s（10:06） |
| 占空比 | 84.8% |
| 峰值 | −7.8 dBFS（干净） |
| **SECS（像不像你）** | **0.9888** |
| ↳ 同人不同段上限 | 0.9928（天然天花板） |
| NatScore（自然度） | −5.292 |

> **门禁结论（RQ-B3）**：合成旁白与本人音色的声纹相似度 **0.9888**，
> 距"同一人两段录音"的上限 0.9928 只差 0.004 —— 音色已到"像你"的边界内。
> 这是 B 侧产物**能过自动门禁**的客观证据。
> ⚠️ **口径更正（2026-10-11）**：本行原把该余弦标成 CAM++，**是错的** ——
> `tools/clone_eval.py` 的 `/emb` 打的是 **Qwen3-TTS 自己的说话人编码器（自空间，与克隆同源）**，
> 不是 CAM++（真 CAM++ 在 `speaker_sep._sv_embed`）。而 `pilotC` 双声纹实验刚证明这把尺子**会饱和**
> （同批 48 样本：自空间同人 87.5% ≥0.98，CAM++ 同人只给 0.7486）。
> ⇒ **0.9888 只能在"自标尺"这个口径下引用**，不能与 CAM++ 阈值互用；
> 门禁已改双门槛（`tools/voice_dual_gate.py`）。本条合成产物**尚未补测 CAM++**，补测前不得并列两刻度。

## 6. 节省率与缺口（★ 唯一待办）

协议 §4 定义：**发声时长节省率 = (A.phonation − ΣB.phonation) / A.phonation**，
其中 **B 侧本人口腔发声 = 补录/改稿重录的录音之和**（写稿、审听、播放不计，§3.1）。

- 分母已实测：A.phonation = **669.4 s**。
- 分子待产生：B 流程里本人开口的每一条录音（`s01_retake_01.wav`…）。
- **现在有 0 条补录** → 按 §8.2 **如实写"本人口腔发声 0 分钟"，并注明"无补录"**，
  **不写"零负担"、不把 100% 当卖点**（那会被读成循环论证）。

补上分子只需一步（无需重录 A）：

```bash
# 走一遍旁白流程、把本人开口的每一条补录存进 s01/ 后：
cd D:/变声
.venv/Scripts/python.exe tools/voice_load.py compare \
  --a media/voice_load/s01/lesson_A.wav \
  --b media/voice_load/s01/retake_01.wav media/voice_load/s01/retake_02.wav \
  --synth media/voice_load/s01/synth.wav \
  --json outputs/voice_load/s01_compare.json
# saving.saving_rate 就是"减负 X%"；再 collect 出总表即可填申报书 §9 模板
```

> **预测**：SECS 0.9888 说明旁白"像你"到接近上限，**大概率只需少量补录**（主要是英文缩写
> 与专名的读音：H1-H2 / RTF / ABX / MOS / GitHub / Personal Voice 这类）→ 预期 B 侧
> 本人发声很短、节省率很高 —— 但这只是**预测**，以补录实数为准，不写进结论。

## 7. 复现命令（全部本机、可重跑）

```bash
# A 侧ffmpeg -y -i "_trash_大创-2026-10/大创申报PPT.mp4" -vn -ac 1 -ar 16000 \
  -c:a pcm_s16le media/voice_load/s01/lesson_A.wav
.venv/Scripts/python.exe tools/voice_load.py measure media/voice_load/s01/lesson_A.wav \
  --json outputs/voice_load/s01_A.json

# B 侧（合成脚本见 outputs/.session/synth_B.py；参考音截自 A 侧）
.venv/Scripts/python.exe tools/voice_load.py measure media/voice_load/s01/synth.wav \
  --json outputs/voice_load/s01_synth.json
.venv/Scripts/python.exe tools/clone_eval.py --ref media/voice_load/s01/ref_voice.wav \
  --candidates media/voice_load/s01/synth.wav --out outputs/voice_load/s01_synth_qc.csv
```

## 8. 诚实边界

1. **n=1 且场景是汇报不是录课** —— 只证明链路可跑通，不作任何统计结论、不外推。
2. B 侧本人口腔发声 = 0（无补录）是**该次流程的事实**，不等于"零负担"（写稿/审听成本真实存在，协议 §8.2）。
3. SECS/NatScore 是**客观近似**，不等于人耳主观评测；RQ3 的主观听测仍待正式实验。
4. A 侧 −1.8 dBFS 超警戒线但已核无削波；正式实验按 −3 ~ −12 dBFS 录。
5. 正式口径：pilot n=3 → 主实验 n=12，AB/BA 交叉，VFI/VAS 前后测（本记录尚无 L2 数据）。
