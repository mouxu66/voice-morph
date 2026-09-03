# 变声项目 AI 协作约定（CodeBuddy 规则）

> 本文件由 `.codebuddy/rules/` 自动加载，约束 CodeBuddy 在本项目的行为。
> 规范原文见项目根 `AGENTS.md`（WorkBuddy 与 AGENTS.md 约定工具共用同一份铁律）。

## 铁律（每次改动必做）

1. **改动即提交**：每完成一次逻辑改动，必须创建一个对应的 Git commit。
   使用 conventional commit 格式（`feat` / `fix` / `docs` / `refactor` / `test` / `chore` …）。
   目的：便于追踪与回滚。
2. **改动即补测**：每次改动后必须编写或更新相关测试，并在交付给用户前，
   确保所有测试与验证全部通过。
3. **踩坑必记**：重要决策、环境坑、训练结论写入
   `D:\变声\.workbuddy\memory\YYYY-MM-DD.md`（跨会话长期结论写入同目录 `MEMORY.md`）。

## 可用技能（已接 `agents/skills/`，经 `.codebuddy/skills` 链接）

`git-commit` / `conventional-commit`（提交）、`pytest-skill`（测试）、
`rag-implementation`、`security-review`、`deepseek-harness`、`paper-fraud-audit`、
`alembic`、`frontend-design` / `design-taste-frontend` / `web-design-guidelines`、
`vercel-react-best-practices`、`dsh-delegate`。

## 安全红线

- 绝不提交密钥（`.env` / `credentials.json` / 私钥）。
- 破坏性 git 操作（`--force` / `reset --hard` / 强推 main）需用户明确授权。
- 不跳过 git hooks（`--no-verify`），除非用户要求。
