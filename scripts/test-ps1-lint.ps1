# test-ps1-lint.ps1
# 静态体检 scripts/*.ps1，专治「编辑时被吃掉的换行 / 参数绑定错误」这类低级但致命的坑。
# 检查项：
#   1. 语法解析错误（AST Parser）
#   2. 合并行：`| Out-Null` 之后同一行还跟着另一条语句（Edit 吞换行的典型症状）
#   3. 脚本内函数的必填参数（Mandatory）是否在所有调用点被提供
#   4. UTF-8 BOM 是否在位（PowerShell 5.1 中文坑）
# 运行：powershell -ExecutionPolicy Bypass -File scripts/test-ps1-lint.ps1

$ErrorActionPreference = "Stop"
$scriptDir = $PSScriptRoot

$targets = @(
  "test-update-e2e.ps1",
  "test-vmrun-args.ps1",
  "test-install-probe.ps1",
  "test-ps1-lint.ps1",
  "release.ps1",
  "check-update-source.ps1",
  "build-and-publish.ps1",
  "vm-test.config.example.ps1"
)

$results = [System.Collections.Generic.List[string]]::new()
$allOk = $true
function Report([bool]$ok, [string]$label, [string]$detail) {
  if (-not $ok) { $script:allOk = $false }
  $results.Add(("[{0}] {1} -- {2}" -f $(if ($ok) { "PASS" } else { "FAIL" }), $label, $detail))
}

# --- 0. 自检：合并行正则的正反例（防止规则本身退化） ---
# 注意：下列样例本身是「会被本规则命中的文本」，扫描自身时需白名单（见下 LINT-SELF 标记）。
$selfCases = @(
  # LINT-SELF: sample
  @{ ln = 'Invoke-GuestCommand -Script $p -NoWait | Out-Null  $tmp = Join-Path $a $b'; merged = $true;  why = "真合并行" },
  @{ ln = 'if (-not (Test-Path $x)) { New-Item -ItemType Directory -Force -Path $x | Out-Null }'; merged = $false; why = "if 块内管道，合法" },
  @{ ln = 'try { Invoke-GuestCommand -Script $probe -TimeoutSec 15 | Out-Null } catch { }'; merged = $false; why = "单行 try/catch，合法" },
  @{ ln = 'Start-Process -FilePath $s -ArgumentList "/S" | Out-Null'; merged = $false; why = "行尾管道，合法" },
  @{ ln = 'New-Item -ItemType Directory -Force -Path $d | Out-Null'; merged = $false; why = "行尾管道，合法" },
  # LINT-SELF: sample
  @{ ln = 'Invoke-Vmrun -gu $u copyFileFromHostToGuest $a $b $c | Out-Null'; merged = $false; why = "行尾管道，合法" }
)
function Test-MergedLine([string]$ln) {
  if ($ln -notmatch '\|\s*Out-Null\s+[^\}\|\)\;]') { return $false }
  if ($ln -match '\|\s*Out-Null\s*\|\s*Out-Null') { return $false }
  return $true
}
$regexIssues = @()
foreach ($c in $selfCases) {
  $got = Test-MergedLine $c.ln
  if ($got -ne $c.merged) { $regexIssues += ("want=" + $c.merged + " got=" + $got + " [" + $c.why + "]") }
}
Report ($regexIssues.Count -eq 0) "merged-line regex self-check" $(if ($regexIssues.Count) { $regexIssues -join "; " } else { ("cases=" + $selfCases.Count) })

foreach ($t in $targets) {
  $p = Join-Path $scriptDir $t
  if (-not (Test-Path $p)) { Report $false "$t exists" "NOT FOUND"; continue }

  # --- 4. BOM ---
  $bytes = [System.IO.File]::ReadAllBytes($p)
  $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
  Report $hasBom "$t UTF-8 BOM" $(if ($hasBom) { "efbbbf" } else { "NO BOM" })

  # --- 1. 语法 ---
  $errs = $null; $tokens = $null
  $ast = [System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$tokens, [ref]$errs)
  if ($errs -and $errs.Count -gt 0) {
    foreach ($e in $errs) { Report $false "$t syntax" ("L" + $e.Extent.StartLineNumber + ": " + $e.Message) }
    continue
  }
  Report $true "$t syntax" ("tokens=" + $tokens.Count)

  # --- 2. 合并行 ---
  # 症状：`| Out-Null` 之后同一行还跟着另一条**独立语句**（Edit 吞掉换行的典型后果）。
  # 白名单：`try { ... | Out-Null } catch { }` 这类单行 try 块是合法写法，不算合并行。
  # 行级白名单：上一行含 `# LINT-SELF: sample` 时整行跳过（本 lint 自身的正反例样例）。
  $lines = [System.IO.File]::ReadAllLines($p)
  $merged = @()
  for ($i = 0; $i -lt $lines.Count; $i++) {
    $ln = $lines[$i]
    if ($i -gt 0 -and $lines[$i - 1] -match '#\s*LINT-SELF:\s*sample') { continue }
    if (-not (Test-MergedLine $ln)) { continue }
    $merged += ("L" + ($i + 1))
  }
  Report ($merged.Count -eq 0) "$t no merged lines after Out-Null" $(if ($merged.Count) { $merged -join "," } else { "clean" })

  # --- 3. 必填参数检查 ---
  $funcs = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
  $sigs = @{}
  foreach ($f in $funcs) {
    $pars = @()
    if ($f.Body.ParamBlock) {
      foreach ($par in $f.Body.ParamBlock.Parameters) {
        $isMand = $false
        foreach ($a in $par.Attributes) {
          if ($a -is [System.Management.Automation.Language.AttributeAst]) {
            if ($a.TypeName.Name -eq 'Parameter') {
              foreach ($na in $a.NamedArguments) {
                if ($na.ArgumentName -eq 'Mandatory') { $isMand = $true }
              }
            }
          }
        }
        $pars += [pscustomobject]@{ Name = $par.Name.VariablePath.UserPath; Mand = $isMand }
      }
    }
    $sigs[$f.Name] = $pars
  }
  $cmds = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)
  $issues = @()
  foreach ($c in $cmds) {
    $name = $c.GetCommandName()
    if (-not $name -or -not $sigs.ContainsKey($name)) { continue }
    $boundNamed = @()
    $positional = 0
    for ($k = 1; $k -lt $c.CommandElements.Count; $k++) {
      $el = $c.CommandElements[$k]
      if ($el -is [System.Management.Automation.Language.CommandParameterAst]) { $boundNamed += $el.ParameterName }
      else { $positional++ }
    }
    foreach ($par in $sigs[$name]) {
      if (-not $par.Mand) { continue }
      if (($boundNamed -notcontains $par.Name) -and ($positional -eq 0)) {
        $issues += ("L" + $c.Extent.StartLineNumber + " " + $name + " missing -" + $par.Name)
      }
    }
  }
  Report ($issues.Count -eq 0) "$t mandatory args" $(if ($issues.Count) { $issues -join "; " } else { "ok" })
}

Write-Host ""
Write-Host "===== test-ps1-lint ====="
foreach ($r in $results) { Write-Host $r }
Write-Host "========================="
if ($allOk) { Write-Host "RESULT: PASS"; exit 0 } else { Write-Host "RESULT: FAIL"; exit 1 }
