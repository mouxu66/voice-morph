# build-and-publish.ps1 —— 主机端：构建前端 + 自签签名打包 + 生成更新清单 + 打印待上传清单。
#
# 依赖：node/npm（web 目录）、CSC_KEY_PASSWORD 环境变量（自签证书密码，见 certs/）。
# 运行：powershell -ExecutionPolicy Bypass -File scripts/build-and-publish.ps1 `
#         -BaseUrl https://example.com/voicemorph/download -NotesFile docs\whats-new\0.3.0.md
#
# 注意：本脚本只做构建与产出，不自动 git 提交（提交按项目铁律由 AI/人工处理）。

param(
  [string]$BaseUrl = "",        # 更新源对外 base-url（末尾不带 /）；留空则清单 url 用本地文件名
  [string]$NotesFile = "",      # 更新说明 markdown（进 latest.json 的 notes 字段）
  [switch]$Mandatory            # 标记强制更新
)

$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot\..   # 回到仓库根
try {
  # 1) 前端构建
  Write-Host "==> npm run build（vite）"
  Push-Location web
  npm run build

  # 2) 版本号 +1（--no-git-tag-version：不自动提交，提交归项目铁律统一处理）
  Write-Host "==> npm version patch"
  npm version patch --no-git-tag-version
  $ver = (Get-Content package.json | ConvertFrom-Json).version
  Write-Host "    新版本：$ver"

  # 3) 桌面端构建（自签签名；无 CSC_KEY_PASSWORD 时 electron-builder 自动跳过签名）
  if (-not $env:CSC_KEY_PASSWORD) {
    Write-Warning "未设置 CSC_KEY_PASSWORD，安装包将不带签名（SmartScreen 会拦，仅适合测试）。"
  }
  Write-Host "==> npm run electron:build"
  npm run electron:build

  # 4) 生成更新清单 latest.json
  $margs = @("electron/make-update-manifest.cjs", "--dir", "release2")
  if ($BaseUrl)    { $margs += @("--base-url", $BaseUrl) }
  if ($NotesFile)  { $margs += @("--notes", $NotesFile) }
  if ($Mandatory)  { $margs += "--mandatory" }
  Write-Host "==> node $($margs -join ' ')"
  node @margs

  Pop-Location

  # 5) 打印待上传清单
  $exe = Get-ChildItem web\release2 -Filter "*.exe" -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notmatch 'uninstall' } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
  Write-Host ""
  Write-Host "================ 待上传文件 ================"
  if ($exe)   { Write-Host "  web/release2/$($exe.Name)      (v$ver)" }
  Write-Host "  web/release2/latest.json"
  if (Test-Path "web/release2/$($exe.BaseName).blockmap") {
    Write-Host "  web/release2/$($exe.BaseName).blockmap"
  }
  Write-Host "============================================"
  Write-Host "把上面文件上传到 `$BaseUrl 指向的位置，并让 VM_UPDATE_URL 指向 latest.json。"
  if ($BaseUrl) {
    Write-Host "  例：VM_UPDATE_URL = $BaseUrl/latest.json"
  }
} finally {
  Pop-Location
}
