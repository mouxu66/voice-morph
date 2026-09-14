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
   **可复现、会再犯的坑另写入 `docs\犯错指南.md`**（症状/根因/对策/证据四要素，
   标注 ✅本机实测 / 📚社区库声称 / ❓未验证）。动 `m2_server/wechat_voice.py`
   之前先读该文件。

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
2. **第三方下载素材一律禁止进入任何袋鼠训练集**（共 4 个来源，均为 B 站下载，含平台吉祥物官方视频与 UP 主二创）。文件留存于 `media/raw_videos/` 但**仅作存档**：不得用于训练、不得再分发、不得作为参考音。具体清单见本机 `media/raw_videos/` 与 `docs/清理清单-2026-09-07.md`（**标题不入库**，2026-09-13 开源前脱敏）。
3. **判定以白名单为准**：只有上面第 1 条那两个前缀（`media/clips/video_260828_110637_*`、`media/clips/video_260828_105338_*`）的切片可以进训练集；其余一律排除，无需逐个辨认来源。干净训练目录 `D:/RVC/dataset_raw/kangaroo_v2/`（73 wav，22050Hz 单声道，已就绪；RVC 根目录可用 `VM_RVC_ROOT` 覆盖）。
4. **RVC 重训不需要任何文字标注**（voice-to-voice，只要音频）；需要 ref_text 的是 TTS 路线（已废弃）。
5. 参考音 `media/voicebank/kangaroo/reference.wav` 取自 video_260828_105338_027（自录素材），仍有效。

## 备注

- 技能源目录 `agents\skills\` 保留不动（另一个 agent 框架使用）；本项目的可用副本在 `.workbuddy\skills\`。
- 本文件为轻量版，只钉死"提交 + 测试 + 记录"三条；详细技术结论见 `.workbuddy\memory\MEMORY.md`。

## 桌面端改动如何生效（防误判）

- 桌面端有**两种运行形态，生效路径完全不同**（2026-09-14 读 `web/electron/backend.cjs` 实测确认；此前本文写的"一律优先命中 `D:\变声` 源码根"**只对源码模式成立**）：
  - **源码模式**（`npm run electron`，`app.isPackaged === false`）：`resolveProjectRoot()` 候选第一项是 `__dirname/../..` = `D:\变声` → 后端跑 `D:\变声\m2_server\server.py`，前端加载 `D:\变声\web\dist\index.html`。**改完重启即生效。**
  - **安装版**（`变声工坊.exe`，`app.isPackaged === true`）：`resolveProjectRoot()` **直接 early-return 包内 `resources/backend`**，`frontendHtmlCandidates()` 也只返回 `resources/backend/web_dist/index.html` —— **绝不回退 `D:\变声` 源码根**（防止自动更新装了新包却仍读旧源码）。改 `m2_server`/`web` 仍会生效，因为后端启动时 `backend_autosync.py` 会把 `D:\变声` 的 `m2_server`/`tools`/`web/dist` 镜像进包内副本。
  - **但 `web/electron/*.cjs`（主进程）不在镜像范围内**——它只活在 app.asar 里。改主进程（`pet.cjs`/`backend.cjs`/`alt-hint.cjs`/`setup-ipc.cjs` 等）**必须 `npm run electron:build` 重打 asar 再装新包/换 exe**；重启旧包无效，且会留下"前端文案已更新、主进程行为还是旧的"的半新半旧状态。
- `tools/sync_backend.ps1` 与 `resources/backend/m2_server` 副本、`resources/backend/web_dist`：对**安装版**而言这是 `resolveProjectRoot()` 的**唯一**路径（见上），对源码模式而言才是兜底副本。**分发副本现在无需手动同步**：后端启动（= 每次打开桌面端）会自动把 `m2_server`/`tools`/`web/dist` 镜像到副本（`m2_server/backend_autosync.py`，`VM_BACKEND_AUTOSYNC=0` 可关）；sync_backend.ps1 仅剩手动应急用途。
- Electron 主进程源码在 `web/electron/*.cjs`（模块化），现役 app.asar 由 `npm run electron:build`（electron-builder）从 web/ 构建；**`.asar_tmp/` + `repack_asar.cjs`/`extract_asar.cjs`/`probe_asar.cjs`/`tools/verify_asar_repack.cjs` 是 2026-09-03 模块化重构之前的过时流程，已于 2026-09-13 全部删除**（它们会拿 46KB 旧单体主进程覆盖现役装配层）。`web/electron` 里剩余的 `smoke-loadpath.cjs` 是现役的加载自检。
- 常见误判（2026-09-14 实测澄清）：
  - "重启就生效" —— **只对 `m2_server`/`web` 成立**。主进程改动不重打包就永远不生效。
  - 判断某份构建到底含不含某改动，**不要猜，直接验指纹**：`grep -c "<新符号>" <安装目录>/resources/app.asar`（asar 内文件内容为原文，可直接 grep）。本次即靠 `shouldShowPet` 计数 0 vs 8 区分出「安装版 0.2.2 未含改动」与「打包版 0.2.3 已含改动」。同理可查包内 `resources/backend/m2_server/*.py` 与 `web_dist/assets/*.js`。
