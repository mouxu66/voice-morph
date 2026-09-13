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

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\sync_backend.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\sync_backend.ps1 -WhatIfSync
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
Write-Host "完成。本机开发不必重打包：main.cjs 的 resolveProjectRoot() 优先命中 D:\变声 源码根。" -ForegroundColor Green
Write-Host "仅当要重新分发安装包、且改了 web/electron 时才执行：cd web; npm run electron:build" -ForegroundColor Yellow
