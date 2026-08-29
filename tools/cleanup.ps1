#Requires -Version 5.1
<#
.SYNOPSIS
    变声工坊 · 冗余清理

.DESCRIPTION
    项目体检标注的冗余项：废弃的 LoRA 产物、与 web/release 重复的旧打包产物、
    空壳目录、根目录散落的构建日志与 QLoRA 训练数据。

    默认只预演（列出将删除的内容与总体积），加 -Apply 才会真正删除。

.EXAMPLE
    .\tools\cleanup.ps1              # 预演
    .\tools\cleanup.ps1 -Apply       # 真的删
#>
[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$Root
)

$ErrorActionPreference = "Stop"

# 项目根解析：优先脚本自身位置，回退调用信息，最后回退当前目录。
# 必须校验到项目特征文件，否则 -Apply 可能作用到错误目录（例如把 D:\ 当成根）。
if (-not $Root) {
    if ($PSScriptRoot) { $Root = Split-Path -Parent $PSScriptRoot }
    elseif ($MyInvocation.MyCommand.Path) { $Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path) }
    else { $Root = (Get-Location).Path }
}
$Root = (Resolve-Path -LiteralPath $Root).Path
if (-not (Test-Path -LiteralPath (Join-Path $Root "m2_server/server.py"))) {
    Write-Host "解析出的项目根不正确：$Root（未找到 m2_server/server.py）。请用 -Root 显式指定。" -ForegroundColor Red
    exit 1
}
Write-Host "项目根：$Root" -ForegroundColor DarkGray

# 需要整个移除的目录
$Dirs = @(
    @{ Rel = "output_qlora"; Reason = "废弃的 LoRA 训练产物（方案已判定不可用）" },
    @{ Rel = "release";      Reason = "旧打包产物，与 web/release/ 重复" },
    @{ Rel = "tts_trial3";   Reason = "空目录壳" },
    @{ Rel = "m1_workshop/__pycache__"; Reason = "Python 字节码缓存" },
    @{ Rel = "m2_server/__pycache__";   Reason = "Python 字节码缓存" }
)

# 需要移除的单个文件（根目录散落物）
$Files = @(
    @{ Rel = "媒体播放器 2026-08-23 23-20-35.mp4"; Reason = "测试录屏" },
    @{ Rel = "test_tts.wav";        Reason = "测试音频" },
    @{ Rel = "build_log.txt";       Reason = "构建日志" },
    @{ Rel = "builder_log.txt";     Reason = "构建日志" },
    @{ Rel = "web/_dev.log";        Reason = "开发日志" },
    @{ Rel = "web/_dev_upgrade.log"; Reason = "开发日志" }
)

# 按通配符匹配（用于成批的实验日志与 QLoRA 训练数据）
$Globs = @(
    @{ Rel = "output_*.log";  Reason = "实验日志（流水线/QLoRA）" },
    @{ Rel = "train_*.jsonl"; Reason = "QLoRA 训练数据集（方案已废弃）" }
)

function Get-TargetSize([string]$Path) {
    if (Test-Path -LiteralPath $Path -PathType Container) {
        $sum = Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum
        $bytes = 0; if ($sum.Sum) { $bytes = [double]$sum.Sum }
        return @{ Bytes = $bytes; Count = [int]$sum.Count }
    }
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        return @{ Bytes = [double](Get-Item -LiteralPath $Path -Force).Length; Count = 1 }
    }
    return $null
}

function Add-PlanItem([string]$Kind, [string]$Rel, [string]$Reason, [string]$Full, [double]$MB, [int]$Count) {
    $script:plan += [pscustomobject]@{
        Kind = $Kind; Rel = $Rel; Reason = $Reason; Full = $Full; MB = $MB; Items = $Count
    }
}

$plan = @()

foreach ($d in $Dirs) {
    $full = Join-Path $Root $d.Rel
    $s = Get-TargetSize $full
    if ($s) { Add-PlanItem "目录" $d.Rel $d.Reason $full ([math]::Round($s.Bytes / 1MB, 2)) $s.Count }
}
foreach ($f in $Files) {
    $full = Join-Path $Root $f.Rel
    $s = Get-TargetSize $full
    if ($s) { Add-PlanItem "文件" $f.Rel $f.Reason $full ([math]::Round($s.Bytes / 1MB, 2)) 1 }
}
foreach ($g in $Globs) {
    $found = Get-ChildItem -Path (Join-Path $Root $g.Rel) -File -Force -ErrorAction SilentlyContinue
    foreach ($m in $found) {
        $rel = $m.FullName.Substring($Root.Length).TrimStart('\', '/')
        Add-PlanItem "文件" $rel $g.Reason $m.FullName ([math]::Round($m.Length / 1MB, 2)) 1
    }
}

if ($plan.Count -eq 0) {
    Write-Host "没有可清理的内容，工作区很干净。" -ForegroundColor Green
    return
}

if ($Apply) { $headline = "=== 清理执行中 ===" } else { $headline = "=== 预演（未删除任何内容；加 -Apply 才会真删）===" }
Write-Host ""
Write-Host $headline -ForegroundColor Cyan
Write-Host ""

$plan | Sort-Object MB -Descending | ForEach-Object {
    "{0,-4} {1,-42} {2,10} MB  {3,5} 项  {4}" -f $_.Kind, $_.Rel, $_.MB, $_.Items, $_.Reason
} | Out-Host

$totalMB = [math]::Round(($plan | Measure-Object -Property MB -Sum).Sum, 2)
Write-Host ("合计 {0} 项，约 {1} MB（{2} GB）" -f $plan.Count, $totalMB, [math]::Round($totalMB / 1024, 2)) -ForegroundColor Yellow

if (-not $Apply) {
    Write-Host ""
    Write-Host "确认无误后执行： .\tools\cleanup.ps1 -Apply" -ForegroundColor DarkGray
    return
}

Write-Host ""
$failed = 0
foreach ($item in $plan) {
    try {
        $isDir = Test-Path -LiteralPath $item.Full -PathType Container
        if ($isDir) {
            Remove-Item -LiteralPath $item.Full -Recurse -Force -ErrorAction Stop
        }
        else {
            Remove-Item -LiteralPath $item.Full -Force -ErrorAction Stop
        }
        Write-Host ("  已删除  {0}  ({1} MB)" -f $item.Rel, $item.MB) -ForegroundColor DarkGray
    }
    catch {
        $failed++
        Write-Host ("  失败    {0}  {1}" -f $item.Rel, $_.Exception.Message) -ForegroundColor Red
    }
}

Write-Host ""
if ($failed -gt 0) {
    Write-Host "完成，但有 $failed 项删除失败（可能被占用）。" -ForegroundColor Red
}
else {
    Write-Host "清理完成，释放约 $totalMB MB。" -ForegroundColor Green
}
