# test-install-probe.ps1
# 验证「静默安装后定位 exe」的两条修复路径：
#   case1 注册表延迟写入 -> 轮询能等到（模拟 NSIS stub 提前返回）
#   case2 注册表始终为空但路径存在 -> 兜底扫描命中
# 通过注入 -RegistryProvider / -ScanProvider 桩实现，不触碰真实注册表。
# 运行：powershell -ExecutionPolicy Bypass -File scripts/test-install-probe.ps1

$ErrorActionPreference = "Stop"

# ---------- 被测逻辑（与 test-update-e2e.ps1 installScript 同构，抽成可注入函数） ----------
function Resolve-InstalledExe {
  param(
    [scriptblock]$RegistryProvider,   # () -> @([pscustomobject]@{DisplayIcon=...})
    [scriptblock]$ScanProvider,       # () -> @("C:\...\app.exe")
    [int]$TimeoutSec = 60,
    [int]$IntervalMs = 50            # 测试用短间隔
  )
  $exe = ""
  $source = ""
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  while ((Get-Date) -lt $deadline) {
    $items = @(& $RegistryProvider)
    foreach ($i in $items) {
      $p = ($i.DisplayIcon -split ',')[0].Trim('"')
      if ($p -and (Test-Path $p)) { $exe = $p; $source = "registry"; break }
    }
    if ($exe) { break }
    Start-Sleep -Milliseconds $IntervalMs
  }
  if (-not $exe) {
    $hits = @(& $ScanProvider)
    foreach ($h in $hits) {
      if ($h -and (Test-Path $h)) { $exe = $h; $source = "scan"; break }
    }
  }
  return [pscustomobject]@{ exe = $exe; source = $source }
}

# ---------- 测试脚手架 ----------
$tmp = New-Item -ItemType Directory -Force -Path (Join-Path $env:TEMP "install_probe_test") |
  Select-Object -ExpandProperty FullName
$fakeExe = Join-Path $tmp "voice-morph-desktop.exe"
"stub" | Set-Content -Path $fakeExe -Encoding ascii

$results = [System.Collections.Generic.List[string]]::new()
$allOk = $true
function Assert-Case([string]$label, [bool]$ok, [string]$detail) {
  if (-not $ok) { $script:allOk = $false }
  $results.Add(("[{0}] {1} -- {2}" -f $(if ($ok) { "PASS" } else { "FAIL" }), $label, $detail))
}

# ---- case1: 注册表延迟写入，第 3 次轮询才出现 ----
$script:counter = 0
$lateReg = {
  $script:counter = [int]$script:counter + 1
  if ($script:counter -lt 3) { return @() }
  return @([pscustomobject]@{ DisplayIcon = "$script:fakeExe,0" })
}
$r1 = Resolve-InstalledExe -RegistryProvider $lateReg -ScanProvider { @() } -TimeoutSec 3 -IntervalMs 50
Assert-Case "case1 registry delayed write is polled until visible" `
  ($r1.exe -eq $fakeExe -and $r1.source -eq "registry" -and $script:counter -ge 3) `
  "source=$($r1.source) polls=$($script:counter) exe=$(if ($r1.exe) { "found" } else { "empty" })"

# ---- case2: 注册表始终空，兜底扫描命中 ----
$r2 = Resolve-InstalledExe -RegistryProvider { @() } -ScanProvider { @($script:fakeExe) } `
  -TimeoutSec 2 -IntervalMs 50
Assert-Case "case2 empty registry falls back to path scan" `
  ($r2.exe -eq $fakeExe -and $r2.source -eq "scan") `
  "source=$($r2.source) exe=$(if ($r2.exe) { "found" } else { "empty" })"

# ---- case3: 两者都失败 -> 明确返回空（不误报成功） ----
$r3 = Resolve-InstalledExe -RegistryProvider { @() } -ScanProvider { @() } -TimeoutSec 2 -IntervalMs 50
Assert-Case "case3 both fail returns empty exe" `
  (($r3.exe -eq "") -and ($r3.source -eq "")) `
  "source=[$($r3.source)] exe=[$($r3.exe)]"

Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "===== test-install-probe ====="
foreach ($r in $results) { Write-Host $r }
Write-Host "=============================="
if ($allOk) { Write-Host "RESULT: PASS"; exit 0 } else { Write-Host "RESULT: FAIL"; exit 1 }
