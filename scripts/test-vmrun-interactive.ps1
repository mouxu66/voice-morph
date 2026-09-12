# test-vmrun-interactive.ps1
# 断言 test-update-e2e.ps1 在拉起 app 时，把 -interactive 插在**正确位置**。
#
# 背景：vmrun 的官方参数序是
#   runProgramInGuest <vmx> [-noWait] [-activeWindow] [-interactive] <程序> [参数]
# 即标志必须落在 **$VmxPath 之后、程序路径之前**。写反了 vmrun 不做参数校验、
# 直接报错退出（或更糟：把 -interactive 当成程序路径），是「静默失败」的典型来源。
#
# 手法：把 test-update-e2e.ps1 里 Invoke-GuestCommand 的**启动段**当成纯文本契约来验，
# 用 mock vmrun（.cmd，把 %* 落到文件）复现两种分支的实参顺序：
#   A) 直接用 & $vmrun @vmArgs 展开（Wait 分支）—— 暴露真实参数序
#   B) 复刻脚本里的 $flags 拼接表达式 —— 断言 -interactive 相对位置
# 之所以不 dot-source 整个 e2e 脚本：它顶层就会 load 配置 + 连 VM，单测不能碰真机。
# 所以这里**对齐的是表达式契约**（与 test-update-e2e.ps1 里的构造写法逐字一致）。
#
# 运行：pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/test-vmrun-interactive.ps1

$ErrorActionPreference = "Stop"
$scriptDir = $PSScriptRoot
$e2ePath = Join-Path $scriptDir "test-update-e2e.ps1"

$results = [System.Collections.Generic.List[string]]::new()
$allOk = $true
function Report([bool]$ok, [string]$label, [string]$detail) {
  if (-not $ok) { $script:allOk = $false }
  $results.Add(("[{0}] {1} -- {2}" -f $(if ($ok) { "PASS" } else { "FAIL" }), $label, $detail))
}

# ---------------- 1. 源码文本契约：确认脚本里确实用了 $flags 拼接，且位置在 $VmxPath 之后 ----------------
$src = Get-Content $e2ePath -Raw
$expressions = @(
  @{ ln = '$vmArgs = @("-gu", $GuestUser, "-gp", $GuestPass, "runProgramInGuest", $VmxPath) + $flags +'; why = "flags 拼接在 VmxPath 之后（行尾 + 续行）" },
  @{ ln = '$vmArgs = @("-gu", $GuestUser, "-gp", $GuestPass, "runProgramInGuest", $VmxPath) + $flags'; why = "flags 拼接在 VmxPath 之后（同行）" }
)
$found = $false
foreach ($e in $expressions) { if ($src.Contains($e.ln.TrimEnd(' +').TrimEnd()) -or $src.Contains($e.ln)) { $found = $true } }
Report $found "e2e 源码使用 @(... '$VmxPath') + `$flags 构造实参" $(if ($found) { "ok" } else { "未找到拼接线，构造写法可能已改" })

$hasSwitch = ($src -match '\[switch\]\$Interactive')
Report $hasSwitch "Invoke-GuestCommand 声明 -Interactive 开关" $(if ($hasSwitch) { "ok" } else { "缺少 [switch]\$Interactive" })

$flagPush = ($src -match 'if\s*\(\s*\$Interactive\s*\)\s*\{\s*\$flags\s*\+=\s*"-interactive"\s*\}')
Report $flagPush "仅在 -Interactive 时压入 -interactive" $(if ($flagPush) { "ok" } else { "未找到 `$flags += '-interactive'" })

# 反例：严禁写成 runProgramInGuest 之前（即 "-interactive" 出现在 $VmxPath 之前）
$wrongOrder = ($src -match 'runProgramInGuest"\s*,\s*"-interactive"')
Report (-not $wrongOrder) "未出现「-interactive 在 vmx 之前」的错误写法" $(if (-not $wrongOrder) { "clean" } else { "发现错序" })

# ---------------- 2. 运行期实参序：mock vmrun 复现两种分支 ----------------
$tmp = Join-Path $env:TEMP ("vmrun_int_test_" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$mock = Join-Path $tmp "mockvmrun.cmd"
$recvFile = Join-Path $tmp "received.txt"
# mock vmrun：把收到的所有参数原样落盘（%* 保留引号，便于断言引号缺失与否）
Set-Content -Path $mock -Value @("@echo off", "echo %* > `"$recvFile`"") -Encoding ascii

function Get-Received {
  if (-not (Test-Path $recvFile)) { return "" }
  return ((Get-Content $recvFile -Raw) -replace '"', '').Trim()
}

# 复刻 e2e 里的构造（逐字一致，见上方源码断言）
$GuestUser = "jjjj"
$GuestPass = "pw"
$VmxPath = "D:\Virtual Machines\Windows 10 x64\Windows 10 x64.vmx"
$B64 = "BASE64=="
$ProgramPath = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

function Build-VmArgs([bool]$interactive) {
  $flags = @()
  if ($interactive) { $flags += "-interactive" }
  return @("-gu", $GuestUser, "-gp", $GuestPass, "runProgramInGuest", $VmxPath) + $flags +
         @($ProgramPath, "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $B64)
}

function Get-IndexOfFirst([string[]]$arr, [string]$needle) {
  for ($i = 0; $i -lt $arr.Count; $i++) { if ($arr[$i] -eq $needle) { return $i } }
  return -1
}

# 分支 A：-Interactive = $true（默认路径）
Remove-Item $recvFile -Force -ErrorAction SilentlyContinue
$vmArgsOn = Build-VmArgs $true
& $mock @vmArgsOn
$recvOn = Get-Received
$iProg = Get-IndexOfFirst $vmArgsOn $ProgramPath
$iVmx  = Get-IndexOfFirst $vmArgsOn $VmxPath
$iInt  = Get-IndexOfFirst $vmArgsOn "-interactive"
Report ($iInt -gt $iVmx) "-interactive 在 vmx 之后" ("vmx索引=$iVmx interactive索引=$iInt")
Report ($iInt -lt $iProg) "-interactive 在程序路径之前" ("interactive索引=$iInt 程序索引=$iProg")
Report ($recvOn -like "*runProgramInGuest*$VmxPath*interactive*") "mock 收到实参序正确（vmrun 展开无引号污染）" $recvOn
# 引号污染自查：Wait 分支用 & @vmArgs（不加引号），收到的应是裸参、路径完整
$vnNoQuote = ($recvOn -notmatch '"')
Report $vnNoQuote "Wait 分支未给参数加引号（含空格 vmx 路径未被截断）" $(if ($vnNoQuote) { "no quotes" } else { "发现引号，路径可能被截断" })

# 分支 B：-Interactive = $false（不应出现标志）
Remove-Item $recvFile -Force -ErrorAction SilentlyContinue
$vmArgsOff = Build-VmArgs $false
& $mock @vmArgsOff
$recvOff = Get-Received
Report ($recvOff -notlike "*interactive*") "未加开关时不出现 -interactive" $recvOff

# 分支 C：标志紧随 vmx（相邻），即中间没有别的东西插进来
$adjacent = ($iProg -eq $iVmx + 2) -and ($iInt -eq $iVmx + 1)
Report $adjacent "-interactive 紧贴 vmx 之后（无多余参数）" ("vmx=$iVmx int=$iInt prog=$iProg")

Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "===== test-vmrun-interactive ====="
foreach ($r in $results) { Write-Host $r }
Write-Host "=================================="
if ($allOk) { Write-Host "RESULT: PASS"; exit 0 } else { Write-Host "RESULT: FAIL"; exit 1 }
