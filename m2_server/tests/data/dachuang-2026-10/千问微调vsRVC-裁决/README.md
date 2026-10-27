# 证据：千问微调 vs RVC 的克隆像度对照（2026-10-14）

**裁决书在** `docs/product/research/m1f_千问微调vsRVC-声音克隆裁决_2026-10-14.md`
（本目录只放**原始分**，因为音频产物在 `media/` 下、被 `.gitignore` 忽略，不入库）。

## 文件

| 文件 | 内容 |
|---|---|
| `jobs.json` | 15 个打分任务（参考音 + 四臂 12 条 + 上限 2 条），路径相对仓库根 |
| `scores.json` | 上面每个任务的 `secs`（CAM++ 音色像度）+ `nats`（NatScore 自然度） |

## 这批分的用法与边界

- **`secs` 可用**（同一次打分、同参考音）：`rvc 0.8356~0.8447 > qwen_ft 0.8030 > base 0.8006`，
  RVC 逐句 3/3 赢、跨 seed 区间不重叠。
- **`nats` 在本批不可用**：真人参考音 −2.303、真人→RVC 上限 −3.033/−3.218，
  而所有合成臂都是正的（+0.63~+4.10）⇒ 尺子把真人排在合成之下，**自然度记「未测」**。
  验尺子的探针：`tts_trial/probe_natscore_domain.py`（递进劣化看单调性）。

## 复现

```bash
.venv/Scripts/python.exe m2_server/audition_score.py \
  --jobs m2_server/tests/data/dachuang-2026-10/千问微调vsRVC-裁决/jobs.json \
  --out  m2_server/tests/data/dachuang-2026-10/千问微调vsRVC-裁决/scores.json
```

（`jobs.json` 里引用的音频需先存在：`media/ft_work/ppt12/…`，由
`tts_trial/ab_ft_vs_rvc.py` / `ab_ft_vs_rvc_seeds.py` 那一轮生成。）
