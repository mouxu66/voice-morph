# 代码审查流程（Code Review Process）

> 与 `CODE_REVIEW_GUIDE.md` 配套。本流程把「火眼眼」评审标准落到日常开发节奏，并与 `AGENTS.md` 铁律（改动即提交 / 改动即补测 / 踩坑必记）对齐。

---

## 1. 原则

1. **小步快跑**：单个 PR ≤ 400 行（不含生成代码/迁移）；超了必须拆分。大改动先发设计/RFC，再分步合入。
2. **作者先自审**：提交前作者自己过一遍 `CODE_REVIEW_CHECKLIST.md`，不把明显问题丢给评审人。
3. **AI 先初审，人复核**：每个 PR 先用「火眼眼」专家做一轮自动审查，人工只盯 🔴 与有争议的 🟡。
4. **零阻断才可合入**：任意 🔴 未解决 = 不可 merge；🟡 需作者与评审协商一致（可记 TODO 跟进）。
5. **改动即对应 commit + 测试 + 记录**（铁律 1/2/3）。

---

## 2. 角色

| 角色 | 职责 |
|---|---|
| **作者（Author）** | 自审清单、跑本地门禁、写 PR 描述（含验证方式）、回应评审意见、补测。 |
| **评审人（Reviewer）** | 至少 1 人；按 GUIDE 给分级意见；对 🔴 有否决权。 |
| **维护者（Maintainer）** | 最终合入；确认铁律（commit/test/record）达成；处理跨模块争议。 |

> 个人项目可一人分饰：作者自审 + 火眼眼初审 + 自己复核，但 🔴 必须显式「已修复/已确认无碍」才合入。

---

## 3. 阶段流水线

### 阶段 0 · 自审（提交前）
- 跑 `CODE_REVIEW_CHECKLIST.md`，修复能自检的问题。
- 确认改动对应一个清晰意图，准备 conventional commit 信息（`feat/fix/docs/refactor/test/chore`）。

### 阶段 1 · 预合入门禁（必须全绿）
后端（在 `m2_server` 根或项目根执行）：
```bash
CODEBUDDY_SAFE_DELETE_ENABLED=0 .venv/Scripts/python.exe -m pytest m2_server -q
```
> 基线：272~280 绿。新增改动不得引入回归；新行为补用例。

前端（在 `web` 目录）：
```bash
npm run build        # = tsc -b && vite build，类型检查 + 打包必须过
```
> 前端暂无 test/lint 脚本，故 `npm run build` 是最小门槛；PR 描述需贴关键交互的验证截图。

其它铁律校验：
- `git diff --cached` 确认无 `.env` / 密钥 / 个人绝对路径（`D:/...mouxu...`）。
- 破坏性文件清理走 `tools/hard_delete.py`，不依赖 `shutil.rmtree` 真删。

### 阶段 2 · 评审（AI + 人工）
1. **火眼眼初审**：在会话中调用「火眼眼」专家，贴出 diff / 改动文件，按 GUIDE 出分级报告。
2. **人工复核**：评审人看 🔴 是否真修复、🟡 是否合理；对设计/契约/并发等需人判断的点给结论。
3. 往返不超过 2 轮；争议升级给维护者。

### 阶段 3 · 合入
- 所有 🔴 清零，🟡 协商一致。
- 用 `git-commit` / `conventional-commit` 技能生成规范化提交（铁律 1，不 `--no-verify`）。
- 桌面端改动：`cd web && npx vite build` 后重启桌面端即生效（无需 sync_backend / repack_asar）。

### 阶段 4 · 合入后
- 踩坑/决策/环境坑写入 `D:\变声\.workbuddy\memory\YYYY-MM-DD.md`（跨会话长期结论入 `MEMORY.md`）。
- 若本次修复了通用反模式（如静默 except），在记忆里登记以便后续专项清理复用。

---

## 4. SLA 与规模

| PR 规模 | 目标评审时长 | 备注 |
|---|---|---|
| 小（<100 行） | 当天 | 可免 RFC |
| 中（100–400 行） | 1 个工作日内 | 需 PR 描述 |
| 大（>400 行） | 拆分后分批 | 先设计再实现 |

---

## 5. 定义完成（DoD）

一个改动算「审查通过」当且仅当：
- [ ] 通过阶段 1 全部门禁（pytest 无回归 / `npm run build` 过）。
- [ ] 火眼眼初审报告无未结 🔴。
- [ ] 重要行为有测试（后端必填；前端至少人工验证记录）。
- [ ] 无密钥/个人路径入库。
- [ ] 已规范化 commit，且踩坑已记录（如适用）。

---

## 6. 工具与命令速查

| 用途 | 命令 / 入口 |
|---|---|
| 后端全量回归 | `CODEBUDDY_SAFE_DELETE_ENABLED=0 .venv/Scripts/python.exe -m pytest m2_server -q` |
| 前端构建+类型检查 | `cd web && npm run build` |
| 规范化提交 | `git-commit` / `conventional-commit` 技能 |
| AI 初审 | 会话内调用「火眼眼」专家，附 diff 与 GUIDE |
| 真正释放磁盘 | `python tools/hard_delete.py --dry-run`（先预览再删） |
| 踩坑记录 | `D:\变声\.workbuddy\memory\YYYY-MM-DD.md` |

---

## 7. 渐进路线（给维护者）

1. **Week 1**：落地本流程 + 清单 + PR 模板；对存量 `rvc_live.py` 等静默 except 做一次专项清理（标记为 🔴 历史债，分批还）。
2. **Week 2+**：前端补 `vitest` + `tsc --noEmit` 门禁；后端加最小 CI（无 CI 时作者本地跑并贴结果）。
3. **持续**：每月回看记忆里的反模式清单，沉淀为预提交钩子（如 `grep` 报警裸 `except`、密钥、个人路径）。
