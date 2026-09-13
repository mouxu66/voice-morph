# 变声工坊 · Win10 虚拟机自动更新端到端测试

> ⚙️ **内部文档（作者本人的两台机器）**：本文假定主机项目根是 `D:\变声`、
> 并且已按 `scripts/vm-test.config.ps1` 配好 Hyper-V / VMware 命令与 guest 凭据。
> 开源用户不必照做：日常验证用 `python tools/check.py --ci-fidelity` 即可（约 3 分钟）；
> 只有要验证**安装包自动更新链路**时才需要这套 VM 流程。
> 2026-09-13 由仓库根移入 `docs/internal/`（原先摆在 README 旁边，容易被误当成上手步骤）。

在干净的 Win10 虚拟机里验证完整更新链路：

> 装 0.2.1 → 收到更新提示 → 下载 → 重启安装 → 0.2.2 生效 → 日志路径正确（不含 `D:\变声`）

AI 无法 GUI 操控虚拟机，脚本只走命令行（`vmrun` / `VBoxManage` / 脚本内 base64 编码的 guest 命令）。**真正在 VM 里跑脚本、把结果贴回 AI 分析的是你本人**（见「完整测试步骤」）。

---

## 1. 前置条件

| 项 | 说明 |
|---|---|
| 虚拟机软件 | VMware Workstation/Player（默认）；VirtualBox 走 `-Hypervisor virtualbox` 分支（best-effort） |
| 干净快照 | 已装 VMware Tools / Guest Additions、**已开自动登录**、无 app、无 `D:\变声`、无 `D:\RVC` |
| 两个安装包 | `web/release2/` 里同时存在 `VoiceMorph-Setup-0.2.1.exe`（旧）与 `VoiceMorph-Setup-0.2.2.exe`（新）+ 对应 `latest.json` |
| 自签证书 | 可选。不导入则安装时弹 SmartScreen「仍要运行」，测试判 PASS 不卡这步 |
| 主机 | PowerShell 7、Node/npm、`python`（起 `http.server`）、`vmrun` 在 PATH |

> 安装包与 `latest.json` 由 `scripts/build-and-publish.ps1` 产出（见第 8 步）。

---

## 2. 虚拟机需要开什么服务

| 服务 | 必需？ | 用途 |
|---|---|---|
| VMware Tools / Guest Additions | **必需** | `vmrun`/`VBoxManage` 的 guest 文件复制、命令执行、进程轮询 |
| 自动登录（Auto-Admin-Logon） | **推荐** | 截图需要交互桌面已解锁；无交互桌面时截图步骤自动跳过（非阻断） |
| OpenSSH Server / WinRM | 可选 | 若不想用 `vmrun` guest 通道，可改走 SSH/WinRM 进 VM 跑命令（脚本当前走 vmrun，未实现 SSH 通道） |

---

## 3. 网络模式与防火墙

- **NAT（默认）**：VM 在 VMware 私有子网，主机是该子网的网关。VM 内访问主机更新源填**主机的 VMnet8 网关 IP**（如 `192.168.19.1`）。脚本 `$HostIP` 留空时会自动探测 VMnet8 地址。
- **桥接（Bridged）**：VM 与主机同局域网，VM 内直接填主机的局域网 IP（如 `192.168.1.x`）。
- **防火墙**：主机需放行入站 TCP `$UpdatePort`（默认 9000）的 `python -m http.server`。若被拦，VM 内 `check-update-source.ps1` 会直接 FAIL，先排查防火墙。

---

## 4. 主机 IP 与虚拟机 IP 怎么查

- **主机（更新源侧）IP**：
  - NAT：`ipconfig` 看 `VMware Network Adapter VMnet8` 的 IPv4 地址，即 VM 看到的网关。
  - 桥接：`ipconfig` 看主网卡 IPv4。
  - 脚本也能自动探测；若探测不准，在 `vm-test.config.ps1` 显式填 `$HostIP`。
- **虚拟机 IP**（一般无需关心，仅排错时用）：VM 内 `ipconfig` 看对应适配器的 IPv4。
- **VM 内访问主机绝不能用 `localhost`**（那是 VM 自己），必须用主机 IP。

---

## 5. 自签证书导入虚拟机信任根（可选）

不导入也能跑（SmartScreen 弹「仍要运行」属预期）。若要完全静默，把根证书导入 VM 信任根：

```powershell
# 在虚拟机内（管理员 PowerShell）执行，certs/black-seraph.pfx 需先拷入 VM
$pfx = "C:\path\to\black-seraph.pfx"
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($pfx, "<证书密码>")
$store = New-Object System.Security.Cryptography.X509Certificates.X509Store("Root","LocalMachine")
$store.Open("ReadWrite")
$store.Add($cert)
$store.Close()
```

或在创建干净快照前就导入，让快照自带信任根。脚本 `$ImportCert=$true` 目前只作为占位提示，实际导入仍需你手动/在快照里完成（避免把证书密码写进脚本）。

---

## 6. 更新源地址怎么配

- 静态清单 `latest.json` 的地址由环境变量 **`VM_UPDATE_URL`** 指定（运行时覆盖 `updater.cjs` 顶部的 `DEFAULT_MANIFEST_URL`）。
- 本测试主机起 `python -m http.server $UpdatePort --directory web/release2`，VM 内：
  ```
  VM_UPDATE_URL = http://<主机IP>:9000/latest.json
  ```
- 脚本在拉起 app 时自动注入该变量，无需手动设。
- **`latest.json` 的 `url` 字段现在是相对文件名（`VoiceMorph-Setup-0.2.2.exe`，ASCII、无中文编码）**，由 `updater.cjs` 在下载时基于 `VM_UPDATE_URL`（即 latest.json 自身地址）解析为绝对地址。所以 latest.json 不绑定具体主机 IP，换机器/换端口无需改它。生成方式见第 8 步 Tip。

---

## 7. 脚本清单

| 文件 | 运行位置 | 功能 |
|---|---|---|
| `scripts/test-update-e2e.ps1` | **主机** | 端口预检/清理 → 起主机更新源 → 回滚快照（断言 `clean-no-app` 存在）→ 拷 0.2.1 静默安装 → 拉起 app（自动更新钩子，结果写到 `C:\vm_e2e`）→ 轮询「下载+静默安装」→ 确认 0.2.2 生效 → 读 stdout/stderr/Chromium 三类日志断言「生产模式已加载 且 不含 `D:\变声`」→ 截图/拷日志回主机 → PASS/FAIL 汇总 |
| `scripts/check-update-source.ps1` | **虚拟机内**（或读同一份配置） | 验证 VM 能访问主机更新源（HTTP 200 + 清单字段齐全） |
| `scripts/build-and-publish.ps1` | **主机** | `npm run build` → `npm version patch` → `npm run electron:build`（自签签名）→ `make-update-manifest.cjs` → 打印待上传清单 |
| `scripts/vm-test.config.example.ps1` | — | 配置模板，复制为 `vm-test.config.ps1` 后填写 |

> `vm-test.config.ps1` 已被 `.gitignore` 忽略（含账号/密码/IP，不入库）。

---

## 8. 完整测试步骤

```powershell
# ① 主机：构建并产出两个版本 + 清单（先确保 web/release2 有 0.2.1 与 0.2.2）
powershell -ExecutionPolicy Bypass -File scripts/build-and-publish.ps1 `
  -BaseUrl https://example.com/voicemorph/download -NotesFile docs\whats-new\0.3.0.md

# ①' 仅重新生成 latest.json（不改版本号、不重打包）时可直接跑：
#    node web/electron/make-update-manifest.cjs --dir release2
#    —— 产出相对 url（如 VoiceMorph-Setup-0.2.2.exe），host 无关。

# ② 主机：复制配置模板并填写（VM 路径、账号、版本号、HostIP 等）
copy scripts\vm-test.config.example.ps1 scripts\vm-test.config.ps1
#   —— 用编辑器填 vm-test.config.ps1 ——
#   重点：$VmxPath 必须指向 `vmrun list` 里实际运行的那个 vmx（见第 11 节）

# ③ 虚拟机内（可选排错）：确认能访问主机更新源
powershell -ExecutionPolicy Bypass -File scripts\check-update-source.ps1 -ConfigFile scripts\vm-test.config.ps1

# ④ 主机：跑端到端
powershell -ExecutionPolicy Bypass -File scripts\test-update-e2e.ps1
```

第 ④ 步会在主机起 `python -m http.server 9000`，自动回滚 VM 快照、安装 0.2.1、拉起 app 触发自动更新到 0.2.2，最后输出：

```
[PASS] VM 回滚至干净快照并启动
[PASS] 0.2.1 静默安装成功  C:\Users\<guest 用户>\AppData\Local\Programs\voice-morph-desktop\voice-morph-desktop.exe
[PASS] 更新源可达（latest.json 合法） PASS:0.2.2
[PASS] 更新检测→下载→静默安装→退出 已完成
[PASS] 版本生效           磁盘 exe 版本=0.2.2，期望 0.2.2
[PASS] 日志路径回归判据（生产模式+不含 D:\变声） OK
[PASS] 截图已保存          D:\变声\scripts\screenshot.png
[PASS] 日志与结果已拷回主机 D:\变声\scripts
RESULT: PASS
```

---

## 9. 结果判读

- **版本生效**：安装目录 exe 的 `FileVersion` 应为 `$NewVersion`（0.2.2）；若仍是 0.2.1，说明自动安装未触发，或 NSIS 静默重装时 electron 残留进程占用文件导致覆盖失败（脚本已在重读版本前 `taskkill` 残留，仍失败则手动确认 VM 内无该 exe 进程）。
- **日志回归判据**：脚本抓三类日志——`app.stdout.log`（主进程 `console.log`）、`app.stderr.log`、`app.chromium.log`（Chromium `--enable-logging=file`），拼成一份后检查 `[frontend] 生产模式`、`[backend] 生产模式后端根` 两行**已出现**且**都不含 `D:\变声`**。只查「不含 D:\变声」而漏查「已加载生产模式」会假 PASS（空日志也过），脚本已同时断言两者。
- 中间产物（日志、结果 JSON、截图）都落在 `scripts/`（`$WorkDir`）。

---

## 10. 已知限制与坑

1. **localhost 不可用**：VM 内 `VM_UPDATE_URL` 必须填主机 IP（脚本已处理）。
2. **venv 硬编码 `D:\变声`**：干净 VM 无此路径 → 包外模型/解释器不注入，核心功能报错。这正是回归守卫生效场景，测试只验「更新链 + 路径门控」，符合预期。
3. **SmartScreen**：初始 0.2.1 拷贝可能带 MOTW，脚本已在 VM 内 `Unblock-File`；更新包由 app 经 http 写入 `userData/updates`，**不带 MOTW**，静默 `/S` 不会被拦。若初始安装卡在 SmartScreen 向导，开 `$ImportCert` 或在快照里导入根证书。
4. **自动安装依赖一个测试钩子**：`VM_UPDATE_TEST_AUTO` 环境变量（默认关闭）。它让 app 在启动静默检查命中更新后**自动下载并静默安装**并写出结果文件。结果文件路径由 `VM_UPDATE_TEST_RESULT` 环境变量决定（测试时脚本注入为 `C:\vm_e2e\update-check-result.json`，与脚本读取路径对齐）；未设时回退到 `userData/update-check-result.json`。这是 VM 无人值守跑通「下载→安装」的唯一干净做法，正式发布不带此变量、行为不变。
5. **截图非强断言**：VMware 无原生 CLI 截图，用 guest 侧 .NET `CopyFromScreen`；需交互桌面已解锁（自动登录）。失败不阻断 PASS/FAIL。
6. **安装目录发现**：脚本查卸载注册表 `DisplayIcon` 反查 exe 路径；若你的 `productName` 改了导致 DisplayName 不匹配，需调整 `test-update-e2e.ps1` 里的匹配正则（`变声|voice-morph`）。

---

## 11. 配置与运行陷阱（务必先看）

1. **`$VmxPath` 必须匹配 `vmrun list` 里实际运行的实例**。虚拟机若迁移过（默认位置 `C:\Users\...\Documents\Virtual Machines` 迁移到 `D:\Virtual Machines` 等），旧路径那份常是过期副本，对它操作会被当成另一个 VM、快照/运行态对不上。确认方法：
   ```powershell
   & "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe" list
   # 输出里 Total running VMs: 1 下面的那行就是真在跑的 vmx，把它填进 $VmxPath
   ```
   脚本启动时也会 `vmrun listSnapshots $VmxPath` 并断言 `clean-no-app` 在列表里，不在就直接报错退出，避免跑到错误副本。

2. **端口 9000 被占用（`The file is already in use`）**：上一次异常退出的 `python -m http.server` 可能还占着端口。脚本在启动前会 `Get-NetTCPConnection -LocalPort 9000` 查占用，**自动 `Stop-Process` 杀掉占用进程并打日志**；若仍报占用，手动查：
   ```powershell
   Get-NetTCPConnection -LocalPort 9000 | Select-Object OwningProcess
   # 再用任务管理器/ Stop-Process 结束对应 PID
   ```

3. **所有 `scripts/*.ps1` 必须是 UTF-8 带 BOM**。PowerShell 5.1 遇到无 BOM 的 UTF-8 中文会按系统代码页（GBK）解码，中文注释/字符串全乱码甚至语法错；PowerShell 7 虽能容错，但同一套脚本可能在两台机器表现不一致。**改名/重存任何 ps1 后务必确认首字节是 `EF BB BF`**（用 VS Code 右下角「UTF-8 with BOM」或 `python -c "open(f,'rb').read(3)"` 校验）。本仓库四个 ps1 已统一为 BOM。

4. **安装包/URL 的中文编码坑（已规避）**：早期安装包名是「变声工坊 Setup 0.2.x.exe」，导致 `latest.json` 的 `url` 被 `encodeURIComponent` 成 `%E5%8F%98...`，依赖 Python `http.server` 在 Windows 上正确 `unquote` 中文路径才下得动，脆弱。现已把 `build.nsis.artifactName` 改为 `VoiceMorph-Setup-${version}.exe`（纯 ASCII），`latest.json` 的 `url` 用相对文件名（host 无关），彻底消除这一类风险。若以后要恢复中文名，务必先验证 VM 内 `updater.cjs` 能正常下载（见 `check-update-source.ps1` + 手动下载试一次）。
