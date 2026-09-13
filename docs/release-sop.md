# 变声工坊 · 发版 SOP

> 一条命令发版：`scripts/release.ps1`。本文覆盖发版前检查、命令序列、常见错误、回滚方法。
> 首次发版请完整读一遍；熟悉后可直接跳到「二、标准发版流程」。

## 一、发版前检查清单

发版前逐项确认，任何一项不通过就**不要发**。

| # | 检查项 | 怎么查 | 不通过的后果 |
|---|---|---|---|
| 1 | 该提交的都提交了 | `git status` 应为 clean | 包里的代码与仓库不一致，无法追溯 |
| 2 | 前端能构建 | `cd web && npm run build` | 打包会中途失败 |
| 3 | **签名证书密码已设** | `$env:CSC_KEY_PASSWORD` 非空 | 包不带签名 → 用户看到「未知发布者」 |
| 4 | 证书文件在 | `Test-Path certs\black-seraph.pfx` | electron-builder 签名步骤失败 |
| 5 | 更新源可达 | VM 内 `Invoke-WebRequest <更新源>/latest.json` | 用户检查更新报「无法连接更新源」 |
| 6 | 版本号没发错 | `web/package.json` 的 version | **最危险**：版本倒退会导致用户更新死循环 |
| 7 | 更新说明写好了 | 准备好 `-Notes` 或 `-NotesFile` | latest.json 的 notes 为空，用户看不到更新内容 |

### 一键前置检查

```powershell
powershell -ExecutionPolicy Bypass -File D:\变声\scripts\release.ps1 -DryRun -BaseUrl http://192.168.1.100:9000
```

只检查不构建，输出当前版本 / 证书状态 / 产物目录 / 更新源地址。**建议每次发版先跑这个。**

## 二、标准发版流程

### 命令序列

```powershell
# 0) 进仓库根（脚本不依赖 CWD，但习惯上在这跑）
cd D:\变声

# 1) 设签名密码（每次新开终端都要设，不写进文件、也不写进本手册）
$env:CSC_KEY_PASSWORD = '<你的 pfx 密码>'

# 2) 前置检查（可选，推荐）
powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -DryRun -BaseUrl http://192.168.1.100:9000

# 3) 一条命令发版
powershell -ExecutionPolicy Bypass -File scripts\release.ps1 `
  -BaseUrl http://192.168.1.100:9000 `
  -Notes "修复录音卡顿；新增音高建议"

# 4) 上传
#    把脚本打印的「待上传文件」三个文件传到更新源根目录：
#      VoiceMorph-Setup-x.x.x.exe
#      VoiceMorph-Setup-x.x.x.exe.blockmap
#      latest.json
```

### release.ps1 干了什么

```
1/6 前置检查        版本号 / CSC_KEY_PASSWORD / 证书文件 / 产物目录 / 更新源地址
2/6 前端构建        cd web && npm run build （tsc -b && vite build）
3/6 版本号 +1       npm version patch --no-git-tag-version（不自动 git commit）
4/6 签名打包        npm run electron:build（electron-builder --win，实为 build + pack）
4.5 签名校验        Get-AuthenticodeSignature，Valid / NotSigned 明确提示
5/6 生成清单        node electron/make-update-manifest.cjs --dir release2 --version <新版本>
                    └─ 自校验：清单 sha256 与磁盘文件实际值比对，不一致直接 FAIL
                    └─ 自校验：清单 version 与 package.json 一致
6/6 待上传清单      打印文件名 + 体积 + 上传目标 + 用户端 VM_UPDATE_URL 该指向哪
```

### 常用参数

| 参数 | 用途 |
|---|---|
| `-BaseUrl <url>` | 更新源对外地址；清单 url 变成 `<url>/VoiceMorph-Setup-x.x.x.exe`。留空则 url 只是本地文件名 |
| `-Notes "文本"` | 更新说明短文本，进 latest.json 的 `notes` |
| `-NotesFile path.md` | 更新说明文件（优先级高于 `-Notes`） |
| `-Mandatory` | 强制更新，前端不给「跳过此版本」 |
| `-SkipBump` | 不升版本，只重打包当前版本（重发同版本时用） |
| `-SkipBuild` | 跳过单独的前端构建（`electron:build` 内部已含） |
| `-DryRun` | 只做前置检查 |

## 三、更新源怎么配

### 局域网 / 本机测试

```powershell
# 主机起静态服务（release.ps1 会在结尾把这行打印出来）
python -m http.server 9000 --directory D:\变声\web\release2
```

客户端（或 VM）环境变量指向**主机 IP**（不能用 `localhost`——VM 里的 localhost 是 VM 自己）：

```
VM_UPDATE_URL = http://192.168.1.100:9000/latest.json
```

### 正式分发

任何能放静态文件的地方都行（对象存储 / GitHub Releases / 网盘直链 / 局域网共享）：

1. 把 `latest.json` 与安装包、blockmap 上传到同一目录
2. 发版时用 `-BaseUrl https://your.domain/path`，让清单里的 url 指向正确地址
3. 客户端 `VM_UPDATE_URL` 指向该地址下的 `latest.json`

> **持久配置**：`web/electron/updater.cjs` 里的 `DEFAULT_MANIFEST_URL` 默认是空串
> （空 = 纯本地模式，不发任何网络请求）。要面向所有用户开更新，把它填成正式更新源地址。
> 临时调试/内网分发用 `VM_UPDATE_URL` 环境变量覆盖即可。

## 四、常见错误处理

### E1 · 用户报「无法连接更新源」

- 更新源地址填的是 `localhost` → 改成可达 IP/域名。
- 防火墙拦了 9000 → 主机放行：`New-NetFirewallRule -DisplayName "update-src" -Direction Inbound -LocalPort 9000 -Protocol TCP -Action Allow`。
- HTTP 服务没起 → 检查 `python -m http.server` 是否还在跑（关终端会一起关）。

### E2 · 用户报「更新源返回 HTTP 404」

- 文件名对不上：清单里的 `url` 与实际上传的文件名不一致。**执行 `release.ps1` 时给 `-BaseUrl`，让脚本自己拼**，别手写。
- 文件传到了子目录：所有文件必须在**同一层**（与 latest.json 同级）。

### E3 · 用户报「安装包校验失败：期望 xxx 实际 yyy」

- **清单与安装包不匹配**。典型场景：重打了包但没重新生成 `latest.json`，或手动改过清单。
- 对策：重跑 `release.ps1`（或者 `-SkipBump` 重打包当前版本），它会**自动校验 sha256 并在不一致时直接 FAIL**。
- 注意 `updater.cjs` 有缓存复用逻辑：同文件名内容变了会**自动重下**，不用手动清客户端缓存。

### E4 · 用户报「装完版本号没变 / 更新一直弹」

- 说明装上去的包不是新版。检查 `latest.json` 的 `version` 是否真的比用户当前版本大。
- **发版时版本号必须递增**。`release.ps1` 用 `npm version patch` 自动 +1，别手动改回去。
- 极端情况：用户装的是「版本号倒退」的包 → 见下面「回滚」。

### E5 · 打包报签名失败 / 包不带签名

- `CSC_KEY_PASSWORD` 没设或设错 → 重新设（具体值见证书管理处，**不落盘、不入库**）。
- 证书文件缺失 → 确认 `certs/black-seraph.pfx` 存在。
- 只想本地测：不加签名也能跑，只是用户会看到「未知发布者」。

### E6 · 用户安装时弹「Windows 已保护你的电脑」（SmartScreen）

- 自签证书的固有现象，**不是 bug**。用户点「更多信息 → 仍要运行」即可。
- 要消除：让用户把根证书导入「受信任的根证书颁发机构」（`scripts/gen-selfsigned-cert.ps1` 可参考生成流程）。
- 商用要彻底消除需买代码签名证书。

### E7 · `npm run electron:build` 中途失败

- 常见于 Node 依赖缺失：`cd web && npm install` 后重试。
- 磁盘空间不足：安装包约 100MB，加上中间产物需预留 1GB+。
- 前端类型错误：先单独跑 `npm run build` 看 tsc 报错。

### E8 · 清单选中了错误的安装包版本

- `make-update-manifest.cjs` 会**优先按 package.json 版本号精确匹配文件名**（`VoiceMorph-Setup-<version>.exe`），
  只有匹配不到才退化为「取 mtime 最新的 exe」并打警告。
- 看到那行 `⚠ 没有文件名精确匹配版本 x.x.x 的安装包` 就要警觉：说明产物命名不符合 `artifactName` 模板，
  或版本号与包名不一致。**此时不要发版**，先查清为什么没匹配上。
- 候选池只收**纯 ASCII 文件名**的 `.exe`：中文名旧包（`变声工坊-Setup-*.exe`）即使同版本也会被排除，
  避免和 ASCII 新包同时命中导致发错包；`.blockmap` / `uninstall.exe` 同样被排除。
- **自查命令**：`node tools/test-manifest-pick.cjs`（17 项，覆盖候选过滤 / 版本边界 / mtime 优先级）。
  改动过 `make-update-manifest.cjs` 或 `artifactName` 后务必跑一遍。
- 事故后果的严重性：选错包 = 把旧包的 sha256 写进清单，客户端**校验通过、静默装回旧版**，日志无异常。

## 五、回滚方法

### 场景 A · 用户装坏了 / 新版有严重 bug，想退回旧版本

**关键认知**：自动更新器**只往版本号大的方向走**（`compareVersion(latest.version, current) <= 0` 就当「已是最新」）。
所以**不能靠改 latest.json 指向旧版本来回滚**——用户端根本不会降级。

正确做法是**发一个版本号更高的修复版**：

```powershell
# 1) 从 git 找到上一个好版本，把它的代码拉回来（或直接修 bug）
git log --oneline            # 找到目标提交
git checkout <good-commit> -- web/src m2_server web/electron

# 2) 正常发版（版本号会从当前值继续 +1，天然高于坏版本）
powershell -ExecutionPolicy Bypass -File scripts\release.ps1 `
  -BaseUrl http://192.168.1.100:9000 `
  -Notes "回滚：修复 x.x.x 引入的录音崩溃"

# 3) 上传，用户检查更新即会升到这个修复版
```

> 例：0.3.0 坏了 → 发 0.3.1（内含 0.2.9 的代码）→ 用户升到 0.3.1 即等于回到旧行为。

### 场景 B · 必须让用户立刻停止自动更新

把更新源的 `latest.json` 临时下线（重命名或删掉）：

```powershell
# 主机更新源
Rename-Item D:\变声\web\release2\latest.json latest.json.disabled
```

用户端会报「无法连接更新源 / HTTP 404」，但**不会崩**——更新失败是静默的，
不影响本地功能。修好后改回来即可。

### 场景 C · 单个用户被卡住，要手动救

让用户手动装旧版安装包（`web/release2/` 里保留着历史版本）：

1. 关掉应用
2. 直接运行旧版 `VoiceMorph-Setup-x.x.x.exe` 覆盖安装
   （NSIS 记住上次安装路径，会原地覆盖）
3. 若提示已安装，可先在「设置 → 应用」里卸载，再装旧版

**注意**：手动装回旧版后，自动更新会立刻提示升级到新版。要暂时压住，可在应用里点「跳过此版本」。

### 场景 D · 本地开发时想重发同版本

```powershell
# 不升版本，重打包 + 重生成清单（适合修了构建配置、要重发同一版本）
powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -SkipBump -BaseUrl http://192.168.1.100:9000
```

> 因为 `updater.cjs` 会按 sha256 判断缓存，重发的包内容若变了，用户端会**自动重下**，不会卡在旧包上。

## 六、验证发版是否成功

发版后必须实际验证一遍（详见 `docs/internal/README-vm-test.md` 的 VM 端到端流程）：

```powershell
# 主机起更新源
python -m http.server 9000 --directory D:\变声\web\release2

# 跑端到端自动更新测试（回滚 VM 快照 → 装旧版 → 自动更新到新版 → 校验）
powershell -ExecutionPolicy Bypass -File D:\变声\scripts\test-update-e2e.ps1
```

看到 `RESULT: PASS` 才算发版闭环。相关脚本静态体检：`scripts/test-ps1-lint.ps1`。

## 七、相关文件索引

| 文件 | 作用 |
|---|---|
| `scripts/release.ps1` | **发版主入口**（本文档对应脚本） |
| `scripts/build-and-publish.ps1` | 底层构建脚本（release.ps1 的前身，保留） |
| `web/electron/make-update-manifest.cjs` | 生成 latest.json（含**按版本号选包**，见 §五 E8） |
| `tools/test-manifest-pick.cjs` | 选包逻辑单测（候选过滤 + 版本匹配 + mtime 优先级），发版前建议跑 |
| `web/electron/updater.cjs` | 客户端更新逻辑（检查/下载/校验/安装） |
| `web/electron/update-ipc.cjs` | 更新 IPC + 启动静默检查 |
| `docs/internal/README-vm-test.md` | VM 端到端测试手册（内部：作者本机环境专用） |
| `scripts/test-update-e2e.ps1` | 端到端自动更新测试 |
| `.workbuddy/memory/auto-update.md` | 更新链路全部踩坑记录 |

## 八、发版检查表（可打印）

```
[ ] git status 干净，改动都已提交
[ ] $env:CSC_KEY_PASSWORD 已设置
[ ] certs/black-seraph.pfx 存在
[ ] 跑过 release.ps1 -DryRun，前置检查全 OK
[ ] 跑过 node tools/test-manifest-pick.cjs（选包逻辑，17 项全绿）
[ ] 更新说明已准备（-Notes 或 -NotesFile）
[ ] 版本号递增正确（release.ps1 自动 patch）
[ ] 打包后签名状态是 Valid（不是 NotSigned）
[ ] latest.json 的 sha256 自校验通过（脚本自动做）
[ ] 待上传三个文件已传到更新源同一目录
[ ] 客户端 VM_UPDATE_URL 指向 latest.json
[ ] 跑过 test-update-e2e.ps1，结果 PASS
[ ] 更新说明已发给用户（如需要）
```
