# 代码审查标准（Code Review Guide）

> 适用范围：`变声` 项目全栈代码（Python 后端 `m2_server`、RVC/推理、TTS 流式、前端 `web`、Electron 主进程、移动端 `mobile`）。
> 配套文档：`CODE_REVIEW_PROCESS.md`（流程）、`CODE_REVIEW_CHECKLIST.md`（作者自审清单）、`PULL_REQUEST_TEMPLATE.md`（PR 模板）。
> 红线来源：`AGENTS.md` 铁律（改动即提交 / 改动即补测 / 踩坑必记）+ 安全红线（绝不提交密钥）。

---

## 0. 严重级别约定

每条审查意见必须标注级别，与「火眼眼」评审格式一致：

| 级别 | 含义 | 处理要求 |
|---|---|---|
| 🔴 **阻断（Blocker）** | 安全漏洞、数据丢失/损坏、并发竞态、破坏契约、关键路径无错误处理 | 合入前**必须**清零 |
| 🟡 **建议（Suggestion）** | 缺少输入校验、命名/逻辑歧义、重要行为无测试、性能/N+1、明显重复 | 应修；可与作者协商降级 |
| 💭 **提示（Nit）** | 风格不一致（无 linter 兜底时）、小命名、文档缺口、可选写法 | 可选，不阻塞 |

**原则**：一条评论只讲一件事，给「为什么」+「建议改法」。先肯定亮点，再指出问题。

---

## 1. 通用审查维度（所有语言）

### 1.1 正确性
- 逻辑是否真的实现了需求？边界条件（空输入、None、0、负数、超长）是否处理？
- 是否破坏了既有 API/契约（路由签名、返回结构、配置字段、数据库 schema）？改动是否向后兼容？
- 字符串/路径拼接、浮点比较、时区、编码（本项目音频多为 22050Hz 单声道 wav）是否正确处理？

### 1.2 安全（🔴 优先）
- 用户输入是否经过校验/转义？SQL 用参数化、命令用白名单、外部值不进 `eval`/`exec`/`pickle.loads`/`yaml.load`（yaml 须 `safe_load`）。
- **密钥红线**：`.env` / `credentials.json` / 私钥 / GitHub `gho_` 令牌**绝不可进入版本库**。审查时 `git diff --cached` 扫一遍新增文件，确认 `.gitignore` 已覆盖。
- 网络请求：外部 URL 是否可能被 SSRF（如用户传入本地地址）？是否带超时与重试上限？
- 文件路径穿越：用户提供的 `voice_id` / 文件名是否做白名单（参考 `market_images.py` 的 `白名单防穿越`）？

### 1.3 错误处理（本项目头号问题）
> 实测痛点：`m2_server/rvc_live.py` 有 22+ 处 `except Exception`，多数静默 `pass`/`continue` 不记录，**故障不可观测**。

- 🔴 禁止静默吞异常：`except Exception: pass` 或只 `continue` 不记日志 = 阻断级。
- 捕获应**尽量窄**（具体异常类型），不要一揽子 `except Exception` 包住不相干代码。
- 关键路径失败必须返回明确错误 / 写日志 / 触发降级（如实时 RVC 加载失败应降级 TTS 直出并记 `STATE.rvc_error`）。
- 资源清理用 `try/finally` 或上下文管理器；不要在异常分支泄漏文件句柄 / 子进程 / GPU 显存。

### 1.4 并发与状态
> 实测痛点：`m2_server` 有 18 处 `global` 可变状态；`cascade_stream.py` 用 `threading.Lock` + 常驻 `load_vc`。

- 🔴 共享可变状态（全局 dict、`STATE`、模型句柄）必须有锁或原子操作保护；审查锁的**覆盖范围**是否包含全部读写。
- 常驻推理资源（RVC engine、TTS model）必须 `warmup()` 完成后才对外服务；首次请求前未完成预热会首句卡顿数秒。
- 流式/实时链路注意取消与背压：客户端断开后生成循环是否及时退出，避免积压。
- 线程/进程不要阻塞事件循环（GIL 友好）；耗时推理放线程或进程池。

### 1.5 性能
- 避免 N+1（循环内查库/查文件）；批量与缓存优先。
- 大文件 / 长音频处理避免整段读入内存；注意采样率转换与重采样开销。
- 不要重复加载大模型；复用单例（`offline_vc_infer.load_vc` / `RvcEngine`）。

### 1.6 可维护性 / 测试
- 函数/模块是否单一职责？是否有「上帝函数」？命名是否表意？
- **重要行为必须有测试**：后端改完必须补/更新 `m2_server/tests/` 下用例（铁律 2）。
- 魔法数字（采样率、端口、超时常量）是否提取为具名常量？
- 导入副作用：本项目 `RVC/configs/config.py` import 时严格 parse `sys.argv`、`setup_env` 会 `chdir` 到 RVC 根——调用方路径必须绝对，且不要在生产模块顶层 import 触发副作用。

### 1.7 破坏性文件操作（本项目特殊坑）
> 本环境 `shutil.rmtree` 被 safe-delete 包装成「移回收站」，且批量删触发 bulk guard。

- 🔴 删除/清理逻辑**不要假设永久删除**。要真正释放空间用 `tools/hard_delete.py`（先 truncate0 再删）。
- 测试里的临时目录清理必须设 `CODEBUDDY_SAFE_DELETE_ENABLED=0`，否则 `shutil.rmtree` 被拦导致用例随机失败。
- 不要写 `.ps1`/`.bat` 处理非 ASCII 路径（编码损坏）。

---

## 2. 分模块审查清单

### 2.1 Python 后端 `m2_server`
- [ ] 新增/改动 API 在 `server.py` 注册且路由、参数、返回结构一致；错误返回统一格式。
- [ ] 异常处理符合要求（1.3），无静默吞。
- [ ] 日志用 `logging` 而非 `print`（桌面端子进程里 print 输出丢失）；至少 3 个文件已规范，其余逐步迁移。
- [ ] `global` 状态变更有锁保护（1.4）。
- [ ] 网络/文件下载（market_*）带超时、重试、路径白名单。
- [ ] 改动后 `m2_server/tests/` 有对应用例，且全量回归通过（见流程文档命令）。
- [ ] 不提交 `.env` / 密钥；不写死绝对路径到个人目录（`D:/...mouxu...`）。

### 2.2 RVC / 离线变声推理
- [ ] `load_vc` / `RvcEngine` 复用单例，不重复加载。
- [ ] `prosody=keep|relay` 开关语义正确，relay 在降噪后、RVC 前中转。
- [ ] index 特征库路径与 `rvc_common.find_index` 解析正确。
- [ ] GPU 显存：推理完是否释放中间张量；长连接是否常驻不重复 load。

### 2.3 TTS 流式 `qwen3_tts_service.py` / `cascade_stream.py`
- [ ] 真·流式 `generate_voice_clone_streaming` 正确逐块 yield `(chunk, sr, timing)`，首包 TTFA 在目标区间（cs=8~12 → 0.5~0.7s）。
- [ ] 采样率恒 24000，长度前缀帧协议不变 → cascade 客户端零改。
- [ ] 声纹 `create_voice_clone_prompt(x_vector_only_mode=True)` 用目标音色自身 ref，无则回退 `tts_models/ref/meituan_rat_002.wav`。
- [ ] 加载/预热失败降级 TTS 直出并记状态；warmup 在 serve 前完成。
- [ ] 客户端断开能及时取消生成循环。

### 2.4 前端 `web`（React + TypeScript）
> 现状：`package.json` 仅有 `tsc -b && vite build`，**无 test/lint 脚本**——前端缺自动化门禁，审查须更严格人工把关。

- [ ] 改 TS 后必须跑完整 `npm run build`（含 `tsc -b`）通过；不遗留 `any` 绕过类型检查。
- [ ] 图片/音频 `src` 必须过 `mediaUrl()`（file:// 生产环境要拼 `http://127.0.0.1:8000`）。
- [ ] 报错统一走 `notify.tsx` + `friendlyError()`；不裸 `console.error` 吞错。
- [ ] 状态管理（zustand/context）避免不必要的全局重渲染；列表/大数组用 memo 与分页。
- [ ] 新增交互组件有基本渲染/交互自测记录（前端暂无单测，作者需在 PR 描述贴验证截图）。

### 2.5 Electron 主进程 `web/electron/*.cjs`
- [ ] 不依赖被注入的环境变量（`ELECTRON_RUN_AS_NODE` / `NODE_OPTIONS`）；如需干净启动用 `env -u` 剥离。
- [ ] IPC 通道注册齐全；`ipcMain.handle` 内异常要 catch 并返回错误，不让渲染进程挂死。
- [ ] `resolveProjectRoot()` 优先命中 `D:/变声` 源码根；分发副本由 `backend_autosync.py` 自动镜像，不要手改 `resources/backend` 副本。
- [ ] 不要跑 `.asar_tmp/` / `repack_asar.cjs` / `extract_asar.cjs`（2026-09-03 重构前的过时流程）。

### 2.6 移动端 `mobile`（Expo / RN）
- [ ] 低延迟音频链路（mic → PC 流式 → playback）不阻塞 UI 线程；注意低端机（荣耀 Play 50 Plus）AnTuTu ~52 万。
- [ ] 权限（麦克风/音频）在运行时请求并优雅降级。
- [ ] 网络重连与背压；弱网不无限积压。

---

## 3. 审查意见格式（每条都这么写）

```
🔴 安全：路径穿越风险
文件 market_images.py:88 用户传入的 voice_id 直接拼进文件路径。

为什么：攻击者可传 "../" 读取任意文件。
建议：用白名单校验 voice_id 字符集，或 os.path.realpath 后确认仍在允许根目录内。
```

```
🟡 可维护性：异常过于宽泛
rvc_live.py:302 except Exception 包住整段，静默 continue。
为什么：推理失败被吞，排查只能靠猜。
建议：只捕获预期异常（如 RuntimeError），其余上抛；失败分支 logging.error 记录上下文。
```

---

## 4. 落地建议（给维护者）

1. **先治头号问题**：把 `rvc_live.py` 等高频静默 `except` 作为首批「阻断级」专项清理，配一个 `grep` 预提交钩子报警。
2. **前端补门禁**：加 `vitest` + `tsc --noEmit` 脚本，至少让 `npm run build` 成为 PR 必过项。
3. **后端加预提交/CI**：`CODEBUDDY_SAFE_DELETE_ENABLED=0 pytest m2_server -q` 作为最小 CI；无 CI 时由作者本地跑并在 PR 贴结果。
4. **AI 辅助初审**：每个 PR 先用「火眼眼」专家做一轮自动审查，人工只复核 🔴 与争议 🟡。
