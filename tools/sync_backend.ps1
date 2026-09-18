#requires -Version 5.1
<#
.SYNOPSIS
    把项目源码同步进打包安装目录（voice-morph-desktop/resources/backend）。

.DESCRIPTION
    安装版运行时后端代码位于 resources/backend/m2_server（package.json extraResources）。
    改完 m2_server/*.py 或 tools/doctor.py 后跑一次，安装包里的代码才会跟着更新。

    注意：模型权重（tts_models/voicebank/media）不进包 —— 本机启动时
    resolveProjectRoot 会优先命中 D:\变声（全量代码+模型），分发到别的机器才回退
    resources/backend，此时需要用户自备模型并用「运行环境体检」自查。

    【2026-09-05 起】本脚本仅剩手动应急用途：后端每次启动（= 每次打开桌面端）
    会通过 m2_server/backend_autosync.py 自动镜像同步 m2_server/tools/web_dist
    到 resources/backend（VM_BACKEND_AUTOSYNC=0 可关），语义与本脚本一致
    （MD5 比对 + 镜像清理多余文件）。

    ⚠️【2026-09-18 重要澄清：默认目标不是「已安装的那份」】
    本脚本与 backend_autosync.py 的默认目标都是**源码根下的 staging 目录**
    `<项目根>\voice-morph-desktop\resources\backend`（打包前暂存用）。
    而**已安装**的桌面端读的是另一个地方：

        %LOCALAPPDATA%\Programs\voice-morph-desktop\resources\backend

    （即 `C:\Users\<你>\AppData\Local\Programs\voice-morph-desktop\resources\backend`）

    两者互不相干，且 autosync **不会**写后者 —— 所以装好的桌面端会**悄悄过期**。
    2026-09-18 实测的后果：`resources/backend/m2_server` 里 `rvc_live.py` 已经是新版
    （会调 `qwen3_tts.worker_alive()`），但 `qwen3_tts.py` 还是旧版（没有这个函数）
    → 运行期 `AttributeError`，实时变声的状态/启动路径直接崩。
    **半新半旧的混装比全旧更危险。**

    要给已安装的桌面端更新，必须显式指定目标：

        powershell -NoProfile -ExecutionPolicy Bypass -File tools\sync_backend.ps1 `
          -TargetRoot "$env:LOCALAPPDATA\Programs\voice-morph-desktop\resources\backend"

    先看会动哪些文件（不实际拷）：

        ... -WhatIfSync -TargetRoot "<同上>"

    注意 `web/electron/pet/*`（桌宠渲染侧）**不在**本脚本与 autosync 的镜像范围内，
    改完要手动拷一份过去（见 AGENTS.md「桌面端改动如何生效」）。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\sync_backend.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\sync_backend.ps1 -WhatIfSync
    # 给「已安装」的桌面端更新（注意目标目录不同，见上）：
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\sync_backend.ps1 `
      -TargetRoot "$env:LOCALAPPDATA\Programs\voice-morph-desktop\resources\backend"
#>
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TargetRoot  = (Join-Path $ProjectRoot "voice-morph-desktop\resources\backend"),
    [switch]$WhatIfSync
)

$ErrorActionPreference = "Stop"

function Sync-Dir {
    param([string]$Src, [string]$Dst, [string[]]$Exclude)

    if (-not (Test-Path $Src)) {
        Write-Host "[skip] 源目录不存在: $Src" -ForegroundColor Yellow
        return 0
    }
    if (-not (Test-Path $Dst)) { New-Item -ItemType Directory -Path $Dst -Force | Out-Null }

    $copied = 0
    Get-ChildItem -Path $Src -File -Recurse | ForEach-Object {
        $rel = $_.FullName.Substring($Src.Length).TrimStart('\')
        # 排除规则：pycache / 日志 / 备份 / 临时
        if ($rel -match '(__pycache__|\.pyc$|\.pyo$|\.log$|\.bak$|\.tmp$)') { return }
        $dest = Join-Path $Dst $rel
        $destDir = Split-Path -Parent $dest
        if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null }
        $need = $true
        if (Test-Path $dest) {
            $srcHash = (Get-FileHash $_.FullName -Algorithm MD5).Hash
            $dstHash = (Get-FileHash $dest -Algorithm MD5).Hash
            $need = $srcHash -ne $dstHash
        }
        if ($need) {
            if ($WhatIfSync) { Write-Host "[would copy] $rel" -ForegroundColor Cyan }
            else { Copy-Item $_.FullName -Destination $dest -Force }
            $copied++
        }
    }
    return $copied
}

Write-Host "=== 后端代码同步 ===" -ForegroundColor Green
Write-Host "源: $ProjectRoot"
Write-Host "目标: $TargetRoot"
Write-Host ""

$n1 = Sync-Dir -Src (Join-Path $ProjectRoot "m2_server") -Dst (Join-Path $TargetRoot "m2_server")
$n2 = Sync-Dir -Src (Join-Path $ProjectRoot "tools")      -Dst (Join-Path $TargetRoot "tools")

Write-Host ""
Write-Host "m2_server: $n1 个文件更新" -ForegroundColor Green
Write-Host "tools:     $n2 个文件更新" -ForegroundColor Green

# 目标目录归属提示：默认目标是**源码根下的 staging 目录**，不是已安装的桌面端。
# 混装（一半新一半旧）比全旧更危险 —— 2026-09-18 实测过一次 AttributeError，见文件头。
$installRoot = Join-Path $env:LOCALAPPDATA "Programs\voice-morph-desktop\resources\backend"
if ((Resolve-Path -LiteralPath $TargetRoot -ErrorAction SilentlyContinue).Path -ne `
    (Resolve-Path -LiteralPath $installRoot  -ErrorAction SilentlyContinue).Path) {
    Write-Host ""
    Write-Host "⚠ 本次目标是 staging 目录，**已安装的桌面端没有更新**。" -ForegroundColor Yellow
    if (Test-Path $installRoot) {
        Write-Host "  已安装的副本在这里，如需一并更新请加 -TargetRoot：$installRoot" -ForegroundColor Yellow
    } else {
        Write-Host "  （本机没检测到已安装的桌面端：$installRoot）" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "说明：只有**源码模式**（npm run electron，app.isPackaged=false）才会优先命中 D:\变声 源码根；" -ForegroundColor Cyan
Write-Host "      安装版（变声工坊.exe）只读包内 resources/backend，绝不回退源码根。" -ForegroundColor Cyan
Write-Host "仅当要重新分发安装包、且改了 web/electron 主进程时才执行：cd web; npm run electron:build" -ForegroundColor Yellow
