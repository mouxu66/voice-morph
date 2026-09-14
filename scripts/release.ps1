# release.ps1 —— 变声工坊发版一条龙：构建 → 升版本 → 签名打包 → 生成更新清单 → 发布 → 打印待上传清单。
#
# 运行（任意工作目录均可，路径以本脚本位置锚定）：
#   powershell -ExecutionPolicy Bypass -File scripts\release.ps1
#   示例（换成你自己的更新源地址）：
#   powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -BaseUrl http://192.168.1.100:9000 -Notes "修复录音卡顿"
#   powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -SkipBump        # 不升版本，只重打包当前版本
#   powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -DryRun          # 只检查前置条件，不构建
#   powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -Publish         # 打完后直接发 GitHub Release（推荐）
#   powershell -ExecutionPolicy Bypass -File scripts\release.ps1 -SkipSign        # 不签名（本地自测；用户装时会看到"未知发布者"）
#
# 与 build-and-publish.ps1 的关系：本脚本是面向发版的正式入口，多了前置检查、
# 版本号一致性校验、清单校验（sha256 回读比对）、GitHub Release 发布、历史产物清理、
# 待上传清单打印与上传提示。build-and-publish.ps1 保留作为底层构建脚本。

param(
  [string]$BaseUrl   = "",      # 更新源对外 base-url（末尾不带 /）。留空则清单 url 用本地文件名
  [string]$Notes     = "",      # 更新说明（短文本）
  [string]$NotesFile = "",      # 更新说明 markdown 文件路径
  [string]$Repo      = "mouxu66/voice-morph",  # -Publish 用的 GitHub 仓库（owner/name）
  [switch]$Mandatory,           # 标记强制更新（前端不给"跳过此版本"）
  [switch]$Publish,             # 发布到 GitHub Release（上传 exe + blockmap + latest.json）
  [switch]$SkipPrune,           # 不清理 release2 里的历史安装包（默认保留最近 2 个版本）
  [switch]$SkipBump,            # 跳过 npm version patch
  [switch]$SkipBuild,           # 跳过前端构建（electron:build 已含 build，通常无需单跑）
  [switch]$SkipSign,            # 不签名（手头没有 pfx 密码时的本地自测；禁止用于正式分发）
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
Step "1/7 前置检查"

if (-not (Test-Path $PkgJson)) { Die "找不到 $PkgJson" }
$pkgBefore = Get-Content $PkgJson -Raw | ConvertFrom-Json
Ok "当前版本：$($pkgBefore.version)"

# 证书密码：签名必需，缺了打出来的包会被 SmartScreen 拦
if ($SkipSign) {
  Warn "已指定 -SkipSign：本次不签名（仅供本地自测，禁止用于正式分发）"
} elseif (-not $env:CSC_KEY_PASSWORD) {
  Warn "未设置 CSC_KEY_PASSWORD —— 安装包将不带签名，Windows 会拦。"
  # 密码绝不写进仓库：这里只给用法，具体值从证书管理处取（2026-09-13 前这里写死过明文，
  # 会随发布日志一起落盘；改后由 tools/check_secrets.py 机器拦截）
  Warn "  设法：`$env:CSC_KEY_PASSWORD = '<你的 pfx 密码>'（自签证书可用 scripts\gen-selfsigned-cert.ps1 重建）"
  Warn "  仅本地测试可忽略；正式发版必须设置（或显式加 -SkipSign 表明是自测）。"
} else {
  Ok "CSC_KEY_PASSWORD 已设置（签名将启用）"
}

# 证书文件
$certPath = Join-Path $RepoRoot "certs\black-seraph.pfx"
if ($SkipSign) {
  Ok "已跳过证书检查（-SkipSign）"
} elseif (-not (Test-Path $certPath)) {
  Warn "找不到自签证书 $certPath，打包可能失败"
} else {
  Ok "证书文件存在"
}

# 产物目录
if (-not (Test-Path $Release2Dir)) { New-Item -ItemType Directory -Force -Path $Release2Dir | Out-Null }
Ok "产物目录：$Release2Dir"

# 更新源地址提醒（-Publish 时会在知道新版本号后自动推导出 GitHub 地址）
if (-not $BaseUrl) {
  if ($Publish) {
    Ok "未给 -BaseUrl：稍后按 GitHub Release 自动推导（https://github.com/$Repo/releases/download/v<新版本>）"
  } else {
    Warn "未给 -BaseUrl，latest.json 的 url 将是本地文件名（需上传前手动补全）"
  }
} else {
  Ok "更新源 base-url：$BaseUrl"
}

# ---------- 发布到 GitHub 的前置条件（仅 -Publish 检查）----------
$GhExe = ""
if ($Publish) {
  $onPath = Get-Command gh -ErrorAction SilentlyContinue
  if ($onPath) {
    $GhExe = $onPath.Source
  } else {
    # 本机 PATH 里没有 gh，只有 WorkBuddy 自带那份 —— 直接用，免得为了发版去装一遍
    $bundled = Join-Path $env:USERPROFILE ".workbuddy-ai\bin\gh\bin\gh.exe"
    if (Test-Path $bundled) { $GhExe = $bundled }
  }
  if (-not $GhExe) {
    Die "找不到 gh（GitHub CLI）。装好 gh 再 -Publish，或去掉 -Publish 改为手动上传。"
  }
  # gh 默认读 %APPDATA%\GitHub CLI，而登录凭据在 ~/.config\gh —— 不指过去就永远报未登录
  # （2026-09-14 实测）。已经设过 GH_CONFIG_DIR 就不动。
  if (-not $env:GH_CONFIG_DIR) {
    $altCfg = Join-Path $env:USERPROFILE ".config\gh"
    if (Test-Path (Join-Path $altCfg "hosts.yml")) {
      $env:GH_CONFIG_DIR = $altCfg
      Warn "未设 GH_CONFIG_DIR，已自动指向 $altCfg"
    }
  }
  # 探测类调用统一临时把 $ErrorActionPreference 放回 Continue：
  # PowerShell 5.1 里原生命令的 stderr 经 2>&1 进管道时，会因 Stop 直接抛 NativeCommandError，
  # 而 gh 在"未登录 / Release 不存在"时正是往 stderr 写 —— 那样就分不清"没登录"和"语法错"。
  $prevEap = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    & $GhExe auth status *> $null
    $ghAuthed = ($LASTEXITCODE -eq 0)
  } finally {
    $ErrorActionPreference = $prevEap
  }
  if (-not $ghAuthed) {
    Die "gh 未登录。先跑：& `"$GhExe`" auth login -h github.com -s repo,workflow"
  }
  Ok "gh 就绪：$GhExe（目标仓库 $Repo）"
}

if ($DryRun) {
  Write-Host "`n[DryRun] 前置检查完成，未执行构建。" -ForegroundColor Yellow
  exit 0
}

# ---------- 2. 前端构建 ----------
if ($SkipBuild) {
  Step "2/7 前端构建（已跳过 -SkipBuild）"
} else {
  Step "2/7 前端构建（vite + tsc）"
  Push-Location $WebDir
  try {
    npm run build
    if ($LASTEXITCODE -ne 0) { Die "前端构建失败（npm run build 退出码 $LASTEXITCODE）" }
  } finally { Pop-Location }
  Ok "前端构建完成"
}

# ---------- 3. 版本号 +1 ----------
Step "3/7 版本号"
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
if ($SkipSign) { Step "4/7 打包（electron-builder --win，-SkipSign 不签名）" }
else           { Step "4/7 签名打包（electron-builder --win）" }
Push-Location $WebDir
try {
  if ($SkipSign) {
    # 用 electron-builder.nosign.cjs 把 win.certificateFile 覆盖成真正的 null。
    # 不能走 CLI：cscInfo 的判定是 `certificateFile != null`，而
    #   -c.win.certificateFile=      → 空串被当成证书路径 → ENOENT: open ''
    #   -c.win.certificateFile=null  → CLI 不做 JSON 解析 → 被当成文件名 "null"
    # 两条都试过，都失败；只有配置文件里给 null 才跳过签名。详见该文件注释。
    npm run build
    if ($LASTEXITCODE -ne 0) { Die "前端构建失败（退出码 $LASTEXITCODE）" }
    npx electron-builder --win --config electron-builder.nosign.cjs
    if ($LASTEXITCODE -ne 0) { Die "electron-builder 打包失败（退出码 $LASTEXITCODE）" }
  } else {
    npm run electron:build
    if ($LASTEXITCODE -ne 0) { Die "electron-builder 打包失败（退出码 $LASTEXITCODE）" }
  }
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
Step "4.5/7 签名校验"
$sig = Get-AuthenticodeSignature $expectedExe
switch ($sig.Status) {
  "Valid"        { Ok "签名有效（$($sig.SignerCertificate.Subject)）" }
  "NotSigned"    { Warn "未签名 —— 用户安装时会看到「未知发布者/Windows 已保护你的电脑」" }
  default        { Warn "签名状态：$($sig.Status)（$($sig.StatusMessage)）" }
}

# ---------- 5. 生成更新清单 ----------
Step "5/7 生成 latest.json"

# -Publish 且没给 -BaseUrl：按 GitHub Release 的 asset 直链推导。
# 形如 https://github.com/<repo>/releases/download/v0.2.3/<文件名>，
# 与下面 gh release create 打出的 tag 必须一致（都用 v$NewVersion）。
if ($Publish -and -not $BaseUrl) {
  $BaseUrl = "https://github.com/$Repo/releases/download/v$NewVersion"
  Ok "按 GitHub Release 推导 base-url：$BaseUrl"
}

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

# ---------- 6. 发布到 GitHub Release ----------
$blockmap = "$expectedExe.blockmap"
if ($Publish) {
  Step "6/7 发布到 GitHub Release"
  $tag = "v$NewVersion"

  # tag 已存在就停：gh 会失败，而这通常意味着"重复发版"这个真问题，不该静默跳过
  $prevEap = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    & $GhExe release view $tag --repo $Repo *> $null
    $tagExists = ($LASTEXITCODE -eq 0)
  } finally {
    $ErrorActionPreference = $prevEap
  }
  if ($tagExists) {
    Die "Release $tag 已存在。本版本重发请先删掉该 Release，或去掉 -Publish 手动上传。"
  }

  $assetArgs = @($expectedExe, $ManifestPath)
  if (Test-Path $blockmap) { $assetArgs += $blockmap }

  # --latest：显式标记为"最新版"。客户端读的是 /releases/latest/... 别名，而 GitHub 对
  # "latest" 的判定并非严格比版本号 —— 显式标一次，别名指向才不依赖它的默认规则。
  $relArgs = @("release", "create", $tag, "--repo", $Repo, "--title", "变声工坊 $NewVersion", "--latest")
  if ($NotesFile -and (Test-Path $NotesFile)) {
    $relArgs += @("--notes-file", $NotesFile)
  } elseif ($Notes) {
    $relArgs += @("--notes", $Notes)
  } else {
    $relArgs += @("--notes", "变声工坊 $NewVersion")
  }
  $relArgs += $assetArgs

  Write-Host "    $GhExe $($relArgs -join ' ')"
  & $GhExe @relArgs
  if ($LASTEXITCODE -ne 0) { Die "gh release create 失败（退出码 $LASTEXITCODE）" }
  Ok "已发布：https://github.com/$Repo/releases/tag/$tag"

  # 客户端读的是「最新版永久别名」而不是 tag 固定地址，所以必须实测它真能拿到清单 ——
  # 打不通就等于更新链断了，这里发现比用户报错早得多。
  $probe = "https://github.com/$Repo/releases/latest/download/latest.json"
  try {
    $resp = Invoke-WebRequest -Uri $probe -UseBasicParsing -TimeoutSec 20
    $remote = $resp.Content | ConvertFrom-Json
    if ($remote.version -ne $NewVersion) {
      Warn "别名返回版本 $($remote.version)，期望 $NewVersion（CDN 缓存未生效？过几分钟再看）"
    } else {
      Ok "客户端别名可访问且版本正确：$probe"
    }
  } catch {
    Warn "别名探测失败：$($_.Exception.Message)"
    Warn "  刚发布时 GitHub 可能还没生效，等几十秒重试：$probe"
  }
} else {
  Step "6/7 发布到 GitHub Release（已跳过，未加 -Publish）"
}

# ---------- 7. 清理历史产物 ----------
Step "7/7 清理历史产物"
$allExe = @(Get-ChildItem $Release2Dir -Filter "VoiceMorph-Setup-*.exe" -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending)
$keep = @($allExe | Select-Object -First 2)
$drop = @($allExe | Select-Object -Skip 2)
if ($SkipPrune) {
  Warn "已跳过清理（-SkipPrune）：目录内有 $($allExe.Count) 个安装包"
} elseif ($drop.Count -eq 0) {
  Ok "无需清理（共 $($allExe.Count) 个安装包，保留最近 2 个）"
} else {
  $freedMb = 0.0
  foreach ($f in $drop) {
    $freedMb += $f.Length / 1MB
    # 先截断到 0 字节再删：本机的"安全删除"会把文件挪进回收站、空间不会立刻归还，
    # 截断过再删即使进回收站也只是空壳（约定见 tools/hard_delete.py）。
    try {
      $fs = [System.IO.File]::Open($f.FullName, 'Open', 'Write')
      $fs.SetLength(0)
      $fs.Close()
    } catch { Warn "截断失败：$($f.Name)" }
    $bm = "$($f.FullName).blockmap"
    Remove-Item $f.FullName -Force -ErrorAction SilentlyContinue
    if (Test-Path $bm) { Remove-Item $bm -Force -ErrorAction SilentlyContinue }
    Ok "已删除 $($f.Name)"
  }
  $keptNames = ($keep | ForEach-Object { $_.Name }) -join ", "
  Ok ("释放约 {0:N0} MB；保留：{1}" -f $freedMb, $keptNames)
}

# ---------- 待上传清单 ----------
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
Write-Host ("总计：{0:N1} MB" -f (($files | ForEach-Object { (Get-Item $_).Length } | Measure-Object -Sum).Sum / 1MB))

if ($Publish) {
  Write-Host "`n本次已由 -Publish 直接发到 GitHub Release，无需手动上传。" -ForegroundColor Green
  Write-Host "  客户端默认更新源（永久别名）：" -ForegroundColor Yellow
  Write-Host "  https://github.com/$Repo/releases/latest/download/latest.json"
} else {
  Write-Host "`n上传目标：更新源根目录（-BaseUrl 指向的位置）" -ForegroundColor Yellow
  if ($BaseUrl) {
    Write-Host "  用户端 VM_UPDATE_URL 应指向：$BaseUrl/latest.json"
  } else {
    Write-Host "  未指定 -BaseUrl：清单里 url 是本地文件名，上传后需手动改成可访问地址，"
    Write-Host "  并把用户端 VM_UPDATE_URL 指向该地址下的 latest.json。"
  }
  Write-Host "  本地局域网测试可直接：python -m http.server 9000 --directory `"$Release2Dir`""
  Write-Host "  发到 GitHub 更省事：重跑加 -Publish 即可（自动建 Release 并上传 asset）"
}

Write-Host "`n[完成] 版本 $($pkgBefore.version) → $NewVersion" -ForegroundColor Green
Write-Host "下一步：客户端启动后 12 秒静默检查 → 或「设置 → 应用 → 检查更新」验证。详见 docs/release-sop.md" -ForegroundColor Green
