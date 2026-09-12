# test-copy-fromguest.ps1
# 回归「Copy-FromGuest 把成功误判成失败」这个 bug。
#
# 背景（2026-09-12 实测）：
#   `vmrun copyFileFromGuestToHost` **成功时也不打 stdout**。旧实现靠
#   `$rc = Invoke-Vmrun copy...` 取退出码 → 函数没有 return，返回的是空输出流 →
#   `$rc -ne 0` 对空值恒为真 → 每次误报失败。而「改动 4」加的 throw 把误报升级成
#   真异常，调用方 `catch { $tmp = $null }` → Wait-GuestReady 永远不就绪。
#   症状：run-diag.log 每轮报 Copy-FromGuest 失败，但详情里「目标存在=True」。
#
# 本测试锁死两条判据：
#   ① 判定只看**目标文件是否存在**，退出码不得参与；
#   ② Invoke-Vmrun 的**返回值必须仍是 vmrun 的 stdout**（list/listSnapshots 靠它解析）。
#
# 手法：从 test-update-e2e.ps1 里用 AST 抽出 Invoke-Vmrun / Write-Diag / Copy-ToGuest /
# Copy-FromGuest 四个函数定义，注入 mock 后在本会话内 dot-source（**不**执行整个 e2e 脚本，
# 否则会加载配置 + 连 VM）。mock vmrun 用 .cmd，按环境变量模拟「有无 stdout / 退出码 / 是否真拷文件」。
#
# 运行：pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/test-copy-fromguest.ps1

$ErrorActionPreference = "Stop"
$scriptDir = $PSScriptRoot
$e2ePath = Join-Path $scriptDir "test-update-e2e.ps1"

$results = [System.Collections.Generic.List[string]]::new()
$allOk = $true
function Report([bool]$ok, [string]$label, [string]$detail) {
  if (-not $ok) { $script:allOk = $false }
  $results.Add(("[{0}] {1} -- {2}" -f $(if ($ok) { "PASS" } else { "FAIL" }), $label, $detail))
}

# ---------------- 用 AST 抽函数定义（不 dot-source 整个 e2e） ----------------
$tokens = $null; $errs = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($e2ePath, [ref]$tokens, [ref]$errs)
if ($errs -and $errs.Count -gt 0) { Report $false "e2e 脚本语法" "解析失败，无法抽函数"; }
$wanted = @("Invoke-Vmrun", "Write-Diag", "Copy-ToGuest", "Copy-FromGuest")
$funcs = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
$chunks = @()
foreach ($w in $wanted) {
  $hit = $funcs | Where-Object { $_.Name -eq $w } | Select-Object -First 1
  if (-not $hit) { Report $false "抽取函数 $w" "NOT FOUND"; continue }
  $chunks += $hit.Extent.Text
}
Report ($chunks.Count -eq $wanted.Count) "从 e2e 抽出 4 个函数定义" ("got=" + $chunks.Count)
if ($chunks.Count -ne $wanted.Count) {
  Write-Host "===== test-copy-fromguest ====="
  foreach ($r in $results) { Write-Host $r }
  Write-Host "RESULT: FAIL"; exit 1
}

# ---------------- mock vmrun ----------------
$tmp = Join-Path $env:TEMP ("copy_fromguest_test_" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$mock = Join-Path $tmp "mockvmrun.cmd"
# 行为由环境变量驱动：
#   MOCK_STDOUT : 非空则 echo 这行（模拟 list/listSnapshots 有输出）
#   MOCK_EXIT   : 退出码
#   MOCK_COPYTO : 非空则把 MOCK_COPYTO 指向的文件"拷"到 MOCK_DEST（模拟真拷贝）
$mockLines = @(
  "@echo off",
  "if not `"%MOCK_STDOUT%`"==`"`" echo %MOCK_STDOUT%",
  "if not `"%MOCK_COPYTO%`"==`"`" copy /y `"%MOCK_COPYTO%`" `"%MOCK_DEST%`" >nul 2>&1",
  "exit /b %MOCK_EXIT%"
)
Set-Content -Path $mock -Value $mockLines -Encoding ascii

# 注入 mock 依赖后定义函数
$vmrun = $mock
$Hypervisor = "vmware"
$GuestUser = "u"; $GuestPass = "p"; $VmxPath = "D:\VMs\Win10\Win10.vmx"
$WorkDir = Join-Path $tmp "work"
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

$chunkText = ($chunks -join "`n`n")
. ([scriptblock]::Create($chunkText))

function New-Src([string]$name) {
  $p = Join-Path $tmp $name
  "hello" | Set-Content -Path $p -Encoding ascii
  return $p
}
function Clear-Diag { Remove-Item (Join-Path $WorkDir "run-diag.log") -Force -ErrorAction SilentlyContinue }
function Get-Diag { if (Test-Path (Join-Path $WorkDir "run-diag.log")) { Get-Content (Join-Path $WorkDir "run-diag.log") -Raw } else { "" } }

function Invoke-Case([string]$mockStdout, [int]$mockExit, [bool]$reallyCopy, [string]$dest, [string]$src) {
  $env:MOCK_STDOUT = $mockStdout
  $env:MOCK_EXIT = "$mockExit"
  if ($reallyCopy) { $env:MOCK_COPYTO = $src; $env:MOCK_DEST = $dest }
  else { $env:MOCK_COPYTO = ""; $env:MOCK_DEST = $dest }
  if (Test-Path $dest) { Remove-Item $dest -Force }
  $threw = $false; $err = ""
  try { Copy-FromGuest "C:\vm_e2e\x.txt" $dest } catch { $threw = $true; $err = $_.Exception.Message }
  return [pscustomobject]@{ threw = $threw; err = $err }
}

# ---------------- 用例 a：空输出 + exit 0 + 真拷 → 不抛（核心回归） ----------------
Clear-Diag
$src = New-Src "a.txt"
$dst = Join-Path $WorkDir "a-host.txt"
$r = Invoke-Case "" 0 $true $dst $src
Report (-not $r.threw) "a) 空输出+rc0+真拷 → 不抛异常" $(if ($r.threw) { "误抛：$($r.err)" } else { "ok（旧实现会误报失败）" })
Report (Test-Path $dst) "a) 目标文件确实存在" $(if (Test-Path $dst) { "ok" } else { "missing" })

# ---------------- 用例 b：空输出 + exit 0 + 不拷 → 抛（存在性判据生效） ----------------
Clear-Diag
$dst2 = Join-Path $WorkDir "b-host.txt"
$r = Invoke-Case "" 0 $false $dst2 $src
Report $r.threw "b) rc0 但文件未落到主机 → 抛异常" $(if ($r.threw) { "ok" } else { "未抛，存在性判据失效" })
$dg = Get-Diag
Report ($dg -like "*目标文件不存在*") "b) 失败写入 run-diag.log" $(if ($dg -like "*目标文件不存在*") { "ok" } else { "日志缺证据" })

# ---------------- 用例 c：exit 1 + 真拷 → 不抛（rc 不参与判定） ----------------
Clear-Diag
$dst3 = Join-Path $WorkDir "c-host.txt"
$r = Invoke-Case "" 1 $true $dst3 $src
Report (-not $r.threw) "c) rc1 但文件已落地 → 不抛异常" $(if ($r.threw) { "误抛：$($r.err)" } else { "ok（rc 只作诊断）" })
Report (Test-Path $dst3) "c) 目标文件确实存在" $(if (Test-Path $dst3) { "ok" } else { "missing" })

# ---------------- 用例 d：有 stdout + exit 0 → Invoke-Vmrun 返回值仍是字符串（不破坏 list/listSnapshots） ----------------
Clear-Diag
$env:MOCK_STDOUT = "snapshot-1"; $env:MOCK_EXIT = "0"; $env:MOCK_COPYTO = ""; $env:MOCK_DEST = ""
$out = Invoke-Vmrun listSnapshots $VmxPath
$isStr = ($out -is [string]) -and ($out -like "*snapshot-1*")
Report $isStr "d) Invoke-Vmrun 返回值仍是 stdout（list 类调用不受影响）" ("got=[" + ($out -join ",") + "] type=" + $(if ($null -ne $out) { $out.GetType().Name } else { "null" }))
Report ($script:VmrunExitCode -eq 0) "d) 同时暴露退出码 = 0" ("rc=$script:VmrunExitCode")

# ---------------- 用例 e：空输出 + exit 0 → 退出码规整为 0（不是 null/空） ----------------
Clear-Diag
$env:MOCK_STDOUT = ""; $env:MOCK_EXIT = "0"; $env:MOCK_COPYTO = ""; $env:MOCK_DEST = ""
$null = Invoke-Vmrun list
$rc = $script:VmrunExitCode
Report ($rc -eq 0) "e) 空输出时退出码仍是数字 0（不再是空值）" ("rc=$rc type=" + $(if ($null -ne $rc) { $rc.GetType().Name } else { "null" }))

# ---------------- 用例 f：Copy-ToGuest 失败只记日志、不 throw ----------------
Clear-Diag
$env:MOCK_STDOUT = ""; $env:MOCK_EXIT = "1"; $env:MOCK_COPYTO = ""; $env:MOCK_DEST = ""
$toThrew = $false
try { Copy-ToGuest $src "C:\vm_e2e\y.txt" } catch { $toThrew = $true }
Report (-not $toThrew) "f) Copy-ToGuest 失败不抛（保持原控制流）" $(if ($toThrew) { "误抛" } else { "ok" })
$dg2 = Get-Diag
Report ($dg2 -like "*Copy-ToGuest 失败*") "f) 失败写入 run-diag.log" $(if ($dg2 -like "*Copy-ToGuest 失败*") { "ok" } else { "日志缺证据" })

# 清理环境变量 + 临时目录
Remove-Item Env:MOCK_STDOUT, Env:MOCK_EXIT, Env:MOCK_COPYTO, Env:MOCK_DEST -ErrorAction SilentlyContinue
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "===== test-copy-fromguest ====="
foreach ($r in $results) { Write-Host $r }
Write-Host "==============================="
if ($allOk) { Write-Host "RESULT: PASS"; exit 0 } else { Write-Host "RESULT: FAIL"; exit 1 }
