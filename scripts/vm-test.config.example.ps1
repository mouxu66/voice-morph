# vm-test.config.ps1 —— 复制本文件为 vm-test.config.ps1 后填写。
# 由 scripts/test-update-e2e.ps1 通过 `. $PSScriptRoot\vm-test.config.ps1` 加载。
# 本文件只作模板，已在 .gitignore 忽略 vm-test.config.ps1（不入库）。

# ---- 虚拟机 ----
$Hypervisor   = "vmware"      # vmware | virtualbox
# vmware 需 vmx 路径；务必与 `vmrun list` 显示的运行实例一致（迁移过虚拟机的，旧默认位置那份常是过期副本）。
# 例：D:\Virtual Machines\Windows 10 x64\Windows 10 x64.vmx
$VmxPath      = "D:\Virtual Machines\Windows 10 x64\Windows 10 x64.vmx"
$VMName       = "Windows 10 x64"  # virtualbox 用
$SnapshotName = "clean-no-app" # 干净快照：无 app、已装 VMware Tools、已开自动登录
$GuestUser    = "jjjj"
$GuestPass    = "填你虚拟机的密码"

# ---- 网络 / 更新源 ----
# 主机更新源用 python -m http.server：$UpdatePort 监听 web/release2
$HostIP   = ""                # 留空=自动探测（VMware NAT 取 VMnet8 网关；否则取首个非回环 IPv4）
$UpdatePort = 9000

# ---- 版本 / 安装包（命名遵循 electron-builder artifactName：VoiceMorph-Setup-x.x.x.exe，在 web/release2/）----
$OldVersion = "0.2.1"         # 初始安装版本
$NewVersion = "0.2.2"         # 期望更新到的版本
$InstallerPrefix = "VoiceMorph-Setup-"   # 文件名前缀（脚本拼成 "$Prefix$Ver.exe"，无空格）

# ---- 证书 ----
# $ImportCert=$true 时，脚本会在「回滚快照之后、安装之前」把 $CertPfxPath 导入
# guest 的受信任根（LocalMachine\Root），消除自签证书的「未知发布者」提示。
# 注意：不能靠快照预导入——回滚会抹掉。导入需管理员权限，请确认 guest 账号可提权。
$ImportCert   = $false        # 是否在 VM 内导入自签根证书。false=接受「仍要运行」提示
$CertPfxPath  = "..\certs\black-seraph.pfx"  # 相对脚本目录；仅 $ImportCert=$true 时用
$CertPassword = "填你的 pfx 密码"             # 仅 $ImportCert=$true 时用

# ---- 超时 / 轮询 ----
$InstallTimeoutSec = 120      # 等待「下载+静默安装+退出」完成的总超时
$PollIntervalSec   = 3
$AppStartWaitSec   = 6        # 拉起后等几秒确认进程起来 / 日志开始写

# ---- 拉起 app 的方式 ----
# $false（默认）= vmrun runProgramInGuest -interactive
#   让 Electron 进入**交互式会话**而非 Session 0 服务会话（GUI 程序必需）。
# $true = guest 内 schtasks 建「交互式任务」再 run（回退方案）
#   用于 -interactive 仍拿不到交互桌面时；依赖 guest 已登录且会话活跃（自动登录）。
# 判据说明：无论哪种方式，都**不能**用执行脚本的 PowerShell 的 SessionId 判断 app 在哪——
#   runProgramInGuest 起的 PowerShell 恒在 Session 0。真判据 = 回查 app 进程自己的 SessionId
#   （脚本轮询阶段会记录在 app-running-<n>.json 的 appSessions 字段）。
$UseSchtasksFallback = $false

# ---- 路径 ----
$WorkDir = $PSScriptRoot      # 主机侧中间产物（日志副本/截图/结果 JSON）落这里
