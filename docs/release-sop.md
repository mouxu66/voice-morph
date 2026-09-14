# 变声工坊 · 发版 SOP

> 一条命令发版：`scripts/release.ps1`。本文覆盖发版前检查、命令序列、常见错误、回滚方法。
> 首次发版请完整读一遍；熟悉后可直接跳到「二、标准发版流程」。

## 〇、发版节奏（先读：什么时候该发，什么时候不该发）

> 2026-09-14 用户拍板：**按大版本发，不随每次改动发**。频繁上传没必要。

**先分清两件事**——它们完全无关：

| 动作 | 客户端能否感知 | 代价 |
|---|---|---|
| `git push` 代码到 GitHub | **完全不能**。客户端从不读仓库源码，只读 `releases/latest/download/latest.json` 这一个永久别名 | 几秒，随便推 |
| 发 Release（`release.ps1 -Publish`） | 能。启动 12s 后会检查到 | 构建几分钟 + **上传 ~100MB 安装包** + 签名密码。**这才是"上传"** |

**所以"每次改动都往网上传"是个误解**：日常改完只 `git push` 就行，客户端零变化。

**已定节奏**：

- **日常改动** → 只 `git push`，**不发 Release**。自己用本地构建（见下）。**版本号一个字都不碰**
- **里程碑**（`0.3.0` / `1.0.0` 这种）→ `release.ps1 -Publish -BumpLevel minor`（或 `major`）
- **必须让所有人升级的关键修复** → 发，并加 `-Mandatory`
  （普通更新用户能点「跳过此版本」，标了 mandatory 的**跳不过去**，见 `updater.cjs`）

**少发是安全的（有代码依据，不用怕）**：线上没有 Release 时，`latest.json` 返回 404 →
`checkForUpdates()` 返回 `{ok:false, hasUpdate:false}` → 调用处直接 `return`。
客户端**不下载、不安装、不报错**，界面上也不弹任何东西。所以"几个月不发"没有任何副作用。

**唯一代价**：不发 Release = **别人拿不到你的修复**，永远停在旧版本。
你本机可以随时跑最新本地构建，但**别的机器 / 已安装的用户**不会自己变新。

**版本号只在发版那一步升，本地开发天然不碰它**：`release.ps1` 的升版本在第 3 步（构建之后），
日常 `git push` 和本地 `--dir` 打包都不会改 `web/package.json` 的 version。所以"本地搞"不需要任何额外动作。

但**升多少必须跟节奏对齐**：`release.ps1` 默认只升 `patch`（0.2.3 → 0.2.4），
而"只在大版本发"要的是 `0.2.3 → 0.3.0` —— 所以里程碑发版**必须显式给 `-BumpLevel minor`**，
否则你会一路发出 0.2.4 / 0.2.5 …，里程碑和补丁分不出来（`compareVersion` 只要求递增，不挑级别，
所以它不会报错，只会悄悄发错版本号）。

重发同版本（修包重传）用 `-SkipBump`。

### 本地日常怎么用最新代码（不发 Release 的前提）

```bash
cd web && npm run build && npx electron-builder --dir   # 只出 win-unpacked：不打安装包、不签名，快得多
# 然后直接跑 web/release2/win-unpacked/变声工坊.exe
```

**注意**：`win-unpacked` 出来的也是 `app.isPackaged === true`，**照样会**在启动 12s 后静默检查更新。
不想让本地绿色版被更新提示打扰，设环境变量 **`VM_UPDATE_URL=off`**
（`checkForUpdates()` 会返回 `configured:false`「更新源已关闭」，不再请求网络）。

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
powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -DryRun -Publish

# 3) 一条命令发版：打包 + 生成清单 + 发 GitHub Release
powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -Publish -Notes "修复录音卡顿"

# 完事。客户端（已安装的旧版）启动 12 秒后会静默检查到，弹更新页。
```

**不再需要手动上传**：`-Publish` 会把 `exe` / `.blockmap` / `latest.json` 作为 asset 挂到
新建的 GitHub Release 上。客户端读的是永久别名 `releases/latest/download/latest.json`，
所以发新版客户端**不用改任何配置**。

不用 GitHub 时（对象存储 / 局域网 / 网盘）仍走老路子：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\release.ps1 `
  -BaseUrl https://example.com/voicemorph/download -Notes "修复录音卡顿"
# 然后手动把「待上传文件」三个文件传到 -BaseUrl 指向的位置
```

### release.ps1 干了什么

```
1/7 前置检查        版本号 / CSC_KEY_PASSWORD / 证书文件 / 产物目录 / 更新源地址
                    └─ -Publish 时另查：gh 可执行文件、gh 登录状态
2/7 前端构建        cd web && npm run build （tsc -b && vite build）
3/7 版本号 +1       npm version patch --no-git-tag-version（不自动 git commit）
4/7 签名打包        npm run electron:build（electron-builder --win，实为 build + pack）
                    └─ -SkipSign 时改跑 npm run build + electron-builder -c.win.certificateFile=（跳过签名）
4.5 签名校验        Get-AuthenticodeSignature，Valid / NotSigned 明确提示
5/7 生成清单        node electron/make-update-manifest.cjs --dir release2 --version <新版本>
                    └─ -Publish 且未给 -BaseUrl 时，自动推导 GitHub asset 直链作为 base-url
                    └─ 自校验：清单 sha256 与磁盘文件实际值比对，不一致直接 FAIL
                    └─ 自校验：清单 version 与 package.json 一致
6/7 发布到 GitHub   gh release create v<新版本> --latest，上传 exe / blockmap / latest.json
                    └─ 自校验：实测 releases/latest/download/latest.json 的版本号是否为新版本
7/7 清理历史产物     release2 里只保留最近 2 个版本的安装包（先截断到 0 字节再删，见下）
                    + 打印待上传文件清单与体积
```

### 常用参数

| 参数 | 用途 |
|---|---|
| `-Publish` | 打完后直接发 GitHub Release（建 tag + Release + 上传 asset + 探测别名）。**推荐** |
| `-Repo <owner/name>` | `-Publish` 的目标仓库，默认 `mouxu66/voice-morph` |
| `-SkipPrune` | 不清理 `release2` 里的历史安装包（默认保留最近 2 个版本） |
| `-BaseUrl <url>` | 更新源对外地址；清单 url 变成 `<url>/VoiceMorph-Setup-x.x.x.exe`。`-Publish` 时会自动推导，无需手填 |
| `-Notes "文本"` | 更新说明短文本，进 latest.json 的 `notes`，同时作为 Release 说明 |
| `-NotesFile path.md` | 更新说明文件（优先级高于 `-Notes`） |
| `-Mandatory` | 强制更新，前端不给「跳过此版本」 |
| `-SkipBump` | 不升版本，只重打包当前版本（重发同版本时用） |
| `-BumpLevel patch\|minor\|major` | 升版本级别，默认 `patch`。**按「〇、发版节奏」只在大版本发时用 `minor`（0.2.3 → 0.3.0）或 `major`**；非法值在任何构建之前就 FAIL（不白跑打包） |
| `-SkipBuild` | 跳过单独的前端构建（`electron:build` 内部已含） |
| `-DryRun` | 只做前置检查 |

> **清理历史产物**：`release2` 每发一次版就多一个 ~100MB 安装包，长期堆积几百 MB。
> 现在默认只留最近 2 个版本（回滚够用）。删除前先 `SetLength(0)` 截断再删 ——
> 本机的「安全删除」会把文件挪进回收站、空间不会立刻归还，截断过再删即使进回收站也只是空壳
> （约定见 `tools/hard_delete.py`）。要留全量就加 `-SkipPrune`。

### 安装行为：升级 = 原地替换

客户端装过一次后，再装新版**不会**产生第二份安装，也不会多一个桌面图标。
这不是靠约定，是靠三处机制（2026-09-14 逐行核对 electron-builder 的 NSIS 模板确认）：

| 机制 | 落点 | 作用 |
|---|---|---|
| 安装路径 | `HKCU\Software\<GUID>` → `InstallLocation` | 安装器每次先读它，装回原处 |
| 快捷方式名 | 同键 → `ShortcutName` | 名字不变则同名覆盖；变了则 `Rename` 迁移旧图标 |
| 旧版本 | `installSection.nsh` → `uninstallOldVersion` | 装之前先卸旧版（`KeepShortcuts=true` 时保留快捷方式） |

`web/package.json` 的 `nsis` 段刻意做了三件事，改动前先看 `tools/test-packaging.cjs` 的守卫：

- `allowToChangeInstallationDirectory: false` —— 不显示目录选择页。**这是"升级绝不并存"的关键**：
  开着的时候安装向导那页路径可编辑，手滑改到别的目录就会装出第二份。
- `shortcutName: "变声工坊"` —— 钉死快捷方式名，不再从 `productName` 推导。
- `deleteAppDataOnUninstall: false` —— 卸载保留 `userData`（模型路径 / 声卡设置 / 更新源）。

> **要自定义安装目录**（例如装到 D 盘）：命令行传 NSIS 标准开关 ——
> `"VoiceMorph-Setup-0.2.3.exe" /D=D:\Apps\VoiceMorph`。
> `/D` 必须是**最后一个参数**，且路径**不能加引号**。


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

### 正式分发 · GitHub Releases（默认，推荐）

`DEFAULT_MANIFEST_URL` 已经写死为本仓库的最新版永久别名：

```
https://github.com/mouxu66/voice-morph/releases/latest/download/latest.json
```

`/releases/latest/download/<asset>` 由 GitHub 动态解析到**最新**那个 Release 的同名 asset ——
所以发新版只要 `-Publish`，客户端不换配置就能跟上。

要点：

1. 每个版本一个 Release（tag 形如 `v0.2.3`），asset 放三个文件：`VoiceMorph-Setup-x.y.z.exe`、
   同名 `.blockmap`、`latest.json`。**`latest.json` 必须与安装包在同一个 Release 里**，
   永久别名才取得到。
2. `-Publish` 会自动 `--latest` 标记，避免 GitHub 按它自己的规则挑错"最新"。
3. 仓库必须 **public**（私有仓库的 asset 需要 token，本项目 updater 不带 token）；
   也正因如此，release 里的安装包是**公开可下载**的。
4. GitHub 的 asset 直链会 302 到 CDN，updater 已支持跟随重定向（最多 5 跳）。

### 正式分发 · 自建源（对象存储 / 网盘 / 局域网共享）

1. 把 `latest.json` 与安装包、blockmap 上传到同一目录
2. 发版时用 `-BaseUrl https://your.domain/path`，让清单里的 url 指向正确地址
3. 客户端 `VM_UPDATE_URL` 指向该地址下的 `latest.json`

### 客户端怎么指向

| 场景 | 做法 |
|---|---|
| 默认（安装版） | 什么都不用做，读 `DEFAULT_MANIFEST_URL` 的 GitHub 别名 |
| 内网 / 自建源 / 调试 | 环境变量 `VM_UPDATE_URL=https://.../latest.json` |
| 要完全离线 | 环境变量 `VM_UPDATE_URL=off`（也接受 `0` / `none` / `false` / `disabled`），一个请求都不发 |

> `VM_UPDATE_URL` 优先级**高于** `DEFAULT_MANIFEST_URL`。注意：环境变量留空**不等于**关闭
> —— 留空会回落到默认 GitHub 源；要关就用 `off`。改默认源要动
> `web/electron/updater.cjs` 的常量，且**必须重新打包**才对已安装的旧版生效
> （旧版读的是它自己 asar 里那份旧常量）。

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
- 只想本地测：加 **`-SkipSign`**。脚本会改用 `web/electron-builder.nosign.cjs`
  把 `win.certificateFile` 置成 `null` 跳过签名；包能用，只是用户会看到「未知发布者」。
  **正式分发禁止加这个开关。**
- ⚠️ **别试图用 CLI 覆盖证书路径，走不通**。electron-builder 判断"要不要签名"用的是
  `certificateFile != null`（只看是不是 null、不看真假），于是：
  `-c.win.certificateFile=` → 空串被当成证书路径 → `ENOENT: open ''`；
  `-c.win.certificateFile=null` → CLI 不做 JSON 解析 → 被当成文件名 → `open '...\web\null'`。
  **只有在配置文件里给真正的 `null` 才生效** —— 这就是 `-SkipSign` 走 `--config` 的原因。

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

### E9 · `-Publish` 报「gh 未登录」/ 找不到 gh

- **找不到 gh**：脚本先在 PATH 找，找不到会退回 WorkBuddy 自带那份
  （`%USERPROFILE%\.workbuddy-ai\bin\gh\bin\gh.exe`）。都没有就装一个，或去掉 `-Publish` 手动上传。
- **报未登录但明明登录过**：本机 gh 默认读 `%APPDATA%\GitHub CLI`，而凭据在 `~/.config\gh`
  —— 脚本会自动补 `GH_CONFIG_DIR`（2026-09-14 实测的坑）。若仍报错，手动设：
  ```powershell
  $env:GH_CONFIG_DIR = "$env:USERPROFILE\.config\gh"
  gh auth status
  ```
- **真的没登录**：`gh auth login -h github.com -s repo,workflow`（`repo` 就够发 Release，
  `workflow` 是为了以后加 Actions 不用重新授权）。

### E10 · `-Publish` 报「Release vX.Y.Z 已存在」

- 同一版本重发。GitHub 上先删掉那个 Release（tag 可留可删），或去掉 `-Publish` 走手动上传覆盖。
- 若只想重打包**同版本**并覆盖：`-SkipBump` + 手动覆盖 asset（`gh release upload --clobber`）。

### E11 · `-Publish` 后别名探测失败 / 版本对不上

- 刚发布几十秒内 GitHub 的 `/releases/latest/...` 可能还没生效，等一会儿重试。
- 版本对不上：检查这次 Release 是否被标成 latest（脚本已带 `--latest`）；若手动在网页上发过
  Release，把新的那个标为 latest，或删掉多的那个。
- 别名能通但客户端仍不更新：确认 `latest.json` **和安装包在同一个 Release 里**
  （永久别名只解析同 Release 的 asset），且清单 `version` 确实大于客户端版本。

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
# 自建源（局域网 / 对象存储）
Rename-Item D:\变声\web\release2\latest.json latest.json.disabled
```

GitHub Releases 上没有"重命名 asset"这回事，改用下面任一方式：

```powershell
# 方式 1：把最新 Release 标成 prerelease —— /releases/latest/ 会跳过它，别名随即失效
gh release edit v0.3.0 --prerelease --repo mouxu66/voice-morph

# 方式 2：删掉 latest.json 这个 asset（安装包留着，方便手动救急）
gh release delete-asset v0.3.0 latest.json --repo mouxu66/voice-morph

# 恢复：改回来即可
gh release edit v0.3.0 --prerelease=false --latest --repo mouxu66/voice-morph
```

用户端会报「无法连接更新源 / HTTP 404」，但**不会崩**——更新失败是静默的，
不影响本地功能。修好后改回来即可。

### 场景 C · 单个用户被卡住，要手动救

让用户手动装旧版安装包（`web/release2/` 里保留着历史版本）：

1. 关掉应用
2. 直接运行旧版 `VoiceMorph-Setup-x.x.x.exe` 覆盖安装
   （NSIS 记住上次安装路径，会原地覆盖）
3. 若提示已安装，可先在「设置 → 应用」里卸载，再装旧版

> **注意**：`release2` 默认只保留**最近 2 个版本**（见 §二 的清理说明）。要救更旧的版本，
> 得从 git 拉回对应代码重打包，或去 GitHub Releases 里翻历史 asset 手动下载。
> 想长期留全量就发版时加 `-SkipPrune`。

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
| `web/electron/UPDATE.md` | 客户端更新机制说明（更新源优先级 / 缓存清理 / 行为细节） |
| `web/electron/make-update-manifest.cjs` | 生成 latest.json（含**按版本号选包**，见 §五 E8） |
| `tools/test-manifest-pick.cjs` | 选包逻辑单测（候选过滤 + 版本匹配 + mtime 优先级），发版前建议跑 |
| `tools/test-updater.cjs` | 更新器单测（默认源 / 关闭源 / 下载校验 / 落标记 / 缓存清理） |
| `tools/test-update-hook.cjs` | 启动静默检查钩子单测 |
| `web/electron/updater.cjs` | 客户端更新逻辑（检查/下载/校验/安装/清缓存） |
| `web/electron/update-ipc.cjs` | 更新 IPC + 启动静默检查 + 启动时清缓存 |
| `docs/internal/README-vm-test.md` | VM 端到端测试手册（内部：作者本机环境专用） |
| `scripts/test-update-e2e.ps1` | 端到端自动更新测试 |
| `.workbuddy/memory/auto-update.md` | 更新链路全部踩坑记录 |

## 八、发版检查表（可打印）

```
[ ] git status 干净，改动都已提交
[ ] $env:CSC_KEY_PASSWORD 已设置
[ ] certs/black-seraph.pfx 存在
[ ] 跑过 release.ps1 -DryRun，前置检查全 OK
[ ] 跑过 node tools/test-update-hook.cjs / test-updater.cjs / test-manifest-pick.cjs（更新链路单测全绿）
[ ] 更新说明已准备（-Notes 或 -NotesFile）
[ ] 版本号递增正确（release.ps1 自动 patch）
[ ] 打包后签名状态是 Valid（不是 NotSigned）
[ ] latest.json 的 sha256 自校验通过（脚本自动做）
[ ] 已发 GitHub Release（-Publish）：exe / blockmap / latest.json 三个 asset 在同一个 Release 里
[ ] Release 已标 latest，且 releases/latest/download/latest.json 返回的就是新版本号
[ ] 不用 GitHub 时：三个文件已传到 -BaseUrl 同一目录，客户端 VM_UPDATE_URL 指向该 latest.json
[ ] release2 只剩最近 2 个版本（想留全量则明确用 -SkipPrune）
[ ] 跑过 test-update-e2e.ps1，结果 PASS
[ ] 更新说明已发给用户（如需要）
```
