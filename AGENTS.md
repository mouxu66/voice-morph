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
   `D:\变声\.workbuddy-ai\memory\YYYY-MM-DD.md`（跨会话长期结论写入同目录 `MEMORY.md`）。
   **可复现、会再犯的坑另写入 `docs\犯错指南.md`**（症状/根因/对策/证据四要素，
   标注 ✅本机实测 / 📚社区库声称 / ❓未验证）。动 `m2_server/wechat_voice.py`
   之前先读该文件。

   > **记忆目录只有这一处**（2026-09-20 已把旧的 `.workbuddy\memory\` 合并进来并删除，
   > 备份留在 `.workbuddy\memory.bak-20260920-204235\`）。两处都会被 `.gitignore`
   > 忽略，不进仓库 —— 但 WorkBuddy 的 `Read`/`Write`/`Edit` 对它们**读写正常**
   > （旧的「一律 BLOCKED」说法已更正，见犯错指南 §8.5）。

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
- 本文件为轻量版，只钉死"提交 + 测试 + 记录"三条；详细技术结论见 `.workbuddy-ai\memory\MEMORY.md`。

## 桌面端改动如何生效（防误判）

- 桌面端有**两种运行形态，生效路径完全不同**（2026-09-14 读 `web/electron/backend.cjs` 实测确认；此前本文写的"一律优先命中 `D:\变声` 源码根"**只对源码模式成立**）：
  - **源码模式**（`npm run electron`，`app.isPackaged === false`）：`resolveProjectRoot()` 候选第一项是 `__dirname/../..` = `D:\变声` → 后端跑 `D:\变声\m2_server\server.py`，前端加载 `D:\变声\web\dist\index.html`。**改完重启即生效。**
  - **安装版**（`变声工坊.exe`，`app.isPackaged === true`）：`resolveProjectRoot()` **直接 early-return 包内 `resources/backend`**，`frontendHtmlCandidates()` 也只返回 `resources/backend/web_dist/index.html` —— **绝不回退 `D:\变声` 源码根**（防止自动更新装了新包却仍读旧源码）。
    - **⚠️ 更正（2026-09-20 读 `backend_autosync.py:105-123` 实测）**：本文此前写的「改 `m2_server`/`web` 仍会生效，因为后端启动时 `backend_autosync.py` 会把 `D:\变声` 的 `m2_server`/`tools`/`web/dist` 镜像进**包内副本**」**是错的**。`sync_backend_copy()` 的目标恒为 `project_root / "voice-morph-desktop" / "resources" / "backend"`，即**源码根下的 staging 目录**；而安装版形态下 `root` 本身就是 `resources/backend` → 命中第 117 行的 early-return（`"安装版运行形态（源码即副本），跳过"`）→ **autosync 什么都不做**。
    - 所以：改 `m2_server/*.py` 后**重启安装版不会生效**，必须显式同步（见下方 `sync_backend.ps1 -TargetRoot` 那条），改前端仍走 `cd web && npm run ship`。
  - **但 `web/electron/*.cjs`（主进程）不在镜像范围内**——它只活在 app.asar 里。改主进程（`pet.cjs`/`backend.cjs`/`alt-hint.cjs`/`setup-ipc.cjs` 等）**必须重打 asar**；重启旧包无效，且会留下"前端文案已更新、主进程行为还是旧的"的半新半旧状态。
    - **发版**走 `npm run electron:build`（electron-builder，重跑 tsc+vite+打包+安装器）。
    - **只想让用户立刻用上**则走**定向重打**（2026-09-17 实测，约 1 分钟，不必重装）：
      ```bash
      # 前提：应用必须已完全退出（asar 被占用时替换会失败）
      ASAR="C:/Users/mouxu/AppData/Local/Programs/voice-morph-desktop/resources/app.asar"
      cp "$ASAR" "$ASAR.bak-$(date +%Y%m%d-%H%M%S)"        # ① 一定要先备份
      cd web && mkdir -p D:/tmp/asar && \
        node node_modules/@electron/asar/bin/asar.js extract "$ASAR" D:/tmp/asar/x
      cp ../web/electron/{main,alt-hint,pet-actions}.cjs D:/tmp/asar/x/electron/   # ② 只换改动的
      node node_modules/@electron/asar/bin/asar.js pack D:/tmp/asar/x D:/tmp/asar/new.asar
      # ③ 换前必须验：list 条目数与原包一致 + diff 无差异 + 解包复核内容
      cp D:/tmp/asar/new.asar "$ASAR" && md5sum "$ASAR" D:/tmp/asar/new.asar       # ④ 必须一致
      ```
      坑：① `asar extract-file` 在本沙箱取不到输出（0 字节），要验就整包解出来看；
      ② **别用 `/tmp`**（MSYS 会解析成 `D:\tmp`，路径对不上就白解一场），用显式 `D:/tmp`；
      ③ 包内主进程路径是 `\electron\*.cjs`（不是 `web/electron`）；
      ④ 这一步**只换主进程 .cjs**，前端 `dist/` 与 `resources/backend/` 不受影响，别顺手覆盖。
- **★ 改完前端只想让它「在桌面应用里生效」——一条命令**：
  ```bash
  cd web && npm run ship      # 构建 → 同步进安装目录 → 逐字节自检
  ```
  然后**完全退出应用再打开**（不是关窗口：桌宠会常驻）。底层是 `tools/ship_frontend.cjs`，
  只做 `web/dist → 安装目录/web_dist`，**完全不碰主进程**，所以不需要重打 asar。
  两个别再踩的点：① `tools/sync_backend.ps1` **不含** `web/dist → web_dist`（只拷 m2_server/tools），
  别指望它同步前端；② **自动更新帮不上**——它查 GitHub Releases，而线上 Releases 为空 →
  每次 404 静默返回，即便发版也是整套安装包替换。
  （源码模式 `npm run electron:dev` 改完重启即生效，用不上这条。）
- `tools/sync_backend.ps1` 与 `resources/backend/{m2_server,web_dist}` 副本：⚠️**这是源码根下的 staging 目录，不是"已安装的那份"**。已安装的桌面端只读 `%LOCALAPPDATA%\Programs\voice-morph-desktop\resources\backend`，而 `backend_autosync.py`（后端启动时自动跑，`VM_BACKEND_AUTOSYNC=0` 可关）**只写 staging、不写它** → 装好的那份会**悄悄过期**（2026-09-18 实测过一次，见 `docs/犯错指南.md` §2.34）。给已安装的那份更新必须**显式指定目标**：
  ```bash
  $dst = "$env:LOCALAPPDATA\Programs\voice-morph-desktop\resources\backend"
  & tools\sync_backend.ps1 -WhatIfSync -TargetRoot $dst   # 先看会动什么
  & tools\sync_backend.ps1           -TargetRoot $dst     # 再真同步
  python tools/verify_backend_sync.py                     # 只读核验：逐字节比对 + 报「不一致/缺失/多余」
  ```
  注意 `sync_backend.ps1` 是**只拷不删**（没有 autosync 的 `f.unlink()` 镜像清理），源里删掉/改名过的文件会**残留在副本里** → 用 `verify_backend_sync.py` 的「多余=N」查。**别用 `md5sum` 手工比对**（§2.28 的 `\` 前缀假红/假绿）。
- **桌宠的 `web/electron/pet/pet.html`（+ `preload.cjs`）是渲染侧文件，不在 asar 里，安装版从磁盘读**：`pet.cjs:27` 的 `PET_DIR` 先试 `resolveProjectRoot()/web/electron/pet`，命中就用它，否则才回退 asar 内置副本。安装版 `resolveProjectRoot()` = `resources/backend`，所以**只要 `resources/backend/web/electron/pet/pet.html` 存在就优先读它 → 改完直接拷一份即热替，不必重打 asar**。⚠️ 但它**不在** `backend_autosync.py` 的镜像范围（只有 `m2_server`/`tools`/`web/dist`），且本机 `D:\变声\voice-morph-desktop\` 为空（无 staging 副本，autosync 恒跳过），所以**每次改 pet.html 都要手动拷**：
  ```bash
  cp D:/变声/web/electron/pet/pet.html \
     "C:/Users/mouxu/AppData/Local/Programs/voice-morph-desktop/resources/backend/web/electron/pet/pet.html"
  python tools/verify_backend_sync.py   # 只读核验；pet 也在比对范围内（10 个文件）
  ```
  ⚠️ **别用 `md5sum` 比对**：Git Bash 的 `md5sum` 遇到含反斜杠的 Windows 路径会在哈希前加 `\` 前缀，一侧相对一侧绝对时**全假红**（68 个 .py 全报不一致），两侧都用绝对路径时**全假绿**（更危险，会把混装放过去）——见 `docs/犯错指南.md` §2.28。要手工比就用 `cmp -s A B`。
  需要重打包的只有 `web/electron/*.cjs` 这类**主进程**文件 —— 别把两者混为一谈（`docs/犯错指南.md` 速查表第 25/31 条）。
- Electron 主进程源码在 `web/electron/*.cjs`（模块化），现役 app.asar 由 `npm run electron:build`（electron-builder）从 web/ 构建；**`.asar_tmp/` + `repack_asar.cjs`/`extract_asar.cjs`/`probe_asar.cjs`/`tools/verify_asar_repack.cjs` 是 2026-09-03 模块化重构之前的过时流程，已于 2026-09-13 全部删除**（它们会拿 46KB 旧单体主进程覆盖现役装配层）。`web/electron` 里剩余的 `smoke-loadpath.cjs` 是现役的加载自检。
- 常见误判（2026-09-14 实测澄清）：
  - "重启就生效" —— **只对 `m2_server`/`web` 成立**（外加 `web/electron/pet/pet.html` 这类渲染侧磁盘文件，但需手动拷进安装版，见上一条）。主进程改动不重打包就永远不生效。
  - 判断某份构建到底含不含某改动，**不要猜，直接验指纹**：`grep -c "<新符号>" <安装目录>/resources/app.asar`（asar 内文件内容为原文，可直接 grep）。本次即靠 `shouldShowPet` 计数 0 vs 8 区分出「安装版 0.2.2 未含改动」与「打包版 0.2.3 已含改动」。同理可查包内 `resources/backend/m2_server/*.py` 与 `web_dist/assets/*.js`。
