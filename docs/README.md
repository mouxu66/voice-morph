# docs 索引

本目录混着两类文档，读之前先确认手里这份属于哪一类：

- **现役（会随代码更新）** —— 与当前实现一致，看到不对就该改它。
- **历史快照（带日期，不再更新）** —— 某次审查/清理的过程记录。**里面的数字与结论只代表那一天**，
  别当成现状；但也别删：它们记录了"为什么当时这么改"。

## 现役

| 文档 | 内容 |
|---|---|
| `犯错指南.md` | **动微信/浮层/打包相关代码前必读**。踩过的坑按「症状/根因/对策/证据」归档，含 ✅本机实测 标注 |
| `AUDIO_TROUBLESHOOTING.md` | 音频设备、VB-CABLE、内录排障 |
| `CASCADE_STREAM_SPEC.md` | 级联变声（录音→ASR→TTS→RVC）链路规格 |
| `微信语音消息调研报告.md` | 微信语音消息格式/接口调研 |
| `release-sop.md` | 发版 SOP（含更新源、签名、回滚） |
| `分发与打包说明.md` | 打包/分发/本机与分发机的路径解析 |
| `ROADMAP.md` / `TTS_CUDA_GRAPH_PLAN.md` | 规划 |
| `CODE_REVIEW_*.md` / `PULL_REQUEST_TEMPLATE.md` | 评审流程与清单 |
| `A2-/A3-/A4-/B档-*落地说明.md` | 各功能档位的落地说明 |
| `product/` | 产品界定、功能规格、竞品分析、路线图、用户研究 |

## 历史快照（按时间倒序，不更新）

| 文档 | 那一天的结论 |
|---|---|
| `CODE_FIX_FINAL.md` | 代码质量 256 项问题清零（64% → 100%），5 文件 37 行改动 |
| `CODE_FIX_COMPLETE.md` | 同一轮修复的第一阶段快照（240+/256，93.75%）；后续见 `CODE_FIX_FINAL.md` |
| `CODE_QUALITY_REPORT.md` | 工具链清单（ruff/black/isort/mypy/pytest/vitest/eslint）+ 质量检测基线 |
| `DEV_TOOLS_SETUP.md` | 开发环境配置快照：AI 插件、npm 包、Python 工具 |
| `INTEGRATION_COMPLETE.md` | 应用内构建监听器集成报告（主进程 `web/electron/build-watch.cjs`） |
| `WATCH_SHIP_COMPLETE_GUIDE.md` | 前端自动构建使用指南。⚠️ 文中"正在开发中"已过时——该功能当日即落地，以 `INTEGRATION_COMPLETE.md` 与 `AGENTS.md` 的 `npm run ship` 一节为准 |
| `开源前待办清单-2026-09-12.md` | 开源就绪度审计（P0 已于 09-13 处理，见文内 ✅；**注意：其中提交哈希写于历史重写前，可能已失效**） |
| `代码审查-2026-09-11.md` | 代码审查 |
| `清理清单-2026-09-07.md` | 清理项 |
| `代码审查-2026-09-05.md` / `功能完善建议-2026-09-05.md` | 代码审查 + 功能建议（A 档来源） |
| `性能排查报告.md` | 性能问题定位记录 |
| `product/research/m1*_2026-09-09.md` | 竞品与自有产品走查 |

## 内部（作者本机环境专用）

| 文档 | 说明 |
|---|---|
| `internal/README-vm-test.md` | Win10 虚拟机里的**安装包自动更新**端到端测试手册；假定项目根是 `D:\变声` 且已配好 `scripts/vm-test.config.ps1`。开源用户日常验证请用 `python tools/check.py --ci-fidelity` |

> 复现本项目的工作流：改代码 → `python tools/check.py --fast`（提交前，钩子自动跑）→
> 推之前 `python tools/check.py --ci-fidelity`（复刻 CI 干净环境）。见根 `README.md` 的「自检」一节。
