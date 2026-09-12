# vm-test.config.ps1 —— 复制本文件为 vm-test.config.ps1 后填写。
# 由 scripts/test-update-e2e.ps1 通过 `. $PSScriptRoot\vm-test.config.ps1` 加载。
# 本文件只作模板，已在 .gitignore 忽略 vm-test.config.ps1（不入库）。

# ---- 虚拟机 ----
$Hypervisor   = "vmware"      # vmware | virtualbox
$VmxPath      = "C:\VM\Win10-Test\Win10-Test.vmx"  # vmware 需 vmx 路径；virtualbox 用 $VMName
$VMName       = "Win10-Test"  # virtualbox 用
$SnapshotName = "clean-no-app" # 干净快照：无 app、已装 VMware Tools、已开自动登录
$GuestUser    = "tester"
$GuestPass    = "P@ssw0rd"

# ---- 网络 / 更新源 ----
# 主机更新源用 python -m http.server：$UpdatePort 监听 web/release2
$HostIP   = ""                # 留空=自动探测（VMware NAT 取 VMnet8 网关；否则取首个非回环 IPv4）
$UpdatePort = 9000

# ---- 版本 / 安装包（命名遵循 electron-builder：变声工坊 Setup x.x.x.exe，在 web/release2/）----
$OldVersion = "0.2.1"         # 初始安装版本
$NewVersion = "0.2.2"         # 期望更新到的版本
$InstallerPrefix = "变声工坊 Setup"   # 文件名前缀（脚本拼成 "$Prefix $Ver.exe"）

# ---- 证书 ----
$ImportCert = $false          # 是否在 VM 内导入自签根证书（消除 SmartScreen）。false=接受「仍要运行」
$CertPfxPath = "..\certs\black-seraph.pfx"  # 相对脚本目录；仅 $ImportCert=$true 时用

# ---- 超时 / 轮询 ----
$InstallTimeoutSec = 120      # 等待「下载+静默安装+退出」完成的总超时
$PollIntervalSec   = 3
$AppStartWaitSec   = 6        # 拉起后等几秒确认进程起来 / 日志开始写

# ---- 路径 ----
$WorkDir = $PSScriptRoot      # 主机侧中间产物（日志副本/截图/结果 JSON）落这里
