#Requires -Version 5.1
<#
.SYNOPSIS
    变声工坊 · 一键环境准备（核心 + 按启用集的插件 extras）

.DESCRIPTION
    本脚本不再"一刀全装"（第 5 步之前是 requirements.txt 一把梭，torch 2GB 必装）：

      1. 创建（或复用）项目 .venv
      2. 装**核心**依赖 —— requirements.txt，只有"不装它应用就起不来"的那些
      3. 按**启用集**装插件 extras —— 读 m2_server/plugins/<id>/plugin.json
         · torch / torchaudio 单独走 CUDA 索引（装成 CPU 版会"显存不可用、推理静默失效"）
         · 关掉的能力不装它的重包（这就是省几个 GB 的地方）
      4. 复检：tools/doctor.py（环境体检）+ tools/plugin_extras.py --check（逐项对账）

    启用集怎么定（优先级从高到低）：
      -Plugins 显式列出 > -Preset 预设 > outputs/plugins.json 里用户关掉的那些

.PARAMETER Root
    项目根（默认取本脚本的上一级目录）。

.PARAMETER Preset
    light（轻量：核心 + 离线变声）/ standard（默认）/ full（全能）/ core（只装核心）。

.PARAMETER Plugins
    高级：显式指定启用的插件 id（如 -Plugins sound.tts,sound.rvc-live），覆盖 -Preset。

.PARAMETER CudaTag
    PyTorch 的 CUDA 标签，默认 cu128（RTX 50 系 Blackwell 需 cu128+ 的 torch 2.9+）。

.PARAMETER SkipTts
    不装 qwen-tts（已有独立 TTS 环境 tts_trial\venv312 时用，省一坨下载）。

.PARAMETER SkipExtras
    只装核心依赖，跳过全部插件 extras（等价于 -Preset core）。

.PARAMETER DryRun
    只打印将要执行的命令，不真装。

.EXAMPLE
    .\tools\setup_env.ps1
    .\tools\setup_env.ps1 -Preset light
    .\tools\setup_env.ps1 -Plugins sound.tts,sound.rvc-live
    .\tools\setup_env.ps1 -Preset full -CudaTag cu129
#>
[CmdletBinding()]
param(
    [string]$Root,
    [ValidateSet("light", "standard", "full", "core")]
    [string]$Preset = "standard",
    [string[]]$Plugins = @(),
    [string]$CudaTag = "cu128",
    [switch]$SkipTts,
    [switch]$SkipExtras,
    [switch]$DryRun
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

$venvPy = Join-Path $Root ".venv/Scripts/python.exe"
$script:hadFailure = $false

function Invoke-Pip {
    <#
      跑一次 pip。$Fatal=$false 时失败只警告（例如 extras 里某个包在新机器上装不上，
      不该让整轮准备中断 —— 最后的 --check 会把缺的列出来）。
    #>
    param([string[]]$Arguments, [string]$What, [bool]$Fatal = $true)
    Write-Host ("    pip " + ($Arguments -join " ")) -ForegroundColor DarkGray
    if ($DryRun) { return }
    & $script:venvPy -m pip @Arguments
    if ($LASTEXITCODE -ne 0) {
        Write-Host ("{0} 失败（退出码 {1}）。可稍后单独重试：" -f $What, $LASTEXITCODE) -ForegroundColor Yellow
        Write-Host ("  & '{0}' -m pip {1}" -f $script:venvPy, ($Arguments -join " ")) -ForegroundColor DarkGray
        if ($Fatal) { exit $LASTEXITCODE }
        $script:hadFailure = $true
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

if (Test-Path -LiteralPath $venvPy) {
    Write-Host "复用已存在的项目虚拟环境：$venvPy" -ForegroundColor Green
}
else {
    Write-Host "==> 创建虚拟环境 .venv" -ForegroundColor Cyan
    if (-not $DryRun) {
        & $py -m venv (Join-Path $Root ".venv")
        if ($LASTEXITCODE -ne 0) { Write-Host "创建虚拟环境失败" -ForegroundColor Red; exit $LASTEXITCODE }
    }
    Write-Host "已创建：$venvPy" -ForegroundColor Green
}

Invoke-Pip -Arguments @("install", "--upgrade", "pip") -What "升级 pip"

# ---------- 2. 核心依赖（不含任何插件 extras） ----------
Write-Host ""
Write-Host "==> 安装核心依赖（requirements.txt）" -ForegroundColor Cyan
Invoke-Pip -Arguments @("install", "-r", (Join-Path $Root "requirements.txt")) -What "核心依赖安装"

# ---------- 3. 按启用集算插件 extras ----------
$plan = $null
if ($SkipExtras) {
    Write-Host ""
    Write-Host "已按 -SkipExtras 跳过全部插件 extras（只装核心依赖）。" -ForegroundColor Yellow
}
else {
    Write-Host ""
    Write-Host "==> 计算启用集与 extras" -ForegroundColor Cyan
    # 用 .venv 的解释器跑：它此时已装好 fastapi/pydantic（plugin_manifest 依赖）
    $extrasArgs = @((Join-Path $Root "tools/plugin_extras.py"), "--json")
    if ($Plugins.Count -gt 0) { $extrasArgs += @("--plugins") + $Plugins }
    else { $extrasArgs += @("--preset", $Preset) }

    if ($DryRun) {
        Write-Host ("    " + $venvPy + " " + ($extrasArgs -join " ")) -ForegroundColor DarkGray
    }
    # 算清单是只读的（读清单 + 读 outputs/plugins.json），-DryRun 下也照跑 ——
    # 否则 DryRun 的启用集是假的，等于没验证。
    $raw = & $venvPy @extrasArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "算 extras 失败。可单独运行排查：" -ForegroundColor Red
        Write-Host "  & '$venvPy' tools\plugin_extras.py" -ForegroundColor DarkGray
        exit $LASTEXITCODE
    }
    $plan = ($raw | Out-String) | ConvertFrom-Json

    Write-Host ("    预设 {0} · 启用 {1} 个能力" -f $plan.preset, $plan.enabled_count) -ForegroundColor Green
    if ($plan.disabled_by_user) {
        Write-Host ("    按你的设置跳过：{0}" -f ($plan.disabled_by_user -join "、")) -ForegroundColor DarkGray
    }
    if ($plan.kept_despite_disabled) {
        Write-Host ("    你关掉了 {0}，但有启用中的能力依赖它 → 仍然装上" -f ($plan.kept_despite_disabled -join "、")) -ForegroundColor DarkGray
    }

    # 过滤掉 $null：JSON 里的空数组经 ConvertFrom-Json 会变成 $null
    $cudaPkgs = @($plan.python_cuda | Where-Object { $_ })
    $pipPkgs = @($plan.python_pip | Where-Object { $_ })
    # -SkipTts 是旧参数，语义 = 从 extras 里去掉 qwen-tts（已有独立 TTS 环境时用）
    if ($SkipTts) {
        $pipPkgs = @($pipPkgs | Where-Object { $_ -ne "qwen-tts" })
        Write-Host "    已按 -SkipTts 去掉 qwen-tts（文字转语音需要独立环境 tts_trial\venv312）" -ForegroundColor Yellow
    }
}

# ---------- 4. 装 extras：torch 走 CUDA 索引，其余走 PyPI ----------
if ($plan) {
    if ($cudaPkgs.Count -gt 0) {
        Write-Host ""
        Write-Host ("==> 安装 PyTorch（{0}，约 2GB，请耐心等待）" -f $CudaTag) -ForegroundColor Cyan
        $cudaArgs = @("install") + $cudaPkgs + @("--index-url", "https://download.pytorch.org/whl/$CudaTag")
        Invoke-Pip -Arguments $cudaArgs -What "PyTorch 安装"
    }
    if ($pipPkgs.Count -gt 0) {
        Write-Host ""
        Write-Host ("==> 安装插件 extras（{0} 个）" -f $pipPkgs.Count) -ForegroundColor Cyan
        # 非致命：extras 里可能有个别包在新机器上装不上，不该中断整轮准备
        Invoke-Pip -Arguments (@("install") + $pipPkgs) -What "插件 extras 安装" -Fatal $false
    }

    if ($plan.external) {
        Write-Host ""
        Write-Host "以下外部依赖 pip 装不了，需要你自己准备：" -ForegroundColor Yellow
        foreach ($item in $plan.external) {
            $size = ""
            if ($item.size_hint_mb) { $size = "（约 $($item.size_hint_mb)MB）" }
            Write-Host ("    [{0}] {1}{2}" -f $item.kind, $item.label, $size) -ForegroundColor DarkGray
        }
    }
    if ($plan.models) {
        Write-Host ""
        Write-Host "模型权重（首次使用会自动下载或需手动放置）：" -ForegroundColor Yellow
        foreach ($item in $plan.models) {
            $size = ""
            if ($item.size_hint_mb) { $size = "（约 $($item.size_hint_mb)MB）" }
            Write-Host ("    {0}{1}" -f $item.label, $size) -ForegroundColor DarkGray
        }
    }
}

# ---------- 5. 复检 ----------
Write-Host ""
Write-Host "==> 环境体检" -ForegroundColor Cyan
if ($DryRun) {
    Write-Host "    （-DryRun：跳过）" -ForegroundColor DarkGray
}
else {
    & $venvPy (Join-Path $Root "tools/doctor.py")
    Write-Host ""
    Write-Host "==> 启用集逐项对账" -ForegroundColor Cyan
    & $venvPy (Join-Path $Root "tools/plugin_extras.py") --check
}

Write-Host ""
if ($DryRun) {
    Write-Host "（-DryRun 完成：以上是要执行的命令，未真装）" -ForegroundColor Yellow
}
elseif ($script:hadFailure) {
    Write-Host "准备完成，但**有包没装上** —— 看上面「逐项对账」里缺的那几个。" -ForegroundColor Yellow
    Write-Host "想装更多能力：改设置页的开关后重跑本脚本，或 -Preset full 一次装全。" -ForegroundColor DarkGray
}
else {
    Write-Host "准备完成。启动应用：npm run electron:dev（开发）或安装 web\release 下的安装包。" -ForegroundColor Green
    Write-Host "想装更多能力：改设置页的开关后重跑本脚本，或 -Preset full 一次装全。" -ForegroundColor DarkGray
}
