# release.ps1 —— 变声工坊发版一条龙：构建 → 升版本 → 签名打包 → 生成更新清单 → 打印待上传清单。
#
# 运行（任意工作目录均可，路径以本脚本位置锚定）：
#   powershell -ExecutionPolicy Bypass -File scripts\release.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -BaseUrl http://192.168.145.1:9000 -Notes "修复录音卡顿"
#   powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -SkipBump        # 不升版本，只重打包当前版本
#   powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -DryRun          # 只检查前置条件，不构建
#
# 与 build-and-publish.ps1 的关系：本脚本是面向发版的正式入口，多了前置检查、
# 版本号一致性校验、清单校验（sha256 回读比对）、待上传清单打印与上传提示。
# build-and-publish.ps1 保留作为底层构建脚本。

param(
  [string]$BaseUrl   = "",      # 更新源对外 base-url（末尾不带 /）。留空则清单 url 用本地文件名
  [string]$Notes     = "",      # 更新说明（短文本）
  [string]$NotesFile = "",      # 更新说明 markdown 文件路径
  [switch]$Mandatory,           # 标记强制更新（前端不给"跳过此版本"）
  [switch]$SkipBump,            # 跳过 npm version patch
  [switch]$SkipBuild,           # 跳过前端构建（electron:build 已含 build，通常无需单跑）
  [switch]$DryRun               # 只做前置检查，不实际构建
)

$ErrorActionPreference = "Stop"

# ---------- 路径锚定（不依赖当前工作目录）----------
$RepoRoot    = Split-Path -Parent $PSScriptRoot
$WebDir      = Join-Path $RepoRoot "web"
$Release2Dir = Join-Path $WebDir "release2"
$PkgJson     = Join-Path $WebDir "package.json"

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    [OK] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    [!] $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "`n[FAIL] $msg" -ForegroundColor Red; exit 1 }

Write-Host "================ 变声工坊发版 ================"
Write-Host "仓库根：$RepoRoot"

# ---------- 1. 前置检查 ----------
Step "1/6 前置检查"

if (-not (Test-Path $PkgJson)) { Die "找不到 $PkgJson" }
$pkgBefore = Get-Content $PkgJson -Raw | ConvertFrom-Json
Ok "当前版本：$($pkgBefore.version)"

# 证书密码：签名必需，缺了打出来的包会被 SmartScreen 拦
if (-not $env:CSC_KEY_PASSWORD) {
  Warn "未设置 CSC_KEY_PASSWORD —— 安装包将不带签名，Windows 会拦。"
  Warn "  设法：`$env:CSC_KEY_PASSWORD = '<你的 pfx 密码>'（或从证书管理处取）"
  Warn "  仅本地测试可忽略；正式发版必须设置。"
} else {
  Ok "CSC_KEY_PASSWORD 已设置（签名将启用）"
}

# 证书文件
$certPath = Join-Path $RepoRoot "certs\black-seraph.pfx"
if (-not (Test-Path $certPath)) { Warn "找不到自签证书 $certPath，打包可能失败" } else { Ok "证书文件存在" }

# 产物目录
if (-not (Test-Path $Release2Dir)) { New-Item -ItemType Directory -Force -Path $Release2Dir | Out-Null }
Ok "产物目录：$Release2Dir"

# 更新源地址提醒
if (-not $BaseUrl) {
  Warn "未给 -BaseUrl，latest.json 的 url 将是本地文件名（需上传前手动补全）"
} else {
  Ok "更新源 base-url：$BaseUrl"
}

if ($DryRun) {
  Write-Host "`n[DryRun] 前置检查完成，未执行构建。" -ForegroundColor Yellow
  exit 0
}

# ---------- 2. 前端构建 ----------
if ($SkipBuild) {
  Step "2/6 前端构建（已跳过 -SkipBuild）"
} else {
  Step "2/6 前端构建（vite + tsc）"
  Push-Location $WebDir
  try {
    npm run build
    if ($LASTEXITCODE -ne 0) { Die "前端构建失败（npm run build 退出码 $LASTEXITCODE）" }
  } finally { Pop-Location }
  Ok "前端构建完成"
}

# ---------- 3. 版本号 +1 ----------
Step "3/6 版本号"
if ($SkipBump) {
  Warn "已跳过升版本（-SkipBump），保持 $($pkgBefore.version)"
} else {
  Push-Location $WebDir
  try {
    npm version patch --no-git-tag-version
    if ($LASTEXITCODE -ne 0) { Die "npm version patch 失败" }
  } finally { Pop-Location }
}
$pkgAfter = Get-Content $PkgJson -Raw | ConvertFrom-Json
$NewVersion = $pkgAfter.version
Ok "本次发版版本：$NewVersion"

# ---------- 4. 签名打包 ----------
Step "4/6 签名打包（electron-builder --win）"
Push-Location $WebDir
try {
  npm run electron:build
  if ($LASTEXITCODE -ne 0) { Die "electron-builder 打包失败（退出码 $LASTEXITCODE）" }
} finally { Pop-Location }
Ok "打包完成"

# 校验：产物文件是否与版本号一致
$expectedExe = Join-Path $Release2Dir "VoiceMorph-Setup-$NewVersion.exe"
if (-not (Test-Path $expectedExe)) {
  Warn "未找到预期产物 VoiceMorph-Setup-$NewVersion.exe"
  Warn "  目录内现有 exe：$((Get-ChildItem $Release2Dir -Filter *.exe | Select-Object -ExpandProperty Name) -join ', ')"
  Die "产物版本与 package.json 不一致，请检查 electron-builder 是否成功"
}
Ok "产物：$expectedExe"

# 签名校验
Step "4.5/6 签名校验"
$sig = Get-AuthenticodeSignature $expectedExe
switch ($sig.Status) {
  "Valid"        { Ok "签名有效（$($sig.SignerCertificate.Subject)）" }
  "NotSigned"    { Warn "未签名 —— 用户安装时会看到「未知发布者/Windows 已保护你的电脑」" }
  default        { Warn "签名状态：$($sig.Status)（$($sig.StatusMessage)）" }
}

# ---------- 5. 生成更新清单 ----------
Step "5/6 生成 latest.json"
Push-Location $WebDir
try {
  $margs = @("electron/make-update-manifest.cjs", "--dir", "release2", "--version", $NewVersion)
  if ($BaseUrl)   { $margs += @("--base-url", $BaseUrl) }
  if ($NotesFile) { $margs += @("--notes", $NotesFile) }
  elseif ($Notes) { $margs += @("--notes-text", $Notes) }
  if ($Mandatory) { $margs += "--mandatory" }
  Write-Host "    node $($margs -join ' ')"
  node @margs
  if ($LASTEXITCODE -ne 0) { Die "生成更新清单失败" }
} finally { Pop-Location }

$ManifestPath = Join-Path $Release2Dir "latest.json"
if (-not (Test-Path $ManifestPath)) { Die "未生成 $ManifestPath" }
$manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
Ok "清单版本：$($manifest.version)"

# 清单自校验：sha256 必须与磁盘文件实际值一致（防止清单与包不匹配）
$actualSha = (Get-FileHash $expectedExe -Algorithm SHA256).Hash.ToLower()
if ($actualSha -ne $manifest.sha256) {
  Die "清单 sha256 与实际文件不符！`n  清单：$($manifest.sha256)`n  实际：$actualSha`n（清单可能指向了错误的安装包）"
}
Ok "sha256 校验通过：$($manifest.sha256.Substring(0,16))…"

if ($manifest.version -ne $NewVersion) {
  Die "清单版本 $($manifest.version) 与 package.json 的 $NewVersion 不一致"
}
Ok "版本号一致性校验通过"

# ---------- 6. 待上传清单 ----------
Step "6/6 待上传文件清单"
$blockmap = "$expectedExe.blockmap"
$files = @($expectedExe, $ManifestPath)
if (Test-Path $blockmap) { $files += $blockmap }

Write-Host ""
Write-Host "================ 待上传文件 ================" -ForegroundColor Cyan
foreach ($f in $files) {
  $size = "{0:N1} MB" -f ((Get-Item $f).Length / 1MB)
  $rel = $f.Replace("$RepoRoot\", "")
  Write-Host ("  {0,-42} {1,10}" -f $rel, $size)
}
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "总计：{0:N1} MB" -f (($files | ForEach-Object { (Get-Item $_).Length } | Measure-Object -Sum).Sum / 1MB)

Write-Host "`n上传目标：更新源根目录（-BaseUrl 指向的位置）" -ForegroundColor Yellow
if ($BaseUrl) {
  Write-Host "  用户端 VM_UPDATE_URL 应指向：$BaseUrl/latest.json"
} else {
  Write-Host "  未指定 -BaseUrl：清单里 url 是本地文件名，上传后需手动改成可访问地址，"
  Write-Host "  并把用户端 VM_UPDATE_URL 指向该地址下的 latest.json。"
}
Write-Host "  本地局域网测试可直接：python -m http.server 9000 --directory `"$Release2Dir`""

Write-Host "`n[完成] 版本 $($pkgBefore.version) → $NewVersion" -ForegroundColor Green
Write-Host "下一步：上传上述文件 → 客户端检查更新 → 验证。详见 docs/release-sop.md" -ForegroundColor Green
