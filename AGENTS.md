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

## 袋鼠音色素材铁律（2026-09-05 用户拍板，所有 agent 必须遵守）

1. **袋鼠骑士训练素材只认两个自录视频**：`video_260828_110637`（45 条切片/2.7min）+ `video_260828_105338`（28 条/1.9min），合计 **73 条 / 4.5 分钟**。这两个才是袋鼠本音的最准确来源。
2. **以下 B 站下载素材一律禁止进入任何袋鼠训练集**：神人の外卖（5）、美团袋鼠视频合集、老板的胆子真是肥嘟嘟的、feidudu_merged。文件留存于 `media/raw_videos/` 但仅作存档。
3. 干净训练目录：`D:/RVC/dataset_raw/kangaroo_v2/`（73 wav，22050Hz 单声道，已就绪）；切片源只认 `media/clips/video_260828_110637_*` 与 `media/clips/video_260828_105338_*` 两个前缀。
4. **RVC 重训不需要任何文字标注**（voice-to-voice，只要音频）；需要 ref_text 的是 TTS 路线（已废弃）。
5. 参考音 `media/voicebank/kangaroo/reference.wav` 取自 video_260828_105338_027（自录素材），仍有效。

## 备注

- 技能源目录 `agents\skills\` 保留不动（另一个 agent 框架使用）；本项目的可用副本在 `.workbuddy\skills\`。
- 本文件为轻量版，只钉死"提交 + 测试 + 记录"三条；详细技术结论见 `.workbuddy\memory\MEMORY.md`。

## 桌面端改动如何生效（防误判）

- 本机桌面端（变声工坊.exe）经 `electron/main.cjs` 的 `resolveProjectRoot()` **优先命中 `D:\变声` 源码根**：后端直接跑 `D:\变声\m2_server\server.py`，前端优先加载 `D:\变声\web\dist\index.html`。**改完 m2_server 或 web 后：`cd web && npx vite build`，然后重启桌面端即生效**——不需要 sync_backend.ps1，也不需要 repack_asar.cjs。
- `tools/sync_backend.ps1` 与 `resources/backend/m2_server` 副本、`resources/backend/web_dist` **只服务于分发到别的机器的安装包**（resolveProjectRoot 回退路径）；`repack_asar.cjs` 重打的 asar 内 dist 仅是前端第三兜底。改完源码顺手跑一次 sync_backend.ps1 保持分发副本不落后即可。
- 常见误判：以为"重启无效是因为桌面端跑内嵌副本"——本机不成立，先查根因再下结论。
