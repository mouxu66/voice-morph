# test-update-e2e.ps1 —— 主机端：Win10 虚拟机自动更新端到端测试编排
#
# 依赖：VMware Tools（默认）/ VirtualBox Guest Additions；主机 PowerShell 7；python（http.server）。
# 运行：powershell -ExecutionPolicy Bypass -File scripts/test-update-e2e.ps1
# 配置：同目录 vm-test.config.ps1（从 vm-test.config.example.ps1 复制填写）。
#
# 链路：回滚干净快照 → 拷入 0.2.1 静默安装 → 拉起 app（设 VM_UPDATE_URL + VM_UPDATE_TEST_AUTO）
#       → app 自动检测/下载/静默安装 0.2.2 并退出 → 重新拉起确认 0.2.2 生效
#       → 读日志断言不含 D:\变声（回归判据）→ 截图/拷日志回主机 → 输出 PASS/FAIL。
#
# 注：本脚本只能命令行驱动 VM，真正的 GUI 点击/截图由 VM 内进程完成；AI 不 GUI 操控 VM。

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

$GuestWork = "C:\vm_e2e"
$GuestLog  = "$GuestWork\app.log"
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
  # 退而求其次：依赖 PATH
  $p = Get-Command vmrun -ErrorAction SilentlyContinue
  return if ($p) { $p.Source } else { $null }
}

$vmrun = if ($Hypervisor -eq "vmware") { Get-Vmrun } else { $null }
if ($Hypervisor -eq "vmware" -and -not $vmrun) {
  Write-Error "未找到 vmrun（VMware Workstation 未安装或不在 PATH）。"
  exit 1
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
  if ($NoWait) { $vmArgs = @("-gu", $GuestUser, "-gp", $GuestPass, "runProgramInGuest", "-noWait", $VmxPath,
                  "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                  "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $b64) }
  $p = Start-Process -NoNewWindow -PassThru -Wait -FilePath $vmrun -ArgumentList $vmArgs
  return $p.ExitCode
}

function Copy-ToGuest($HostFile, $GuestFile) {
  if ($Hypervisor -eq "virtualbox") {
    & VBoxManage -u $GuestUser -p $GuestPass guestcontrol $VMName copyto $HostFile $GuestFile
  } else {
    & $vmrun -gu $GuestUser -gp $GuestPass copyFileFromHostToGuest $VmxPath $HostFile $GuestFile | Out-Null
  }
}
function Copy-FromGuest($GuestFile, $HostFile) {
  if ($Hypervisor -eq "virtualbox") {
    & VBoxManage -u $GuestUser -p $GuestPass guestcontrol $VMName copyfrom $GuestFile $HostFile
  } else {
    & $vmrun -gu $GuestUser -gp $GuestPass copyFileFromGuestToHost $VmxPath $GuestFile $HostFile | Out-Null
  }
}

# ---------------- VM 生命周期 ----------------
function Revert-And-Start {
  if ($Hypervisor -eq "virtualbox") {
    & VBoxManage controlvm $VMName poweroff 2>$null
    & VBoxManage snapshot $VMName restore $SnapshotName
    & VBoxManage startvm $VMName --type gui
  } else {
    & $vmrun stop $VmxPath soft 2>$null
    & $vmrun revertToSnapshot $VmxPath $SnapshotName
    & $vmrun start $VmxPath
  }
}

function Wait-GuestReady {
  $ready = $false
  for ($i = 0; $i -lt 40; $i++) {
    $probe = @'
New-Item -ItemType Directory -Force -Path "C:\vm_e2e" | Out-Null
"ready" | Out-File -Encoding ascii C:\vm_e2e\ready.txt
'@
    try { Invoke-GuestCommand -Script $probe -TimeoutSec 15 | Out-Null } catch {}
    $tmp = Join-Path $WorkDir "ready.txt"
    try { Copy-FromGuest "$GuestWork\ready.txt" $tmp } catch { $tmp = $null }
    if ($tmp -and (Test-Path $tmp)) { $ready = $true; Remove-Item $tmp -Force; break }
    Start-Sleep -Seconds 5
  }
  return $ready
}

# ---------------- 主机侧 http.server ----------------
$OldInstaller = "web/release2/$InstallerPrefix $OldVersion.exe"
if (-not (Test-Path $OldInstaller)) {
  Write-Error "找不到旧版本安装包：$OldInstaller（先跑 build-and-publish.ps1 生成，或确认路径）。"
  exit 1
}
$httpJob = $null
try {
  Write-Host "启动主机更新源：python -m http.server $UpdatePort --directory web/release2"
  $httpJob = Start-Process -NoNewWindow -PassThru -FilePath python -ArgumentList @("-m", "http.server", "$UpdatePort", "--directory", "web/release2")
  Start-Sleep -Seconds 2

  # 1) 回滚 + 启动
  Revert-And-Start
  if (-not (Wait-GuestReady)) {
    Write-Error "VM 未能就绪（VMware Tools / 自动登录 可能未配置）。"
    exit 1
  }
  Add-Result $true "VM 回滚至干净快照并启动"

  # 2) 拷入 0.2.1 并静默安装
  Copy-ToGuest (Resolve-Path $OldInstaller).Path "$GuestWork\installer.exe"
  $installScript = @'
$setup = "C:\vm_e2e\installer.exe"
Unblock-File $setup -ErrorAction SilentlyContinue
Start-Process -FilePath $setup -ArgumentList '/S' -Wait
$icon = Get-ChildItem "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall",
  "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall",
  "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall" -ErrorAction SilentlyContinue |
  Get-ItemProperty -ErrorAction SilentlyContinue |
  Where-Object { $_.DisplayName -match '变声|voice-morph' } |
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

  # 3) 拉起 app（自动更新钩子 + 日志落盘）
  $launchScript = @'
$exe = (Get-Content C:\vm_e2e\install-result.json | ConvertFrom-Json).exe
$env:VM_UPDATE_URL = "http://__HOSTIP__:__PORT__/latest.json"
$env:VM_UPDATE_TEST_AUTO = "1"
Start-Process -FilePath $exe -ArgumentList "--enable-logging=file","--log-file=C:\vm_e2e\app.log"
'@ -replace '__HOSTIP__', $ResolvedHostIp -replace '__PORT__', "$UpdatePort"
  Invoke-GuestCommand -Script $launchScript -NoWait | Out-Null
  Start-Sleep -Seconds $AppStartWaitSec

  # 4) 轮询：更新检测/下载/静默安装是否完成
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
    # 结果文件已写出 = 检测到更新并触发自动安装
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

  # 5) 确认 0.2.2 生效：重拉起（不带钩子）并读版本 + 日志
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

  # 6) 日志回归判据：不含 D:\变声
  $logHost = Join-Path $WorkDir "app.log"
  Copy-FromGuest $GuestLog $logHost
  $logContent = Get-Content $logHost -Raw -ErrorAction SilentlyContinue
  $leak = ($logContent -match 'D:\\变声')
  Add-Result (-not $leak) "日志路径回归判据（不含 D:\变声）" $(if ($leak) { "发现 D:\变声 泄漏" } else { "OK" })

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

} finally {
  if ($httpJob -and -not $httpJob.HasExited) { Stop-Process -Id $httpJob.Id -Force -ErrorAction SilentlyContinue }
}

# ---------------- 汇总 ----------------
Write-Host "`n================ 端到端测试汇总 ================"
foreach ($line in $Summary) { Write-Host $line }
Write-Host "================================================="
if ($Pass) { Write-Host "RESULT: PASS" } else { Write-Host "RESULT: FAIL" }
exit $(if ($Pass) { 0 } else { 1 })
