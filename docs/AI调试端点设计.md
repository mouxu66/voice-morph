# AI 调试端点 · 设计与门禁同步清单

> 状态：**两期均已实现** —— 只读面（2026-10-05）+ 白名单写动作与审计面板（2026-10-06）。
> 2026-10-06 实施中改了两处设计之外的判断，都记在 §十一「实施后的偏离」。
> 读者：评审者 + 实现者（人/agent）。
> 缘起：用户问「要不要给 AI 专门开发一个端点来调试/控制这个程序，用户出问题让 AI 帮忙」。
> 结论：**要，但只读优先、写入白名单、默认全关**。
> 2026-10-05 两处拍板：① 只读快照**并入 `core.system` 零门槛可用**（不做成可选插件）；
> ②「复制诊断报告」按钮放**设置页环境体检卡片旁**。两项均已反映到下文与 §八/§九。
>
> 相关先例（改这份设计前先读）：
> `docs/插件化设计.md`（一切皆插件）、`docs/遥控页二维码方案.md` §五（`hook.remote`
> 是唯一的「编排层插件」先例）、`docs/手机遥控页.md`（既有的对外控制面）。

---

## 一、真问题与不解决的问题

**真问题不是「AI 控制不了应用」，而是「AI 看不见应用内部的状态」。**
`tools/desktop-control/` 已经给了 AI 眼睛和手（截图 + UIA 点击），但：

- 像素路径看不见**为什么**：这次为什么降级、`block_reason` 是什么、哪个能力加载失败、
  上一条发送的 warning 落在哪；
- Electron/Chromium 窗口 UIA 树是空壳（自家「变声工坊」、桌宠都是），
  「截图 + 坐标」能点按钮但答不了上面这些问题；
- 用户电脑不在身边时，AI 连像素都没有，只剩用户口述。

所以本端点的价值排序是：**状态可读（排障）> 有限动作（修复）> 屏幕控制（已有，不做）**。

### 明确不做（避免下次重开讨论）

- ❌ **远程 shell / 任意命令执行**。这是红线，任何形态（含「传一段 Python 表达式」）都不做。
- ❌ **第二套功能 API**。动作一律**转发现有函数/端点** —— 本项目已吃过「死参数」的亏
  （`cascade_stream` 给 `/tts` 发了没人读的 `fast`，`docs/犯错档案-工程.md` §8.38）。
- ❌ **LAN 暴露**。第一期只认回环；将来若做，也要单独开关 + token，且**写动作永不对 LAN 开**。
- ❌ **不可逆的外部副作用**（微信发送、上传、分享）与**删除类**操作 —— 见 §五拒绝清单。
- ❌ **把 desktop-control 搬进端点**。像素操作留在 `dc.ps1` / `pet.py`；端点补的是状态。
- ❌ **不做实时流/持续轮询**。每次「看」都是一次快照，同 `dc.ps1 shot` 的哲学。
- ❌ **不绑 MCP / 任何框架**。端点形状稳定之后，谁想包 MCP server 是下一步的事，
  现在绑框架等于在未定型的接口上加一层要同步的壳。

---

## 二、形态（★ 2026-10-05 拍板后：读写分层）

### 2.1 第一期（只读）并入 `core.system`

| 决策 | 取值 | 理由 |
|---|---|---|
| 归属 | `core.system`（`routers` 加一项 `ai_api`） | 用户拍板：只读快照**零门槛可用** —— 报障时用户还没装/开任何东西就该能点「复制诊断报告」（§九第 1 条） |
| 模块 | 新模块 `m2_server/ai_api.py` | 与 `system_api` 分开：AI 面是给外部工具看的**稳定契约**，别混进体检本身；`core.system` 的 `routers` 支持多项 |
| 路由认领 | `plugin.json` 的 `routers` 加 `"ai_api"` | `_validate()` 要求每个 router **恰被一个插件认领**；核心不可关 → 始终挂载 |
| 依赖 | 不 `requires` | 快照按「能力在就采、不在就带原因跳过」工作，关掉微信/桌宠也能用 |

### 2.2 第二期（写动作）另起可选插件 `hook.ai`

写能力的安全等级高于读，**不给它 core 的零门槛**：

| 决策 | 取值 | 说明 |
|---|---|---|
| 插件 id / category / order | `hook.ai` / `hook` / 240 | 与 `hook.wechat` / `hook.remote` 同族（现役最大 order 是 `hook.remote` 的 230） |
| 写端点模块 | 新模块 `ai_actions_api.py`（`hook.ai` 认领） | ⚠️ **不能再把 `ai_api` 认领给第二个插件**（`_validate()`：一个 router 只能一个主人）——所以写动作必须另起模块，关闭 = 不挂载 = 404 |
| 动作表 | 中性模块 `ai_actions.py`（**无路由**） | 读侧 `/api/ai/actions`（core）与写侧执行器共用**唯一真相源**；core 侧不许 import 已关闭插件的模块，所以表不能放在 `ai_actions_api.py` 里 |
| 三重门 | 见 §三 3.2 | 插件启用 + `VM_AI_CONTROL=1` + `VM_AI_TOKEN`，fail closed |

> 「只读 core、写动作插件」这条分层的代价：多一个模块与一次路由认领，换来的是
> 读面零门槛（用户报障即时可用）与写面默认全关（不可逆动作要三重钥匙）。

---

## 三、路由与鉴权

### 3.1 三个端点

| 方法 | 路径 | 作用 | 归属 | 阶段 |
|---|---|---|---|---|
| `GET` | `/api/ai/context?format=json\|md&logs=0\|N` | 只读快照（字段见 §四）；`format=md` 返回可直接粘贴的脱敏报告 | `core.system`（新模块 `ai_api`，不可关） | 第一期 |
| `GET` | `/api/ai/actions` | 动作目录：第一期只答「拒绝清单 + 写未启用」；二期加动作 schema | `core.system` | 第一期 |
| `POST` | `/api/ai/actions/{action_id}` | 执行一个白名单动作（body 带参数；强制 `dry_run` 优先） | `hook.ai`（可选插件，默认关） | 第二期 |

### 3.2 鉴权矩阵（★ 比现有 API 更严，这是刻意的）

现有模型是「回环永远放行；设了 `VM_API_TOKEN` 才管 LAN；不设则 LAN 全放行」
（`m2_server/server.py` 的 `_TokenMiddleware`）。**本端点不能沿用**：
快照里含用户路径、最近记录摘要、日志尾部 —— 这些在 LAN 无鉴权下等于对外广播。

| 调用方 | 读（context / actions） | 写（POST actions） |
|---|---|---|
| 本机回环（Electron / curl / agent / vite proxy） | ✅ 放行 | ❌ 需同时满足三个条件（见下） |
| 局域网（手机 / 另一台电脑） | ❌ **默认 403**（即使设了 token 也不放，需将来的 `VM_AI_ALLOW_LAN=1`） | ❌ **永不开放** |
| 公网 Origin 的浏览器页面 | ❌ 403（沿用 `_OriginGuardMiddleware`） | ❌ 403 |
| 插件被用户关掉 | 不适用：只读面归 `core.system`，核心不可关 | ❌ 404（`hook.ai` 未启用） |

写动作的三重门（**fail closed**，任一不满足即拒）：

1. 插件在启用集（默认关）；
2. `VM_AI_CONTROL=1` 环境变量总开关（同 `VM_WARMUP` / `VM_PLUGIN_SEED` 的既有手法，
   检查放**函数内部**，防止 import 期副作用 —— §8.41）；
3. `X-API-Key` / `api_key` 匹配 **`VM_AI_TOKEN`**（**即使回环也要**；未设 token = 写能力不存在）。

> **为什么单开 `VM_AI_TOKEN` 而不复用 `VM_API_TOKEN`**：职责混用会权限扩张 ——
> 为手机开遥控（设了 API token）顺手就把「AI 写动作」的门也开了。两个 token 互不回退。

补充硬化（实现时二选一或都做）：

- `POST` 一律要求 JSON body（触发 CORS 预检，预检被现有 Origin 守卫挡住公网来源）；
- 拒绝来自非回环 socket 的**任何** `POST`（即使 token 正确）；
- 响应头 `Cache-Control: no-store`（诊断快照不该被缓存）。

### 3.3 状态码语义（拒绝也要说清「为什么」）

- `404` —— 插件没开（与其它插件一致）；
- `403` —— token 缺失/不匹配，或 `VM_AI_CONTROL` 未开（detail 里点名缺哪一项）；
- `409` —— 动作被前置条件拒绝（如「核心能力不可关」「仍被 X 依赖」——复用既有守卫的原文）；
- `400` —— 参数不合法（`schema` 里声明过的约束）。

---

## 四、只读快照字段清单（第一期的全部内容）

### 4.1 设计规则（先于字段）

1. **复用不重写**：快照调用的都是既有函数（`system_api.diagnose()`、
   `plugin_manifest.state_of()`、`warmup.status()`、history 读取函数、wechat precheck 函数）。
   在快照里重新实现一遍探测 = 造第二真相源，两边漂移时没人知道该信谁。
2. **绝不 import 重库**：缺 torch 的「轻量预设」下快照必须照常 200
   （同 `remote_api` 的取舍：只读 JSON + 状态文件）。
3. **每处空值必须带原因**：`notes` 段逐条解释「这里为什么是空的」——
   沿用 `60秒魔法时刻.md` 的两条诚实性不变量口径。
4. **脱敏规则固定**：不含 token / 剪贴板 / 聊天内容；`input_text` 一律剔除；
   用户主目录替换为 `%USERPROFILE%`；日志行截断到 200 字符。报告生成器把规则原文
   写进 `redaction` 段（用户能看懂自己交出去的是什么）。
5. **`schema: 1` 版本号**：消费方按此解析；只增字段不升版，删改字段才升。
6. **性能预算（实测，2026-10-05）**：热路径 ≈ 1.4s —— 音频只读枚举（PowerShell
   `diag`）≈ 1.3s 是大头；冷启动首调更慢（`/health` 的 torch 探测 ≈ 2.2s、首次
   import 更多）。**刻意不做缓存**：宁可慢一点，也不报一个陈旧的设备状态
   （报告是用户点一次的动作，不是轮询）。

### 4.2 字段表

```jsonc
{
  "schema": 1,
  "generated_ts": 1760000000,
  "app":        { /* 版本 / 端口 / 安装形态 / python 版本；拿不到给 null + 原因 */ },
  "health":     { /* status, cuda（null=未装 torch，不是故障） */ },
  "plugins":    { /* enabled / disabled / broken(带原因) / loaders 原始账本 */ },
  "diagnose":   { /* 复用 /diagnose：items + skipped + all_ok */ },
  "audio":      { /* 默认输入/输出设备名、CABLE 是否存在、残留巡检结论 */ },
  "warmup":     { /* TTS / RVC 是否已预热 */ },
  "wechat":     { /* 复用 precheck：restart_needed / reason / last_input_device / block_reason */ },
  "history":    { /* 最近 N=20 条：id/ts/kind/voice_id/duration_s/warning/starred/tags */ },
  "errors":     { /* 后端进程内环形缓冲最近 N 条 WARNING/ERROR（截断 200 字符） */ },
  "notes":      [ /* 每条「跳过/拿不到」的原因，机器可读 */ ],
  "redaction":  { /* 已采取哪些脱敏动作 */ }
}
```

| 段 | 来源（复用对象） | 缺失/降级时的表达 |
|---|---|---|
| `app` | `runtime.ROOT`、`cfg.SERVER_PORT`、安装形态探测、`sys.version` | 拿不到（如打包环境无 git）→ `null` + `notes` 说明 |
| `health` | `/health` 的同一实现 | 缺 torch → `cuda: null`（**不是**故障，前端注释口径） |
| `plugins` | `plugin_manifest.by_id/state_of/enabled_ids/disabled_ids` + `plugin_loader.results()` | 清单读不到 → 段内给原因，端点不 500 |
| `diagnose` | `system_api.diagnose()`（直接调用，不重算） | 相关能力全关 → 沿用 `skipped` 逐条原因 |
| `audio` | `audio_api` 的只读诊断函数 | 无设备/无 CABLE → 如实报缺哪个 |
| `warmup` | `warmup.status()` | worker 未起 → `ready:false` + 一句话 |
| `wechat` | `wechat_voice` 的 precheck（只读） | 插件关 → `{ "enabled": false, "reason": "hook.wechat 未启用" }`，不 404 不 500 |
| `history` | history 读取函数（倒序取前 N） | 文件不存在 → 空数组 + 原因；坏行沿用「跳过不 500」 |
| `errors` | **读 Electron 捕获的 `%APPDATA%/voice-morph-desktop/backend.log` 尾部**（只读末尾 64KB、筛疑似错误行、行截断 200 字符；`VM_BACKEND_LOG` 可覆盖路径） | 文件不存在（后端独立由终端直跑）→ 空数组 + `notes` 说明「stdout 即日志」。★ 实现时否掉了设计初稿的 ring buffer：全仓日志就是 `print` 到 stdout（`plugin_loader.report` 的注释原文），装 logging handler 只能捕到零头 —— 那会把「没日志」伪装成「没错误」 |
| `notes` | 现算 | 它就是「诚实性」本身，不许为空数组以外的省略 |
| `redaction` | 静态声明 | — |

### 4.3 报告格式（`format=md`）

同一份数据渲染成 Markdown：用户点「复制诊断报告」→ 粘贴给任意 AI（或客服）。
**这一步不需要开任何写开关**，也不要求用户装额外东西。
报告头部固定写：应用版本 / 时间 / 「本文档由本机生成，已做哪些脱敏」。

---

## 五、动作清单与拒绝清单（第二期）

### 5.1 候选动作（每个都要 schema + dry-run + 前置条件）

| action_id | 复用 | 前置 | 风险 | 第一期放不放 |
|---|---|---|---|---|
| `plugins.enable` / `plugins.disable` | `plugin_manifest.set_enabled`（既有 409 守卫原文返回） | 核心不可关；被依赖不许关 | 低（重启生效，如实返回 `restartRequired`） | ✅ 建议放 |
| `plugins.preset` | `plugin_manifest.apply_preset` | 预设名存在 | 低 | ✅ 建议放 |
| `audio.setup_best` / `audio.restore` | `audio_api` 的一键配置 / 恢复 | **必须成对**：restore 永远可达 | 中（改系统默认设备） | ⏸ 第二期再放 |
| `wechat.preview_text` | `/api/wechat/preview_text`（合成试听，**不发送**） | `hook.wechat` 启用 | 低（吃显存，有既有单飞互斥） | ⏸ 第二期再放 |
| `warmup.trigger` | `warmup` 的预热入口 | 无 | 低（吃显存） | ⏸ 观望 |

**动作响应统一形状**：`{ok, code, message, evidence: {before, after}, duration_ms, audit_id}`。
`evidence` 是「下一步该看哪」的机器可读指针（如 `{"plugins_disabled": [...]}`），
不要塞人话长文（人话在 `message`）。

`dry_run=true` 时只返回「会发生什么」：复用既有 precheck / 守卫试算，
**不产生任何副作用**（含不写审计的执行记录，但写一条 dry-run 记录，见 §六）。

### 5.2 拒绝清单（写进 `GET /api/ai/actions` 的 `denied` 段，带理由）

| 拒绝什么 | 为什么 |
|---|---|
| 任意命令 / 脚本执行、动态求值 | 红线。AI 在**本机**已有终端，不需要借应用开后门 |
| `wechat.send_text` / `send_voice`、任何「发出去」 | **不可逆的外部副作用**：发进的是别人的微信。要发必须人确认，而人确认的界面是应用自己，不是端点 |
| 删除类：音色删除 / `system_storage_clean` / history 批量删 / 市场卸载 / 皮肤卸载 | 破坏性且用户难以恢复；第一期一律拒绝 |
| 文件系统任意读写、任意路径导出 | 越权面太大；产物一律走既有 `media/`、`outputs/` 约定 |
| 改任意系统设置（如「把默认设备设成 X」） | 只允许 `audio.setup_best`+`restore` 这一对；不做任意目标 |

> 拒绝清单是**声明式**的：动作表里没有的 action_id 一律 404（默认拒绝），
> 不靠「记得不要实现它」。

---

## 六、审计与开关

### 6.1 开关（全部默认关 / 空，fail closed）

| 层 | 键 | 缺省 | 作用 |
|---|---|---|---|
| 装/不装 | `outputs/plugins.json` 的 `disabled` 列表 | 默认关 | 关掉 = 路由不挂 = 404 |
| 写总闸 | `VM_AI_CONTROL` | 未设 = 无写能力 | 同 `VM_WARMUP` 手法，检查放函数内（§8.41） |
| 鉴权 | `VM_AI_TOKEN` | 未设 = 写不可用、LAN 读 403 | 不回退到 `VM_API_TOKEN` |
| （将来）| `VM_AI_ALLOW_LAN` | 未设 = LAN 读 403 | 第一期不实现，只在代码里留判据注释 |

### 6.2 审计

- 文件：`outputs/ai_actions.jsonl`（append-only）。
- **为什么与 `history.jsonl` 分开**：history 是「用户的作品」，有 2000 条上限、
  会被用户清理；审计是「外部动作的记录」，不能被「清作品库」顺带清掉，
  也不能混进作品列表。两者生命周期不同。
- 字段：`id / ts / action / params（脱敏）/ dry_run / ok / code / duration_ms /
  actor（取自 X-AI-Actor 头，缺省 unknown）/ audit_ref（关联的 evidence）`。
- **落库 + 有人消费**：这是 §2.23 的教训原文 —— 只落库没人看 = 没做。
  所以第二期必须带一个只读的「AI 操作记录」面板；第一期（无写动作）只有
  dry-run 记录，可以先只有文件 + `notes` 指路。
- **写动作在审计落盘失败时拒绝执行**（fail-closed）：审计是这个端点的存在前提；
  写不进审计的动作不许发生。
- 线程规矩（§2.24）：写审计的路径必须在**起线程那一刻快照**，不许运行时查全局
  `HISTORY_FILE` 式变量 —— 否则测试的 monkeypatch 会在写侧失效，把数据搬进真实 `outputs/`。
- 新文件的路径必须走 `cfg.OUTPUTS_DIR`（导入期早绑定问题，§8.36 / §8.37 ——
  `test_output_isolation.py` 是现成守卫）。

---

## 七、按项目门禁需要同步的每一处（实现时逐条勾）

> 这一节是本稿的重点：本项目的门禁密度很高，「写完功能才发现一堆红」的成本
> 远高于开工前先对账。下面每一条都注明**为什么**。

### 7.1 新增 router / 新增插件必然触发的

> 第一期（只读）**不新增插件**，只是给 `core.system` 加一个模块：第 0、2、6、7 条必做；
> 第 1、3、4、5 条属第二期（新增 `hook.ai`）。

| # | 门禁 / 文件 | 为什么 | 怎么做 |
|---|---|---|---|
| 0 | `m2_server/plugins/core.system/plugin.json` 的 `routers` 加 `"ai_api"` | 每个 router 恰被一个插件认领；不加则该模块无人认领、挂载计划里没有它 | 第一期必做 |
| 1 | `m2_server/plugins/hook.ai/plugin.json`（第二期） | 唯一真相源；`routers` 非空、router 不许被两个插件认领、`requires` 存在且无环 | 按 §二 2.2 的取值；`disable.note` 写清「关了写动作全消失」 |
| 2 | `README.md` 三处手抄计数（能力数 / router 数 / 目录树） | `test_docs_freshness.py` 现算校验，加插件必动 | 按门禁报错信息改 |
| 3 | `m2_server/tests/test_plugin_switch.py` | **在 `--fast` 里**：加一个插件会改「关一个能力跳到谁」的既有样本；`requires` 会给被依赖者加依赖方 | 新增 `ai_api` 的开关用例；样本被改动的按先例**改样本 + 加守卫**，不放宽断言 |
| 4 | `m2_server/tests/test_plugin_manifest.py` / `test_plugin_extras.py` / `test_plugin_deps.py` | 清单结构、extras 与真实 import 对账 | ★ 本设计**零新第三方依赖**（只用 stdlib + 现有 fastapi 栈）——这是硬约束，为的就是不动这组门禁 |
| 5 | `THIRD_PARTY_NOTICES.md` + `tools/audit_licenses.py` | 许可漏登记事后补不回来 | 零新依赖 → 不涉及（写进提交说明，让评审能核对） |
| 6 | `m2_server/tests/test_route_shadowing.py` | 新路由不许与现有重叠；通配路径必须垫后（§8.43） | `/api/ai/actions/{action_id}` 是单段，无通配风险；仍跑一遍确认 |
| 7 | `m2_server/tests/test_tts_payload_contract.py` | 「客户端发的字段 ⊆ 端点读的字段」——死参数的机器判据 | 前端新函数发给 `/api/ai/*` 的每个字段都要被端点真读到 |

### 7.2 新增端点行为要配的测试（写进新测试文件）

| # | 断言组 | 要点 |
|---|---|---|
| 8 | 快照形状与来源对账 | `plugins.broken` 与 `plugin_loader.results()` 对账；`diagnose` 段与 `/diagnose` 逐字段一致（**双向差集**，不是「都存在」——§8.21 的教训） |
| 9 | 鉴权矩阵 | 回环读放行 / LAN 读 403 / 写缺 token 403 / 写缺 `VM_AI_CONTROL` 403 / 插件关 404 —— 五态逐条 |
| 10 | 拒绝清单 | 表外 action_id → 404；`send_*` 类不存在于动作表（**断言不存在**同样要写） |
| 11 | 审计 | `tmp_path` 里落盘、字段齐全、失败时写动作被拒（桩掉写失败） |
| 12 | 脱敏 | 构造含 `input_text`、含用户名路径的历史记录 → 报告里必须**不出现**（负面断言） |
| 13 | ★ 变异验证 | 每条守卫测试都要**注入真违规它会红**（§8.19 / §8.21：错得自洽的守卫比没有更糟）；HTTP 级从 `TestClient` 打进去（§8.43：只从函数层调过的守卫等于没有） |

### 7.3 前端与桌面端（第一期就有 UI）

| # | 门禁 / 动作 | 为什么 |
|---|---|---|
| 14 | `tools/audit_endpoint_ownership.py` | 新前端函数 → 端点归属必须可定位（第一期就要跑；本按钮打的是 `core.system` 的 `/api/ai/*`，**不产生**新的「核心页嵌可关组件」） |
| 15 | `pluginVisible` 门控（第二期） | 二期若把动作按钮/记录面板放进核心页，必须按 `hook.ai` 状态门控，否则关掉插件后按钮还在 → 点一下 404（§8.40、§2.43 的教训） |
| 16 | `web` 三步：`tsc -b` / `npm run lint` / `vitest run` | `tools/check.py` 的 web 步与 ci.yml 逐条对齐；eslint 只在门禁里跑过才不算死脚本（§3.15 / §3.23 族） |
| 17 | 桌面端生效路径 | 只改 `m2_server` + `web/src` → 源码模式重启即生效；安装版后端走 `sync_backend.ps1 -TargetRoot` + `verify_backend_sync.py`；前端走 `npm run ship`；**不涉及**主进程 → 不重打 asar |

### 7.4 文档

| # | 动作 | 时点 |
|---|---|---|
| 18 | 本文进 `docs/README.md` 索引 | 本次提交（已做） |
| 19 | `docs/犯错指南.md` 速查表加一行（「用户报障时先让 AI 打 `/api/ai/context`」） | 落地后 |
| 20 | `docs/插件化设计.md` 加一条增量注记（同 `hook.remote` 的先例：在 §5.0 的「之后的增量」段加，不改历史快照） | 落地后 |
| 21 | `AGENTS.md` 若加一行「排障先打这个端点」 | 落地并稳定后再说，设计稿阶段不动 |
| 22 | 本文里指向 `docs/` 内其它文档的链接与带文件名的 `§N.M` 引用必须可解析（`test_docs_references.py`） | 本次提交前已跑 |

### 7.5 不涉及（写清为什么，防评审时再问）

- **CI workflow**：不改 `.github/workflows/ci.yml`（无新 job、无新依赖）；
  `test_check_ci_fidelity.py` 不受影响。
- **asar**：无 `web/electron/*.cjs` 改动。
- **pet.html 手动拷贝**：不涉及。
- **`web/public/remote.html`（手机遥控）**：**明确不接**本端点 —— AI 面只给本机，
  多一个跨端门控链就多一处会漂的依赖表（`hook.remote` 的 `TABS` 教训）。

---

## 八、分期与验收

### 第一期（只读，独立可交付）—— 并入 `core.system`，零门槛 ✅ 已实现（2026-10-05）

- `GET /api/ai/context`（json + md）、`GET /api/ai/actions`（答拒绝清单 + 「写未启用」）；
- 前端「复制诊断报告」按钮 —— 落在**环境体检面板**里那个已有按钮上（升级而不是新加），
  端点拿不到时降级简版并如实标注「简版」；
- 测试：`m2_server/tests/test_ai_api.py`（14 条）+ `web/src/components/EnvHealth.test.tsx`（2 条）；
- 验收：全量 `python tools/check.py` 绿。

### 第二期（白名单动作 + 审计面板）

- `POST /api/ai/actions/{id}`：`plugins.enable/disable/preset` 三个动作起步；
- 「AI 操作记录」只读面板；
- `audio.*` 与 `wechat.preview_text` 视第一期真机使用情况再定。

---

## 九、拍板记录

1. ~~只读快照也默认关吗~~ **已拍板（2026-10-05）：并入 `core.system` 零门槛可用**。
2. ~~「复制诊断报告」按钮放哪~~ **已拍板（2026-10-05）：设置页环境体检卡片旁**。
3. **仍待拍板：第二期动作是否包含 `audio.setup_best`（改系统默认设备）**——
   推荐先只放 `plugins.*`（零外部副作用），设备类等真机需求。

---

## 十、与现有工具的分工（一张表收尾）

| 场景 | 用什么 |
|---|---|
| 网页（自家前端 / remote.html） | Freebuff 预览面板 / 无头 CDP（`tools/web_shot.py`） |
| Windows 桌面（微信、桌宠、安装版）的**像素**操作 | `tools/desktop-control/`（`dc.ps1` / `pet.py` / `cdp.py`） |
| 应用的**内部状态**与有限修复 | 本端点（`/api/ai/*`） |
| 环境缺什么（装没装 torch/ffmpeg/RVC） | `tools/doctor.py --json` 与 `/api/diagnose`（快照会**复用**它们，不另起炉灶） |

---

## 十一、实施后的偏离（2026-10-06 补记）

实施第一期时实测出三处设计稿没预见到的东西。三条都记在这里，因为
**它们改变了设计的结论，而不只是实现细节**。

### 11.1 🔴 P0：日志里的用户原话会进报告（设计稿的脱敏规则不完整）

设计稿 §4.2 写了 `errors` 段要「截断 200 字符 + 脱敏」，§4.1 第 4 条列了
四条脱敏规则 —— 但**没有一条覆盖「日志正文里嵌着用户说的话」**。

真实现：`m2_server/cascade_stream.py:935` 有
`print(f"[live-asr] 「{text}」 ({...}s)")`，把 **ASR 识别出的用户原话**
写进 stdout → Electron 落 `backend.log` → 本接口读它。而 `_LOG_SUSPECT`
命中「失败/错误/warn」，所以用户在游戏/直播里说的那句抱怨
（「**这局失败了**」「不行」）恰好**极易命中**筛选 —— 讽刺的是，
正因为那是用户抱怨的话，它才更可能被写进报告。

实测（构造 `cascade_stream.py:935` 的真实格式）：

```
输入：[live-asr] 「我银行卡密码是 6222020200112233 这局失败了帮我改一下」 (1.2s)
第一期实现 → 原样出现在 errors.items，账号与原话全部泄漏
```

而第一期的守卫测试假绿：`test_redaction_applies_to_log_lines` 只造了
`[plugin_loader] 不可用: <主目录路径>` 这种**本来就无害**的行，
而 `_redact_str()` 只替换主目录与 token —— 两者都碰不到用户原话。

**修法（已实施）**：

1. 引号包裹的内容整段替换：`「…」`/`『…』` → `「<用户原话已省略>」`。
   刻意**不丢整行**：引号与行尾耗时是诊断信号（发生过一次转写 + 耗时），
   抹掉的只是原话。
2. 号码兜底：身份证 18 位 / 银行卡 16-19 位 / 手机号 11 位，
   两侧 `(?<!\d)`/`(?!\d)` 卡边界 —— 没有边界会把 13 位数字串从中间截断、
   **尾巴原样留下**（半截号码比完整号码更糟：看起来像被部分识别过的真号）。
3. ★ **脱敏在截断之前**。反过来 200 字符的截断会把「」拦腰切断，
   闭合引号没了规则就匹配不上 —— 一个只写在注释里没人验证的顺序漏洞。

新增 4 条守卫（22 条 → 26 条），每条都做过变异验证。**顺带一个方法论教训**：
我第一版写的「反向守卫（数字不被误伤）」在变异验证时**没红** ——
因为它测的压根不是边界的真实作用。边界真正防的是「部分吞噬」，
测试也必须测那个。

### 11.2 回环检查从「手写调用」提到「router 级依赖」

设计稿 §3.2 与原实现都是**每个端点手写** `_require_loopback()`。
致命处：新增端点时忘了写 = 默认对 LAN 开放，而快照里含本机路径与日志尾部
—— 这是「忘了写一行代码」就能造成的后果，不能靠自觉。

已改为 `APIRouter(dependencies=[Depends(_require_loopback)])`，
并加一条**自动遍历守卫**：把所有已挂载的 `/api/ai/*` 路由逐个用 LAN 来源打一遍。
所以将来新增端点漏了保护也会红。

> 这条守卫自己踩了个坑值得记：本项目这个 FastAPI 版本里
> `app.include_router()` 塞进去的是 `_IncludedRouter` **包装器**，
> 它自己的 `.path` 是空串、`.routes` 是 `None`（实测 38 个 router 挂载后
> `app.routes` 里 38 条全是空 path）。第一版直接遍历 `app.routes` 收集到 0 条，
> 而 `assert paths` 那句让它「红」了 —— **红的理由是「没找到路由」而不是
> 「LAN 被放行」**，等于差点把一个恒真断言当成门禁。修法是走
> `original_router.routes`，并且要按端点自己的方法打（拿 GET 打 POST 端点
> 得到 405，那是「测法错了」不是「保护没生效」）。

### 11.3 审计的 fail-closed 顺序：先落 intent，不是执行后补记

设计稿 §6.2 写「写动作在审计落盘失败时拒绝执行（fail-closed）」。
我第一版实现成「执行完再写审计，失败就 503」—— **那不是 fail-closed**：
动作已经发生、副作用已落盘，此时报错只会让调用方以为没执行而重试。
已改为：**执行前先落一条 intent 账本**（落不进就 503，动作不许发生），
执行完再补记一条结果行。结果行落盘失败**不再抛**：此时动作已发生，
再报错会诱发重试，那才是真事故；intent 行至少还在，
它证明了「谁在什么时候试了什么」。

### 11.4 三重门与动作表：两处按 §2.2 落地，编号沿用

- 写端点 `ai_actions_api`（`hook.ai` 认领，order 240）；
  中性动作表 `ai_actions.py`（**无路由**）+ `ai_audit.py`（审计）。
- 动作只有两个：`plugins.enable` / `plugins.preset`（`risk: low`）。
  §5.2 的拒绝清单**原样落地并双向对账**（动作表与拒绝清单不许有交集，
  由 `test_deny_list_and_action_table_are_disjoint` 钉住）。
- `audio.setup_best` / `wechat.preview_text` **按§九第 3 条继续不做** ——
  改系统默认设备要等真机需求。

### 11.5 跨文件污染：`_SKIPPED` 是模块级的，而 `reset_cache()` 管不到它

写第二期时踩到的最后一个坑，症状与本设计稿无关，却一度卡住门禁，值得单记。

**症状**：`test_ai_actions_api.py` 跑完后，
`test_plugin_manifest.py::test_router_never_attempted_counts_as_broken_not_ok`
红成 `{'total': 28, 'ok': 1, 'broken': 27, 'disabled': 0}`。
**只在全量跑时出现**，单跑任一文件都绿（2026-10-06 实测）。

**为什么查错地方**：加探针查 `plugin_loader.results()`，账本确实是空的
（`ok=0`）、`disabled_ids()` 也是空集 —— 于是"我没污染"的结论看起来成立。
**真凶不在账本里**：`plugin_manifest._SKIPPED` 是个**模块级集合**，
`mount_plan()` 不带 `include_disabled` 时会把每个 disabled 插件的 router 记进去
（`plugin_manifest.py:304`），而 `state_of()` 靠它区分「因为被关掉所以没加载」
（正常）与「清单声称挂着却没挂」（要报）。测试里 `hook.ai` 被关掉过一次，
`("ai_actions_api", "router")` 就永久留在 `_SKIPPED` 里 ——
`reset_cache()` **管不到它**，只有 `reset_skipped()` 能清。

于是轮到那条用例时：`hook.ai` 明明"根本没被尝试过"，却因为在 `_SKIPPED` 里
而**不报「未注册」** → 零 reasons → `state_of()` 判成 `STATE_OK` → `ok=1`。

**修法**（两处都补，因为两个方向都会漏）：
- `test_plugin_manifest.py::_clean_caches` 的 setup **与** teardown 各清一次
  （setup 清「上个文件留下的」，teardown 清「本文件自己留下的」）；
- `test_ai_actions_api.py` 的收尾 fixture同样两处清。

**新增两条守卫**（都经变异验证：删掉任一处 `reset_skipped()` 都正确变红）：
- `test_skipped_ledger_is_populated_by_mount_plan_and_cleared_only_by_its_own_reset`
  —— 钉死"`mount_plan()` 会记账"与"`reset_cache()` **清不掉**它"。
  后半句是关键：把"清一半"这个错法变成会红的断言，而不是留给下一个文件乱红。
- `test_autouse_fixture_leaves_no_skipped_trace_for_the_next_file`
  —— 从**被污染方**角度钉住自己的 fixture 仍会清，且要求 `yield` 前后各一次。

**方法论**（与 §11.1 的反向守卫同源）：
> 新增任何**模块级测试态集合**时，收尾必须连它一起清。判据只有一条：
> **这个集合会不会被别人的断言读到？**
> 只探自己以为的那份账本，会得到"我没污染"的假阴性。
