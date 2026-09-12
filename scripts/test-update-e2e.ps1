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
    Start-Process -NoNewWindow -FilePath $vmrun -ArgumentList $vmArgs | Out-Null
    return $null
  }
  $p = Start-Process -NoNewWindow -PassThru -Wait -FilePath $vmrun -ArgumentList $vmArgs
  return $p.ExitCode
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

# ---------------- 主机侧 http.server（端口预检 + 退出清理，报错1 根因）----------------
$OldInstaller = "web/release2/$InstallerPrefix$OldVersion.exe"
if (-not (Test-Path $OldInstaller)) {
  Write-Error "找不到旧版本安装包：$OldInstaller（先确保 release2 下存在该文件，命名遵循 artifactName）。"
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

  Write-Host "启动主机更新源：python -m http.server $UpdatePort --directory web/release2"
  $httpJob = Start-Process -NoNewWindow -PassThru -FilePath python -ArgumentList @("-m", "http.server", "$UpdatePort", "--directory", "web/release2")
  Start-Sleep -Seconds 2

  # 0) VM 状态机 + 就绪
  Ensure-VmReady
  if (-not (Wait-GuestReady)) {
    Write-Error "VM 未能就绪（VMware Tools / 自动登录 可能未配置）。"
    exit 1
  }
  Add-Result $true "VM 回滚至干净快照并启动"

  # 1) 拷入 0.2.1 并静默安装
  Copy-ToGuest (Resolve-Path $OldInstaller).Path "$GuestWork\installer.exe"
  $installScript = @'
$setup = "C:\vm_e2e\installer.exe"
Unblock-File $setup -ErrorAction SilentlyContinue
Start-Process -FilePath $setup -ArgumentList '/S' -Wait
$icon = Get-ChildItem "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall",
  "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall",
  "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall" -ErrorAction SilentlyContinue |
  Get-ItemProperty -ErrorAction SilentlyContinue |
  Where-Object { $_.DisplayName -match '变声|voice-morph|voicemorph' } |
  Select-Object -ExpandProperty DisplayIcon -First 1
$exe = ($icon -split ',')[0].Trim('"')
if (-not $exe -or -not (Test-Path $exe)) { $exe = "" }
[pscustomobject]@{ exe = $exe } | ConvertTo-Json | Set-Content C:\vm_e2e\install-result.json
'@
  Invoke-GuestCommand -Script $installScript -TimeoutSec 120 | Out-Null
  $tmp = Join-Path $WorkDir "install-result.json"
  Copy-FromGuest "$GuestWork\install-result.json" $tmp
  $installInfo = Get-Content $tmp -Raw | ConvertFrom-Json
  Remove-Item $tmp -Force
  if (-not $installInfo.exe -or -not $installInfo.exe.EndsWith('.exe')) {
    Write-Error "静默安装后未能定位 exe 路径（安装可能弹了 SmartScreen 向导）。若如此请开启 `$ImportCert 或在快照里导入自签根证书。"
    exit 1
  }
  Add-Result $true "0.2.1 静默安装成功" $installInfo.exe

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

  # 3) 拉起 app（自动更新钩子 + 结果路径 + 日志重定向落盘）
  $launchScript = @'
$exe = (Get-Content C:\vm_e2e\install-result.json | ConvertFrom-Json).exe
$env:VM_UPDATE_URL = "http://__HOSTIP__:__PORT__/latest.json"
$env:VM_UPDATE_TEST_AUTO = "1"
$env:VM_UPDATE_TEST_RESULT = "C:\vm_e2e\update-check-result.json"
Start-Process -FilePath $exe -ArgumentList "--enable-logging=file","--log-file=C:\vm_e2e\app.chromium.log" `
  -RedirectStandardOutput "C:\vm_e2e\app.stdout.log" -RedirectStandardError "C:\vm_e2e\app.stderr.log"
'@ -replace '__HOSTIP__', $ResolvedHostIp -replace '__PORT__', "$UpdatePort"
  Invoke-GuestCommand -Script $launchScript -NoWait | Out-Null
  Start-Sleep -Seconds $AppStartWaitSec

  # 4) 轮询：更新检测/下载/静默安装是否完成（结果文件写出 + app 进程退出）
  $done = $false
  $deadline = (Get-Date).AddSeconds($InstallTimeoutSec)
  while ((Get-Date) -lt $deadline) {
    $poll = @'
$exename = (Get-Content C:\vm_e2e\install-result.json | ConvertFrom-Json).exe
$name = [System.IO.Path]::GetFileNameWithoutExtension($exename)
$running = @(Get-Process -Name $name -ErrorAction SilentlyContinue).Count
[pscustomobject]@{ running = $running } | ConvertTo-Json | Set-Content C:\vm_e2e\app-running.json
'@
    Invoke-GuestCommand -Script $poll -TimeoutSec 30 | Out-Null
    $tmp = Join-Path $WorkDir "app-running.json"
    Copy-FromGuest "$GuestWork\app-running.json" $tmp
    $running = (Get-Content $tmp -Raw | ConvertFrom-Json).running
    Remove-Item $tmp -Force
    # 结果文件已写出（VM_UPDATE_TEST_RESULT 指向 C:\vm_e2e）= 检测到更新并触发自动安装
    $resTmp = Join-Path $WorkDir "update-check-result.json"
    $got = $false
    try { Copy-FromGuest "$GuestWork\update-check-result.json" $resTmp; $got = $true } catch { }
    if ($got -and $running -eq 0) { $done = $true; break }
    Start-Sleep -Seconds $PollIntervalSec
  }
  if (-not $done) {
    Add-Result $false "更新链路在超时内未完成（自动下载/静默安装/退出）"
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
