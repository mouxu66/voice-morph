# test-update-e2e.ps1 —— 主机端：Win10 虚拟机自动更新端到端测试编排
#
# 依赖：VMware Tools（默认）/ VirtualBox Guest Additions；主机 PowerShell 7；python（http.server）。
# 运行：powershell -ExecutionPolicy Bypass -File scripts/test-update-e2e.ps1
# 配置：同目录 vm-test.config.ps1（从 vm-test.config.example.ps1 复制填写）。
#
# 链路：端口预检/清理 → 起主机更新源 → 回滚干净快照 → 拷入 0.2.1 静默安装
#       → 拉起 app（设 VM_UPDATE_URL + VM_UPDATE_TEST_AUTO + VM_UPDATE_TEST_RESULT，stdout 重定向落盘）
#       → app 自动检测/下载/静默安装 0.2.2 并退出 → 重读 exe 版本确认 0.2.2 生效
#       → 读日志断言「生产模式已加载 且 不含 D:\变声」（回归判据）
#       → 截图/拷日志回主机 → 输出 PASS/FAIL。
#
# 注：本脚本只能命令行驱动 VM，真正的 GUI 点击由 VM 内进程完成；AI 不 GUI 操控 VM。

param(
  [string]$ConfigFile = "$PSScriptRoot\vm-test.config.ps1"
)

$ErrorActionPreference = "Stop"

# ---------------- 加载配置 ----------------
if (-not (Test-Path $ConfigFile)) {
  Write-Error "找不到配置文件 $ConfigFile —— 请复制 vm-test.config.example.ps1 为 vm-test.config.ps1 并填写。"
  exit 1
}
. $ConfigFile

# vmx 路径校验：必须来自配置，且文件要存在；错的路径会在 vmrun 里被当另一个实例
if (-not $VmxPath -or -not (Test-Path $VmxPath)) {
  Write-Error "VmxPath 无效或文件不存在：$VmxPath（请改 vm-test.config.ps1，务必与 `vmrun list` 的运行实例一致）。"
  exit 1
}

$GuestWork = "C:\vm_e2e"
$GuestLogPattern = @("$GuestWork\app.stdout.log", "$GuestWork\app.stderr.log", "$GuestWork\app.chromium.log")
$Pass = $true
$Summary = [System.Collections.Generic.List[string]]::new()

function Add-Result([bool]$ok, [string]$label, [string]$detail = "") {
  if (-not $ok) { $script:Pass = $false }
  $mark = if ($ok) { "PASS" } else { "FAIL" }
  $script:Summary.Add("[$mark] $label" + $(if ($detail) { " —— $detail" } else { "" }))
}

# ---------------- 工具定位 ----------------
function Get-Vmrun {
  if ($Hypervisor -eq "virtualbox") { return $null }
  $cands = @(
    "$env:ProgramFiles (x86)\VMware\VMware Workstation\vmrun.exe",
    "$env:ProgramFiles\VMware\VMware Workstation\vmrun.exe"
  )
  foreach ($c in $cands) { if (Test-Path $c) { return $c } }
  $p = Get-Command vmrun -ErrorAction SilentlyContinue
  return if ($p) { $p.Source } else { $null }
}

$vmrun = if ($Hypervisor -eq "vmware") { Get-Vmrun } else { $null }
if ($Hypervisor -eq "vmware" -and -not $vmrun) {
  Write-Error "未找到 vmrun（VMware Workstation 未安装或不在 PATH）。"
  exit 1
}

# vmrun 统一走数组参数，规避路径含空格被截断（报错2 的根因）
function Invoke-Vmrun {
  param([Parameter(ValueFromRemainingArguments = $true)] [string[]]$VmArgs)
  & $vmrun @VmArgs
}

# ---------------- 主机 IP 探测 ----------------
function Get-HostIp {
  if ($HostIP) { return $HostIP }
  $vmnet = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.InterfaceAlias -match 'VMnet8' }
  if ($vmnet) { return $vmnet[0].IPAddress }
  $alt = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.InterfaceAlias -notmatch 'Loopback|vEthernet|VMnet|Docker' -and $_.IPAddress -ne '127.0.0.1' } |
    Select-Object -First 1
  return $alt.IPAddress
}
$ResolvedHostIp = Get-HostIp
if (-not $ResolvedHostIp) {
  Write-Error "无法自动探测主机 IP，请在 vm-test.config.ps1 显式设置 `$HostIP。"
  exit 1
}
Write-Host "主机更新源 IP：$ResolvedHostIp : $UpdatePort"

# ---------------- Guest 命令（base64 编码，规避转义）----------------
function Invoke-GuestCommand {
  param(
    [Parameter(Mandatory)] [string]$Script,
    [int]$TimeoutSec = 300,
    [switch]$NoWait
  )
  $b64 = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Script))
  if ($Hypervisor -eq "virtualbox") {
    $args = @("-u", $GuestUser, "-p", $GuestPass, "guestcontrol", $VMName, "run",
              "--exe", "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
              "--", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $b64)
    if ($NoWait) { $args = @("-u", $GuestUser, "-p", $GuestPass, "guestcontrol", $VMName, "start",
                "--exe", "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                "--", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $b64) }
    & VBoxManage @args
    return $LASTEXITCODE
  }
  $vmArgs = @("-gu", $GuestUser, "-gp", $GuestPass, "runProgramInGuest", $VmxPath,
              "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
              "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $b64)
  if ($NoWait) {
    # Start-Process 把 -ArgumentList 数组拼成命令行字符串时不会自动给含空格参数加引号，
    # 必须手动加双引号（见 .workbuddy/memory/auto-update.md 第九节坑）。
    # 注意：Wait 分支用 & @vmArgs（不加引号），两套机制不同，不可统一——
    # 若 Wait 分支也加引号，vmrun 会把引号当成参数的一部分。
    $quotedArgs = $vmArgs | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }
    Start-Process -NoNewWindow -FilePath $vmrun -ArgumentList $quotedArgs | Out-Null
    return $null
  }
  # Wait 分支：调用符 + 数组展开（splatting），OS 直接收数组，无需引号；
  # 若加引号 vmrun 会把引号当成参数的一部分。
  & $vmrun @vmArgs
  return $LASTEXITCODE
}

function Copy-ToGuest($HostFile, $GuestFile) {
  if ($Hypervisor -eq "virtualbox") {
    & VBoxManage -u $GuestUser -p $GuestPass guestcontrol $VMName copyto $HostFile $GuestFile
  } else {
    Invoke-Vmrun -gu $GuestUser -gp $GuestPass copyFileFromHostToGuest $VmxPath $HostFile $GuestFile | Out-Null
  }
}
function Copy-FromGuest($GuestFile, $HostFile) {
  if ($Hypervisor -eq "virtualbox") {
    & VBoxManage -u $GuestUser -p $GuestPass guestcontrol $VMName copyfrom $GuestFile $HostFile
  } else {
    Invoke-Vmrun -gu $GuestUser -gp $GuestPass copyFileFromGuestToHost $VmxPath $GuestFile $HostFile | Out-Null
  }
}

# ---------------- VM 状态机（报错3 的根因：revert 前必须确保 VM 已关且快照存在）----------------
function Ensure-VmReady {
  # 1) 若 VM 正在运行，硬关机（revertToSnapshot 要求 VM 处于关机态）
  $running = Invoke-Vmrun list 2>$null | Select-String ([regex]::Escape($VmxPath))
  if ($running) {
    Write-Host "VM 正在运行，先硬关机..."
    Invoke-Vmrun stop $VmxPath hard 2>$null
    Start-Sleep -Seconds 5
  }
  # 2) 断言清干净快照存在（防止操作到错误/旧副本）
  $snaps = Invoke-Vmrun listSnapshots $VmxPath 2>$null
  if (-not ($snaps | Select-String ([regex]::Escape($SnapshotName)))) {
    Write-Error "快照 '$SnapshotName' 不在该 vmx 的快照列表中（vmrun listSnapshots）。请确认 VmxPath 指向 `vmrun list` 里的运行实例。"
    exit 1
  }
  # 3) 回滚 + 无 GUI 启动
  Write-Host "回滚快照 $SnapshotName 并启动（nogui）..."
  Invoke-Vmrun revertToSnapshot $VmxPath $SnapshotName 2>$null
  Invoke-Vmrun start $VmxPath nogui 2>$null
}

function Wait-GuestReady {
  $ready = $false
  for ($i = 0; $i -lt 40; $i++) {
    $probe = @'
New-Item -ItemType Directory -Force -Path "C:\vm_e2e" | Out-Null
"ready" | Out-File -Encoding ascii C:\vm_e2e\ready.txt
'@
    try { Invoke-GuestCommand -Script $probe -TimeoutSec 15 | Out-Null } catch { }
    $tmp = Join-Path $WorkDir "ready.txt"
    try { Copy-FromGuest "$GuestWork\ready.txt" $tmp } catch { $tmp = $null }
    if ($tmp -and (Test-Path $tmp)) { $ready = $true; Remove-Item $tmp -Force; break }
    Start-Sleep -Seconds 5
  }
  return $ready
}

# ---------------- 自签证书导入（可选，$ImportCert）----------------
# 必须在「回滚快照之后」执行：快照恢复会抹掉证书，所以不能靠快照预导入。
function Import-GuestCert {
  if (-not $ImportCert) { return $true }
  $pfx = $CertPfxPath
  if (-not [System.IO.Path]::IsPathRooted($pfx)) {
    $pfx = Join-Path $PSScriptRoot $pfx
  }
  if (-not (Test-Path $pfx)) {
    Add-Result $false "证书导入失败" "找不到 $pfx（检查 `$CertPfxPath）"
    return $false
  }
  Write-Host "导入自签根证书到 guest 受信任根..."
  Copy-ToGuest (Resolve-Path $pfx).Path "$GuestWork\cert.pfx"
  # PFX 密码通过 base64 传入，避免在命令行明文出现
  $b64Pass = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($CertPassword))
  $certScript = @"
`$pw = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('$b64Pass'))
try {
  `$ok = Import-PfxCertificate -FilePath 'C:\vm_e2e\cert.pfx' -CertStoreLocation 'Cert:\LocalMachine\Root' -Password (ConvertTo-SecureString -String `$pw -AsPlainText -Force) -ErrorAction Stop
  "OK" | Out-File -Encoding ascii C:\vm_e2e\cert-result.txt
} catch {
  "FAIL:`$_" | Out-File -Encoding ascii C:\vm_e2e\cert-result.txt
}
"@
  Invoke-GuestCommand -Script $certScript -TimeoutSec 60 | Out-Null
  $tmp = Join-Path $WorkDir "cert-result.txt"
  try { Copy-FromGuest "$GuestWork\cert-result.txt" $tmp } catch { }
  $line = if (Test-Path $tmp) { (Get-Content $tmp -Raw).Trim() } else { "FAIL:no-output" }
  Remove-Item $tmp -Force -ErrorAction SilentlyContinue
  Add-Result ($line -like "OK*") "自签证书已导入 guest 受信任根" $line
  return ($line -like "OK*")
}

# ---------------- 主机侧 http.server（端口预检 + 退出清理，报错1 根因）----------------
# 所有主机侧路径一律以「本脚本所在目录的父目录（= 仓库根）」为基准，
# 不依赖当前工作目录——否则从 web/release2 等子目录启动会把相对路径拼错。
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Release2Dir = Join-Path $RepoRoot "web\release2"
$OldInstaller = Join-Path $Release2Dir "$InstallerPrefix$OldVersion.exe"
if (-not (Test-Path $OldInstaller)) {
  Write-Error "找不到旧版本安装包：$OldInstaller`n（先确保 $Release2Dir 下存在该文件，命名遵循 artifactName）"
  exit 1
}

$httpJob = $null
try {
  # 端口预检：若 9000 已被占用（含上一次异常退出的 python），先杀掉并记下 PID
  $occupied = Get-NetTCPConnection -LocalPort $UpdatePort -ErrorAction SilentlyContinue |
    Where-Object { $_.State -eq 'Listen' }
  if ($occupied) {
    foreach ($pid_ in ($occupied.OwningProcess | Sort-Object -Unique)) {
      Write-Host "端口 $UpdatePort 被 PID $pid_ 占用，尝试终止..."
      Stop-Process -Id $pid_ -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
  }

  Write-Host "启动主机更新源：python -m http.server $UpdatePort --directory $Release2Dir"
  # 路径若含空格，Start-Process -ArgumentList 不会自动加引号（同 vmrun 坑），手动加
  $dirArg = if ($Release2Dir -match '\s') { '"' + $Release2Dir + '"' } else { $Release2Dir }
  $httpJob = Start-Process -NoNewWindow -PassThru -FilePath python -ArgumentList @("-m", "http.server", "$UpdatePort", "--directory", $dirArg)
  Start-Sleep -Seconds 2

  # 0) VM 状态机 + 就绪
  Ensure-VmReady
  Write-Host "等待 guest 就绪（VMware Tools + 自动登录）..."
  if (-not (Wait-GuestReady)) {
    Write-Host "诊断：vmrun list 输出如下（确认 VM 是否在运行实例列表）"
    Invoke-Vmrun list 2>$null | ForEach-Object { Write-Host "  $_" }
    Write-Host "诊断：该 vmx 的快照列表"
    Invoke-Vmrun listSnapshots $VmxPath 2>$null | ForEach-Object { Write-Host "  $_" }
    Write-Error "VM 未能就绪（VMware Tools / 自动登录 可能未配置）。"
    exit 1
  }
  Add-Result $true "VM 回滚至干净快照并启动"

  # 0.5) 可选：导入自签根证书（必须在回滚之后，否则会被快照恢复抹掉）
  Write-Host "检查自签证书导入（ImportCert=$ImportCert）..."
  Import-GuestCert | Out-Null

  # 1) 拷入 0.2.1 并静默安装（轮询 + 诊断 dump + 路径兜底）
  Write-Host "拷入旧版安装包并静默安装：$OldInstaller"
  #    坑：electron-builder 的 NSIS /S 是 stub 行为，Start-Process -Wait 等到的是 stub 退出，
  #    真正的安装进程仍在写注册表 → 立即查注册表会拿到空值。故改为「不依赖 -Wait + 轮询」。
  Copy-ToGuest (Resolve-Path $OldInstaller).Path "$GuestWork\installer.exe"
  $installScript = @'
$ErrorActionPreference = "Continue"
$setup = "C:\vm_e2e\installer.exe"
$exe = ""
$source = ""
$cmdline = "$setup /S"

# --- 安装前快照：安装包 MOTW 证据 ---
$motw = "none"
try {
  $zi = Get-Item -Path "$setup`:Zone.Identifier" -Stream Zone.Identifier -ErrorAction SilentlyContinue
  if ($zi) { $motw = "present" }
} catch { }

Unblock-File $setup -ErrorAction SilentlyContinue

# 记录安装前的 Uninstall 项，便于差分判断「本次新增了哪条」
$regRoots = @(
  "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall",
  "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall",
  "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
)
function Get-MatchItems {
  $out = @()
  foreach ($r in $regRoots) {
    $items = Get-ChildItem $r -ErrorAction SilentlyContinue |
      Get-ItemProperty -ErrorAction SilentlyContinue |
      Where-Object { $_.DisplayName -match '变声|voice-morph|voicemorph' }
    foreach ($i in $items) {
      $out += [pscustomobject]@{
        Name        = [string]$i.PSChildName
        DisplayName = [string]$i.DisplayName
        DisplayIcon = [string]$i.DisplayIcon
      }
    }
  }
  return $out
}
$before = @(Get-MatchItems)
$beforeNames = @($before | ForEach-Object { $_.Name })

# --- 启动 /S 安装（不 -Wait，避免 NSIS stub 提前返回造成的假完成） ---
Start-Process -FilePath $setup -ArgumentList '/S' | Out-Null

# --- 轮询最多 60s：注册表出现「新增项且 DisplayIcon 指向真实文件」---
$deadline = (Get-Date).AddSeconds(60)
$after = @()
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Seconds 2
  $after = @(Get-MatchItems)
  foreach ($i in $after) {
    $p = ($i.DisplayIcon -split ',')[0].Trim('"')
    if ($p -and (Test-Path $p)) { $exe = $p; $source = "registry"; break }
  }
  if ($exe) { break }
}

# --- 兜底：注册表没写全时，扫常见安装位置 ---
if (-not $exe) {
  $cands = @()
  $la = $env:LOCALAPPDATA
  $pf = $env:ProgramFiles
  $pf86 = ${env:ProgramFiles(x86)}
  foreach ($root in @("$la\Programs", "$pf", "$pf86")) {
    if (-not $root -or -not (Test-Path $root)) { continue }
    $cands += Get-ChildItem $root -Directory -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -match 'voice-morph|voicemorph|变声' }
  }
  foreach ($d in $cands) {
    $hit = Get-ChildItem $d.FullName -Recurse -Filter *.exe -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -notmatch 'uninstall|elevate|crashpad' } |
      Select-Object -First 1
    if ($hit) { $exe = $hit.FullName; $source = "scan"; break }
  }
}

# --- 诊断 dump：无论成败都写，供下一轮定性（SmartScreen / 时序 / 路径）---
$diag = [ordered]@{}
$diag.cmdline       = $cmdline
$diag.motw          = $motw
$diag.exe           = $exe
$diag.source        = $source
$diag.uninstallAll  = @($after)
$diag.uninstallNew  = @($after | Where-Object { $beforeNames -notcontains $_.Name })
$procs = @()
foreach ($pn in @('installer', '变声工坊', 'voice-morph-desktop', 'elevate')) {
  $procs += @(Get-Process -Name $pn -ErrorAction SilentlyContinue | ForEach-Object { $_.ProcessName })
}
$diag.procsStillRunning = @($procs)
$la = $env:LOCALAPPDATA
$diag.localProgramsDirs = @()
if (Test-Path "$la\Programs") {
  $diag.localProgramsDirs = @(Get-ChildItem "$la\Programs" -Directory -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty Name)
}
$diag.tempNsisLogs = @()
try {
  $diag.tempNsisLogs = @(Get-ChildItem $env:TEMP -Filter "*.log" -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match 'nsis|setup|install|installer' } |
    Select-Object -First 10 -ExpandProperty Name)
} catch { }
$diag | ConvertTo-Json -Depth 5 | Set-Content C:\vm_e2e\install-diag.json

[pscustomobject]@{ exe = $exe; source = $source } | ConvertTo-Json | Set-Content C:\vm_e2e\install-result.json
'@
  Invoke-GuestCommand -Script $installScript -TimeoutSec 150 | Out-Null
  $tmp = Join-Path $WorkDir "install-result.json"
  Copy-FromGuest "$GuestWork\install-result.json" $tmp
  $installInfo = Get-Content $tmp -Raw | ConvertFrom-Json
  Remove-Item $tmp -Force

  # 诊断文件一并拷回（即使后续 FAIL 也留证据）
  $diagTmp = Join-Path $WorkDir "install-diag.json"
  try { Copy-FromGuest "$GuestWork\install-diag.json" $diagTmp } catch { }

  if (-not $installInfo.exe -or -not ($installInfo.exe -like '*.exe')) {
    $hint = ""
    if (Test-Path $diagTmp) {
      try {
        $d = Get-Content $diagTmp -Raw | ConvertFrom-Json
        $hint = "诊断：source=$($d.source) motw=$($d.motw) 残留进程=[$($d.procsStillRunning -join ',')] " +
                "LOCALAPPDATA\Programs=[$($d.localProgramsDirs -join ',')] 见 $diagTmp"
      } catch { }
    }
    Write-Error "静默安装后未能定位 exe 路径。$hint"
    exit 1
  }
  Add-Result $true "0.2.1 静默安装成功" "$($installInfo.exe)（来源：$($installInfo.source)）"

  # 2) 预检更新源可达（在 VM 内访问主机 latest.json）
  $srcCheck = @'
$url = "http://__HOSTIP__:__PORT__/latest.json"
try {
  $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 15
  $m = $r.Content | ConvertFrom-Json
  if ($m.version -and $m.url -and $m.sha256) { "PASS:$($m.version)" | Out-File -Encoding ascii C:\vm_e2e\src-check.txt }
  else { "FAIL:missing-fields" | Out-File -Encoding ascii C:\vm_e2e\src-check.txt }
} catch { "FAIL:$_" | Out-File -Encoding ascii C:\vm_e2e\src-check.txt }
'@ -replace '__HOSTIP__', $ResolvedHostIp -replace '__PORT__', "$UpdatePort"
  Invoke-GuestCommand -Script $srcCheck -TimeoutSec 30 | Out-Null
  $tmp = Join-Path $WorkDir "src-check.txt"
  Copy-FromGuest "$GuestWork\src-check.txt" $tmp
  $srcLine = if (Test-Path $tmp) { (Get-Content $tmp -Raw).Trim() } else { "FAIL:no-output" }
  Remove-Item $tmp -Force
  Add-Result ($srcLine -like "PASS:*") "更新源可达（latest.json 合法）" $srcLine

  # 2.5) 清掉 NSIS 安装器残留（installer/elevate）再拉起 app。
  #      /S 安装不 -Wait（NSIS stub 会提前返回），所以这里必须在拉起前显式等它退干净，
  #      否则安装目录里的 exe 可能仍被锁 → app 起来即崩、Chromium 日志 0 字节。
  #      实测证据见 .workbuddy/memory/auto-update.md 第九节（install-diag.json
  #      procsStillRunning=["installer"]）。这是纯防御，不改链路逻辑。
  $clearInstaller = @'
$names = @('installer', 'elevate', 'Au_')
$deadline = (Get-Date).AddSeconds(45)
while ((Get-Date) -lt $deadline) {
  $alive = @()
  foreach ($n in $names) {
    $alive += @(Get-Process -Name $n -ErrorAction SilentlyContinue |
      Where-Object { -not $_.HasExited } | ForEach-Object { $_.ProcessName })
  }
  if ($alive.Count -eq 0) { break }
  Start-Sleep -Seconds 2
}
# 还活着就强杀（elevate 是 UAC 提权代理，杀掉不影响已完成的安装）
$killed = @()
foreach ($n in $names) {
  $ps = @(Get-Process -Name $n -ErrorAction SilentlyContinue)
  if ($ps.Count -gt 0) {
    $killed += $ps | ForEach-Object { $_.ProcessName }
    $ps | Stop-Process -Force -ErrorAction SilentlyContinue
  }
}
[pscustomobject]@{
  killed = @($killed)
  at     = (Get-Date).ToString('o')
} | ConvertTo-Json -Depth 3 | Set-Content C:\vm_e2e\clear-installer.json
'@
  Invoke-GuestCommand -Script $clearInstaller -TimeoutSec 90 | Out-Null
  $tmp = Join-Path $WorkDir "clear-installer.json"
  try { Copy-FromGuest "$GuestWork\clear-installer.json" $tmp } catch { }
  if (Test-Path $tmp) {
    try {
      $ci = Get-Content $tmp -Raw | ConvertFrom-Json
      if (@($ci.killed).Count -gt 0) {
        Write-Host "  已清理安装器残留：[$(($ci.killed) -join ',')]"
      }
    } catch { }
    Remove-Item $tmp -Force
  }

  # 3) 拉起 app（自动更新钩子 + 结果路径 + 日志重定向落盘）
  #    注意：环境变量靠「guest 内 PowerShell 进程设 $env: + Start-Process 子进程继承」注入，
  #    vmrun 本身没有传 env 的参数（这是唯一可行的注入方式）。
  #    日志路径全部落在 C:\vm_e2e\，由 GuestLogPattern 采集。
  $launchScript = @'
$exe = (Get-Content C:\vm_e2e\install-result.json | ConvertFrom-Json).exe
$env:VM_UPDATE_URL = "http://__HOSTIP__:__PORT__/latest.json"
$env:VM_UPDATE_TEST_AUTO = "1"
$env:VM_UPDATE_TEST_RESULT = "C:\vm_e2e\update-check-result.json"
# 记录即将用的启动命令与 env，供失败时定性（不改行为）
[pscustomobject]@{
  exe     = $exe
  exeExists = (Test-Path $exe)
  args    = @("--enable-logging=file", "--log-file=C:\vm_e2e\app.chromium.log")
  env     = [pscustomobject]@{
    VM_UPDATE_URL        = $env:VM_UPDATE_URL
    VM_UPDATE_TEST_AUTO  = $env:VM_UPDATE_TEST_AUTO
    VM_UPDATE_TEST_RESULT = $env:VM_UPDATE_TEST_RESULT
  }
  interactive = [Environment]::UserInteractive
  sessionId   = (Get-Process -Id $PID).SessionId
} | ConvertTo-Json -Depth 4 | Set-Content C:\vm_e2e\launch-params.json
Start-Process -FilePath $exe -ArgumentList "--enable-logging=file","--log-file=C:\vm_e2e\app.chromium.log" `
  -RedirectStandardOutput "C:\vm_e2e\app.stdout.log" -RedirectStandardError "C:\vm_e2e\app.stderr.log"
'@ -replace '__HOSTIP__', $ResolvedHostIp -replace '__PORT__', "$UpdatePort"
  Invoke-GuestCommand -Script $launchScript -NoWait | Out-Null
  Start-Sleep -Seconds $AppStartWaitSec

  # 4) 轮询：更新检测/下载/静默安装是否完成（结果文件写出 + app 进程退出）
  #    诊断增强（纯观测，不改判定逻辑）：
  #      - 同时统计三个可能的进程名（安装后的 exe 名来自 productName「变声工坊」，
  #        但也可能是 voice-morph-desktop / electron），避免只查一个名字而误判 running=0
  #      - 每轮把进程快照 + stderr 累积写成 app-running-<n>.json 时间序列，
  #        这样「app 从未启动」「起来又崩」「一直在跑」三种情况可区分
  $done = $false
  $deadline = (Get-Date).AddSeconds($InstallTimeoutSec)
  $pollNo = 0
  while ((Get-Date) -lt $deadline) {
    $pollNo += 1
    $poll = @'
$exename = (Get-Content C:\vm_e2e\install-result.json | ConvertFrom-Json).exe
$name = [System.IO.Path]::GetFileNameWithoutExtension($exename)
# 三个候选进程名：主 exe 名 + 打包目录名 + 通用 electron
$candNames = @($name, 'voice-morph-desktop', 'electron') | Select-Object -Unique
$counts = [ordered]@{}
foreach ($n in $candNames) {
  $counts[$n] = @(Get-Process -Name $n -ErrorAction SilentlyContinue).Count
}
$running = $counts[$name]
# stderr 内容（Electron 崩溃时这里会有退出原因）
$stderr = ""
if (Test-Path C:\vm_e2e\app.stderr.log) {
  try { $stderr = (Get-Content C:\vm_e2e\app.stderr.log -Raw -ErrorAction SilentlyContinue) } catch { }
  if ($null -eq $stderr) { $stderr = "" }
}
# 各类日志大小，判断 Chromium 是否真的初始化过
$sizes = [ordered]@{}
foreach ($f in @('app.chromium.log', 'app.stdout.log', 'app.stderr.log')) {
  $p = "C:\vm_e2e\$f"
  $sizes[$f] = if (Test-Path $p) { (Get-Item $p).Length } else { -1 }
}
$hasResult = Test-Path C:\vm_e2e\update-check-result.json
$snap = [ordered]@{
  n        = __N__
  at       = (Get-Date).ToString('o')
  exe      = $exename
  counts   = $counts
  running  = $running
  hasResult = $hasResult
  sizes    = $sizes
  stderr   = $stderr
}
$snap | ConvertTo-Json -Depth 5 | Set-Content C:\vm_e2e\app-running.json
$snap | ConvertTo-Json -Depth 5 | Set-Content ("C:\vm_e2e\app-running-" + __N__ + ".json")
'@ -replace '__N__', "$pollNo"
    Invoke-GuestCommand -Script $poll -TimeoutSec 30 | Out-Null
    $tmp = Join-Path $WorkDir "app-running.json"
    Copy-FromGuest "$GuestWork\app-running.json" $tmp
    $snapObj = Get-Content $tmp -Raw | ConvertFrom-Json
    $running = $snapObj.running
    Remove-Item $tmp -Force
    # 结果文件已写出（VM_UPDATE_TEST_RESULT 指向 C:\vm_e2e）= 检测到更新并触发自动安装
    $resTmp = Join-Path $WorkDir "update-check-result.json"
    $got = $false
    try { Copy-FromGuest "$GuestWork\update-check-result.json" $resTmp; $got = $true } catch { }
    if ($got -and $running -eq 0) { $done = $true; break }
    Start-Sleep -Seconds $PollIntervalSec
  }
  # 超时时把最后一轮快照作为证据打出来（不只看「超时」三个字）
  if (-not $done) {
    $evi = ""
    $lastTmp = Join-Path $WorkDir "app-running.json"
    try { Copy-FromGuest "$GuestWork\app-running.json" $lastTmp } catch { }
    if (Test-Path $lastTmp) {
      try {
        $s = Get-Content $lastTmp -Raw | ConvertFrom-Json
        $sz = ($s.sizes.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)B" }) -join " "
        $cnt = ($s.counts.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join " "
        $errTxt = if ($s.stderr) { ($s.stderr -replace "\r?\n", " | ") } else { "(空)" }
        $evi = "轮询 $($s.n) 次；进程[$cnt]；日志[$sz]；结果文件=$($s.hasResult)；stderr=$errTxt"
      } catch { }
      Remove-Item $lastTmp -Force
    }
    # 附带 launch-params（会话/交互性）与 clear-installer 证据
    $lpTmp = Join-Path $WorkDir "launch-params.json"
    try { Copy-FromGuest "$GuestWork\launch-params.json" $lpTmp } catch { }
    if (Test-Path $lpTmp) {
      try {
        $lp = Get-Content $lpTmp -Raw | ConvertFrom-Json
        $evi += "；UserInteractive=$($lp.interactive) sessionId=$($lp.sessionId) exeExists=$($lp.exeExists)"
      } catch { }
      Remove-Item $lpTmp -Force
    }
    Add-Result $false "更新链路在超时内未完成（自动下载/静默安装/退出）" $evi
  } else {
    Add-Result $true "更新检测→下载→静默安装→退出 已完成"
  }

  # 5) 确认 0.2.2 生效：重读磁盘 exe 版本（先清可能的 electron 残留，缓解 NSIS /S 覆盖失败）
  $killResidual = @'
$exename = (Get-Content C:\vm_e2e\install-result.json | ConvertFrom-Json).exe
$name = [System.IO.Path]::GetFileNameWithoutExtension($exename)
Get-Process -Name $name -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
'@
  Invoke-GuestCommand -Script $killResidual -TimeoutSec 15 | Out-Null
  $verScript = @'
$exe = (Get-Content C:\vm_e2e\install-result.json | ConvertFrom-Json).exe
$v = (Get-Item $exe).VersionInfo.FileVersion
[pscustomobject]@{ version = $v } | ConvertTo-Json | Set-Content C:\vm_e2e\version.json
'@
  Invoke-GuestCommand -Script $verScript -TimeoutSec 30 | Out-Null
  $tmp = Join-Path $WorkDir "version.json"
  Copy-FromGuest "$GuestWork\version.json" $tmp
  $ver = (Get-Content $tmp -Raw | ConvertFrom-Json).version
  Remove-Item $tmp -Force
  Add-Result ($ver -eq $NewVersion) "版本生效" "磁盘 exe 版本=$ver，期望 $NewVersion"

  # 6) 日志回归判据：生产模式已加载 且 不含 D:\变声
  $logHost = Join-Path $WorkDir "app.log"
  $all = ""
  foreach ($gf in $GuestLogPattern) {
    $tmp = Join-Path $WorkDir ([System.IO.Path]::GetFileName($gf))
    try { Copy-FromGuest $gf $tmp } catch { }
    if (Test-Path $tmp) { $all += (Get-Content $tmp -Raw -ErrorAction SilentlyContinue) + "`n" }
  }
  $hasFrontend = $all -match '\[frontend\] 生产模式'
  $hasBackend = $all -match '\[backend\] 生产模式后端根'
  $leak = ($all -match 'D:\\变声')
  Add-Result ($hasFrontend -and $hasBackend -and -not $leak) "日志路径回归判据（生产模式+不含 D:\变声）" $(
    if (-not ($hasFrontend -and $hasBackend)) { "未检测到生产模式日志行（可能日志未落盘）" }
    elseif ($leak) { "发现 D:\变声 泄漏" }
    else { "OK" })

  # 7) 截图（best-effort，需 VM 交互桌面已解锁/自动登录）
  $shotScript = @'
try {
  Add-Type -AssemblyName System.Windows.Forms
  Add-Type -AssemblyName System.Drawing
  $s = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
  $b = New-Object System.Drawing.Bitmap($s.Width, $s.Height)
  $g = [System.Drawing.Graphics]::FromImage($b)
  $g.CopyFromScreen($s.Location, [System.Drawing.Point]::Empty, $s.Size)
  $b.Save("C:\vm_e2e\screenshot.png")
  "ok" | Out-File -Encoding ascii C:\vm_e2e\shot.txt
} catch { "fail:$_" | Out-File -Encoding ascii C:\vm_e2e\shot.txt }
'@
  Invoke-GuestCommand -Script $shotScript -TimeoutSec 30 | Out-Null
  $shotHost = Join-Path $WorkDir "screenshot.png"
  try { Copy-FromGuest "$GuestWork\screenshot.png" $shotHost; Add-Result $true "截图已保存" $shotHost } catch { Add-Result $true "截图跳过（无交互桌面/失败，非阻断）" }

  # 8) 拷日志/结果回主机（已在 WorkDir）
  Add-Result $true "日志与结果已拷回主机" $WorkDir

} catch {
  Add-Result $false "未预期异常" $_.Exception.Message
} finally {
  if ($httpJob -and -not $httpJob.HasExited) { Stop-Process -Id $httpJob.Id -Force -ErrorAction SilentlyContinue }
  Write-Host "已停止本脚本启动的 python http.server（PID $($httpJob.Id)）。"
}

# ---------------- 汇总 ----------------
Write-Host "`n================ 端到端测试汇总 ================"
foreach ($line in $Summary) { Write-Host $line }
Write-Host "================================================="
if ($Pass) { Write-Host "RESULT: PASS" } else { Write-Host "RESULT: FAIL" }
exit $(if ($Pass) { 0 } else { 1 })
