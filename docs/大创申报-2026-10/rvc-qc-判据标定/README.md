# RVC 变声验收（`emb_sim`）判据标定 · 证据

**这是 `tools/voice_qc.py` 的 `run_voice()` 里 `emb_sim` 项（100 分制占 25 分）判据的实测标定**，
回答一个问题：现役的「Qwen 自空间余弦 ≥ 0.95」在这条**变声验收**路径上站不站得住，要不要换。

- 脚本：`tools/voice_qc_ruler_calib.py`（复现见下）
- 单测：`m2_server/tests/test_voice_qc_ruler_calib.py`（复现下面这三个文件，并钉住判定规则）
- 生成日期：2026-10-11

## 文件

| 文件 | 内容 |
|---|---|
| `pairs.csv` | **逐对分数本身**（96 对：12 同人 + 84 异人），两把尺子各一列 |
| `results.json` | 分布 / AUC / EER / d′ / 现役 0.95 的通过率 / 双门槛推导 / 重放 / **相对排序** / **上线判据的 `prod_margin` 重放** |
| `report.md` | 给人看的报告（结论 + 两把尺子 + 三个换法 + 边界） |

## 结论（2026-10-11）

1. **现役 0.95 站不住**（`switch`）：同人侧余量 −0.0392（12 对同人只有 6 对 ≥0.95，即**误杀一半**），
   异人侧余量 −0.0034。
2. **不能照搬 pilotC 的双门槛**（`../pilotC-公开语料证据/dual_embed/`）：拿那套
   `cam_pass=0.53` 重放，同人只过 2/12（8 个 `review`、2 个 `reject`）—— **两条路径的刻度不能互用**。
   这正是本标定的意义。
3. **本批两把尺子的同人/异人簇都重叠**（自空间 0.9108 ≤ 0.9534；CAM++ 0.2659 ≤ 0.4859），
   `derive()` 如实报「无阈值可推」——**任何绝对阈值都有错判**。
4. **但相对判据可行**：同一份输出 vs 全部 8 支参考音，**独立声纹（CAM++）12/12 都把自己的目标
   参考排在第 1**（余量 +0.024 ~ +0.516，全为正）；自空间只有 8/12（4 对余量为负）。
   → 该换的是**判法**（相对余量），不是尺子；自空间不该当准入线。
5. **已落地（2026-10-11）**：`tools/voice_qc.py` 的 `emb_sim` 项 pass 换成**相对余量**
   —— `CAM++(输出, 目标参考) > CAM++(输出, 输入者本人)`；`results.json` 的 `prod_margin`
   就是它的从证据重放：**12/12 通过，最小余量 +0.2137**。问不出来时（自转换 / CAM++ 权重
   未缓存）如实记「未测」（`pass=null`），**未测 ≠ 不合格**。分制与另三项未动。
   附带：参考音 **<5s** 时在结果里提示（验收记 `voice.warnings`，嗓音随访记 `rec.warnings`
   —— 口径唯一定义在 `m2_server/speaker_sep.MIN_REF_SECONDS`）—— 提示，不作废判定。

## 边界（别外推）

- 目标音色只有 **3 个说话人 / 4 支模型**（袋鼠两支 + 卡通两支），输入是他人参考音 3 段 ——
  结论只对**这条验收链路**负责。
- 参考音时长 4.2~30s 不等（卡通市场音色只带 4.2s preview）。**短参考是 CAM++ 簇重叠的主因**，
  换素材要重标。
- 公开语料（AISHELL-3、VBDataset 试听）与自录素材混用；**只发布分数，不发布音频**。

## 复现

```bash
# 推理产物已在 outputs/qc_calib/ 时只复算（不重跑 RVC）
.venv/Scripts/python.exe tools/voice_qc_ruler_calib.py --reuse
# 从零重跑（含 12 次 RVC 离线推理，与验收同一条命令）
.venv/Scripts/python.exe tools/voice_qc_ruler_calib.py
# 改脚本后请把三份产物重新拷进本目录，再跑单测（单测就是靠它复现结论）
```

```bash
.venv/Scripts/python.exe -m pytest m2_server/tests/test_voice_qc_ruler_calib.py -q
```
