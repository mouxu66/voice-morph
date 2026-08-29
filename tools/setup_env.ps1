#Requires -Version 5.1
<#
.SYNOPSIS
    变声工坊 · 一键环境准备

.DESCRIPTION
    在拿到源码的新机器上准备 Python 后端环境：
      1. 创建（或复用）项目 .venv
      2. 按 requirements.txt 安装依赖
      3. 安装与显卡匹配的 GPU 版 PyTorch（默认 cu128；RTX 50 系 Blackwell 需要 cu128+ 的 torch 2.9+）
      4. 可选安装 qwen-tts（文字转语音用）
    装完跑 python tools\doctor.py 复检。

.PARAMETER CudaTag
    PyTorch 的 CUDA 标签，默认 cu128。

.PARAMETER SkipTts
    跳过 qwen-tts 安装。

.EXAMPLE
    .\tools\setup_env.ps1
    .\tools\setup_env.ps1 -SkipTts
#>
[CmdletBinding()]
param(
    [string]$Root,
    [string]$CudaTag = "cu128",
    [switch]$SkipTts
)

$ErrorActionPreference = "Stop"

if (-not $Root) {
    if ($PSScriptRoot) { $Root = Split-Path -Parent $PSScriptRoot }
    elseif ($MyInvocation.MyCommand.Path) { $Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path) }
    else { $Root = (Get-Location).Path }
}
$Root = (Resolve-Path -LiteralPath $Root).Path
if (-not (Test-Path -LiteralPath (Join-Path $Root "m2_server/server.py"))) {
    Write-Host "项目根不正确：$Root（未找到 m2_server/server.py）。请用 -Root 指定。" -ForegroundColor Red
    exit 1
}
Write-Host "项目根：$Root" -ForegroundColor DarkGray

function Invoke-Step([string]$Message, [scriptblock]$Action) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
    & $Action
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne $null) {
        Write-Host "步骤失败（退出码 $LASTEXITCODE）：$Message" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

# ---------- 1. 找一个可用的 Python ----------
$py = $null
foreach ($cand in @((Join-Path $Root ".venv/Scripts/python.exe"), "python", "py")) {
    if ($cand -like "*.exe") {
        if (Test-Path -LiteralPath $cand) { $py = $cand; break }
    }
    else {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if ($cmd) { $py = $cmd.Source; break }
    }
}
if (-not $py) {
    Write-Host "未找到 Python。请先安装 Python 3.11 或 3.12（https://www.python.org/downloads/），并勾选 Add to PATH。" -ForegroundColor Red
    exit 1
}

$venvPy = Join-Path $Root ".venv/Scripts/python.exe"
if (Test-Path -LiteralPath $venvPy) {
    Write-Host "复用已存在的项目虚拟环境：$venvPy" -ForegroundColor Green
}
else {
    Write-Host "==> 创建虚拟环境 .venv" -ForegroundColor Cyan
    & $py -m venv (Join-Path $Root ".venv")
    if ($LASTEXITCODE -ne 0) { Write-Host "创建虚拟环境失败" -ForegroundColor Red; exit $LASTEXITCODE }
    Write-Host "已创建：$venvPy" -ForegroundColor Green
}

& $venvPy -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { Write-Host "升级 pip 失败" -ForegroundColor Red; exit $LASTEXITCODE }

# ---------- 2. GPU 版 PyTorch ----------
Write-Host ""
Write-Host "==> 安装 PyTorch（$CudaTag）" -ForegroundColor Cyan
Write-Host "    这一步会下载 2GB 以上，请耐心等待…" -ForegroundColor DarkGray
& $venvPy -m pip install torch torchaudio --index-url "https://download.pytorch.org/whl/$CudaTag"
if ($LASTEXITCODE -ne 0) {
    Write-Host "PyTorch 安装失败。可改试 -CudaTag cu129（RTX 50 系 Blackwell）：" -ForegroundColor Red
    Write-Host "  .\tools\setup_env.ps1 -CudaTag cu129" -ForegroundColor DarkGray
    exit $LASTEXITCODE
}

# ---------- 3. 其余后端依赖 ----------
Write-Host ""
Write-Host "==> 安装后端依赖" -ForegroundColor Cyan
& $venvPy -m pip install -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { Write-Host "后端依赖安装失败" -ForegroundColor Red; exit $LASTEXITCODE }

# ---------- 4. 可选：qwen-tts ----------
if ($SkipTts) {
    Write-Host ""
    Write-Host "已按 -SkipTts 跳过 qwen-tts，文字转语音功能将不可用。" -ForegroundColor Yellow
}
else {
    Write-Host ""
    Write-Host "==> 安装 qwen-tts（文字转语音，可选）" -ForegroundColor Cyan
    & $venvPy -m pip install qwen-tts
    if ($LASTEXITCODE -ne 0) {
        Write-Host "qwen-tts 安装失败（不影响实时变声，仅影响文字转语音）。" -ForegroundColor Yellow
        Write-Host "可稍后单独安装： & '$venvPy' -m pip install qwen-tts" -ForegroundColor DarkGray
    }
}

# ---------- 5. 复检 ----------
Write-Host ""
Write-Host "==> 环境体检" -ForegroundColor Cyan
& $venvPy (Join-Path $Root "tools/doctor.py")
Write-Host ""
Write-Host "准备完成。启动应用：npm run electron:dev（开发）或安装 web\\release 下的安装包。" -ForegroundColor Green
