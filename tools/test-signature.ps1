# 代码签名链路自检（免 ping 真安装包）：
#   同一份临时 exe：签名前 Get-AuthenticodeSignature == NotSigned，
#                   签名后 == Valid。
# 运行：powershell -File tools/test-signature.ps1 （需要 certs/black-seraph.pfx + CSC_KEY_PASSWORD）
# 退出码 0 = 通过；2 = 跳过（证书未生成）；1 = 失败。
param(
    [string]$Password = $env:CSC_KEY_PASSWORD
)

$root = Split-Path $PSScriptRoot -Parent
$pfx = Join-Path $root "certs\black-seraph.pfx"

if (-not (Test-Path $pfx) -or -not $Password) {
    Write-Host "[test-signature] 跳过：先运行 scripts\gen-selfsigned-cert.ps1 生成证书并设置 CSC_KEY_PASSWORD"
    exit 2
}

$signtool = "C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe"
if (-not (Test-Path $signtool)) { $signtool = (Get-Command signtool -ErrorAction SilentlyContinue).Source }
if (-not $signtool) { throw "未找到 signtool.exe（Windows SDK）" }

# 临时真实 PE：electron.exe 是未签名 PE（node_modules 内，devDependency 保证存在）。
# 不用 notepad.exe —— 系统 exe 是目录签名（catalog），复制后 Get-AuthenticodeSignature
# 仍返回 Valid，且 signtool remove 报 CryptSIPRemoveSignedDataMsg 0x57 剥不掉。
$src = Join-Path $root "web\node_modules\electron\dist\electron.exe"
if (-not (Test-Path $src)) { throw "未找到 electron.exe：$src" }
$tmp = Join-Path $env:TEMP "vm-sig-test-$PID.exe"
Copy-Item $src $tmp -Force
try {
    # --- 1. 未签名状态 ---
    $before = Get-AuthenticodeSignature -FilePath $tmp
    if ($before.Status -ne "NotSigned") {
        throw "前提失败：复制出来的 exe 应是 NotSigned，实际 $($before.Status)"
    }
    Write-Host "[test-signature] 未签名状态 NotSigned ✓"

    # --- 2. 签名（本测试不挂时间戳，保持离线可跑；真实打包由 electron-builder 加时间戳）---
    & $signtool sign /f $pfx /p $Password /fd SHA256 $tmp
    if ($LASTEXITCODE -ne 0) { throw "signtool 签名失败（exit $LASTEXITCODE）" }

    # --- 3. 签名后状态 ---
    $after = Get-AuthenticodeSignature -FilePath $tmp
    if ($after.Status -ne "Valid") {
        throw "签名后应为 Valid，实际 $($after.Status)（$($after.StatusMessage)）—— 证书若不在本机信任根会是 NotTrusted"
    }
    Write-Host "[test-signature] 签名后状态 Valid ✓"
    Write-Host "[test-signature] 签名者 $($after.SignerCertificate.Subject)"
    Write-Host "[test-signature] 全部通过"
    exit 0
} finally {
    Remove-Item $tmp -ErrorAction SilentlyContinue
}