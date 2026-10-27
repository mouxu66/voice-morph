# 大创申报-2026-10 · 申报材料已归档（本目录只剩证据）

> 归档日期：**2026-10-15**。大创申报已结束，用户要求清掉申报资料。

## 一句话

申报成品与「申报语境」文档（申报书 / PPT / 答辩讲稿 / 导师沟通 / 官方要求 …）
已整目录移到**仓库根目录 `_trash_大创-2026-10/`**（未入库、可反悔）；
本目录**只留下被现役代码、测试或评测协议读到的证据**。

## 为什么没有整目录搬走

有几份文件是**被代码和测试直接读的**（不是"可能有用"，是 `pytest` 会红）：

| 留在这里的东西 | 谁在读它 |
|---|---|
| `rvc-qc-判据标定/` | `m2_server/tests/test_voice_qc_ruler_calib.py:30`（`EVIDENCE_DIR`）、`m2_server/tests/test_voice_qc_emb_judgement.py:164`、`tools/voice_qc.py` |
| `减负pilot-s01-记录.md`、`大创方案-v2.md` | `m2_server/tests/test_secs_ruler_labeling.py:106-107`（`STALE_DOCS`）、`:141`；`docs/交接-AI必读.md:36` |
| `pilotC-公开语料证据/` | `tools/voice_dual_gate.py:46`（`EVIDENCE_DIR`，运行时真的会去读）、`m2_server/voicepreserve.py:1099`、`docs/附录-可执行规程.md:303-326`、`docs/交接-AI必读.md:40` |
| `千问微调vsRVC-裁决/` | `docs/交接-AI必读.md:531`、`docs/product/research/m1f_千问微调vsRVC-声音克隆裁决_2026-10-14.md`（可重放原始分） |
| `pilot-讲稿-10分钟.md` | `docs/附录-可执行规程.md:40` |

另外三份是**技术结论/执行卡**（没被代码引用，但内容是项目结论而非申报文案，故留下）：
`AB对照-12分钟PPT微调-2026-10-12.md`、`RVC克隆-12分钟真人素材-2026-10-12.md`、
`RVC音色优化空间-三个上限-2026-10-13.md`、`听测现场执行卡-RQ3.md`、
`真人小样本试跑-执行卡.md`。

## 搬走的东西（在 `_trash_大创-2026-10/`）

- **成品/媒体**：`大创申报.pdf`、`大创申报.pptx`、`大创申报PPT.mp4`（178 MB，12:47 真人讲 PPT）、
  `大创申报PPT.pdf`、`大创申报PPT-去AI味版.pdf`、`大创申报PPT.pptx`、`大创项目申报书*.docx/pdf`、
  `附件1-校级大创项目申报表-已填写.xlsx`、`项目申报书.docx`
- **申报文本**：`01-申报书正文.md`、`README.md`（旧版，申报 hub）、`PPT结构重排-按官方5条.md`、
  `P13改造-已完成.md`、`官方要求-创新类.md`、`导师意见10-11-逐条回应与整改.md`、
  `导师沟通-完善建议-2026-10-04.md`、`导师邀请一页纸.md`、`答辩讲稿-19页.md`、
  `项目完善建议-2026-10-04.md`、`_probe.txt`

⚠️ `大创申报PPT.mp4` 是那批 RVC 克隆实验（81 条 / 12.23 分钟）的**原始素材**，
上面几份留在这里的实验记录已把引用路径改成 `_trash_大创-2026-10/大创申报PPT.mp4`。
**要复现那些结论就别清空这个归档目录。**

## 想连证据一起清掉，得同时改这些地方

`m2_server/tests/test_voice_qc_ruler_calib.py`、`test_voice_qc_emb_judgement.py`、
`test_secs_ruler_labeling.py`、`tools/voice_dual_gate.py`、`tools/voice_qc.py`、
`m2_server/voicepreserve.py`，以及上表里 docs 侧的引用。

## 申报专用脚本（仍在 `tools/`，已无输入）

`dachuang_docx_condense.py`、`dachuang_docx_sync.py`、`dachuang_numbers_sync.py`、
`dachuang_p13_build.py`、`dachuang_p13_shots.py`、`dachuang_pptx_condense.py`、
`sb_fill_docx.py`、`sb_fill_xlsx.py`、`sb_probe_docx.py` ——
它们的输入（申报书 docx / PPT pptx）已归档，现在指向不存在的路径，需要时再改路径或一起归档。
