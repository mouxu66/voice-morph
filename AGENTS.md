# AGENTS.md · 变声项目 AI 协作约定

> 本文件约束所有在本项目工作的 AI agent（WorkBuddy / Codex / Claude 等）。
> 每次完成改动前，先读完本文件并遵守下列铁律。

## 铁律（每次改动必做）

1. **改动即提交**：每完成一次逻辑改动，必须创建一个对应的 Git commit。
   使用 conventional commit 格式（`feat` / `fix` / `docs` / `refactor` / `test` / `chore` …）。
   目的：便于追踪与回滚。
2. **改动即补测**：每次改动后必须编写或更新相关测试，并在交付给用户前，
   确保所有测试与验证全部通过。
3. **踩坑必记**：重要决策、环境坑、训练结论写入
   `D:\变声\.workbuddy\memory\YYYY-MM-DD.md`（跨会话长期结论写入同目录 `MEMORY.md`）。

## 可用技能（已装入 `.workbuddy\skills\`）

| 技能 | 用途 |
|---|---|
| `git-commit` / `conventional-commit` | 规范化提交（铁律 1） |
| `pytest-skill` | Python 单元测试（铁律 2） |
| `rag-implementation` | RAG 检索增强实现 |
| `security-review` | 安全审查 |
| `deepseek-harness` | DeepSeek 调用封装 |
| `paper-fraud-audit` | 论文造假审计 |
| `alembic` | 数据库迁移 |
| `frontend-design` / `design-taste-frontend` / `web-design-guidelines` | 前端/UI 设计 |
| `vercel-react-best-practices` | React/Next.js 性能最佳实践 |
| `dsh-delegate` | 子任务委派 |

> 仅 `git-commit`、`conventional-commit`、`pytest-skill` 与本文件铁律直接相关；
> 其余技能在相关任务出现时由 agent 自动调用。

## 安全红线

- 绝不提交密钥（`.env` / `credentials.json` / 私钥）。
- 破坏性 git 操作（`--force` / `reset --hard` / 强推 main）需用户明确授权。
- 不跳过 git hooks（`--no-verify`），除非用户要求。

## 备注

- 技能源目录 `agents\skills\` 保留不动（另一个 agent 框架使用）；本项目的可用副本在 `.workbuddy\skills\`。
- 本文件为轻量版，只钉死"提交 + 测试 + 记录"三条；详细技术结论见 `.workbuddy\memory\MEMORY.md`。
