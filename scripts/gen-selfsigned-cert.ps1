# 生成自签名代码签名证书（开发/内测用，正式发布请换 OV/EV 正式证书）。
# 产物：<根目录>/certs/black-seraph.pfx（含私钥，.gitignore 已排除 certs/，绝不入库）
#
# 用法：
#   powershell -File scripts/gen-selfsigned-cert.ps1 -Password "你的密码"
#   （或先 $env:CSC_KEY_PASSWORD="你的密码" 再运行，与 electron-builder 的
#     ${env.CSC_KEY_PASSWORD} 保持一致）
#
# 说明：
#   - 证书自签且被加入本机 CurrentUser\Root，本机 Get-AuthenticodeSignature 才能给出
#     Status=Valid（不在信任根里时是 NotTrusted）
#   - 私钥标记 Exportable，否则 Export-PfxCertificate 报错
param(
    [string]$Password = $env:CSC_KEY_PASSWORD,
    [string]$OutDir = (Join-Path $PSScriptRoot "..\certs"),
    [string]$Subject = "CN=VoiceMorph Dev Signing, O=VoiceMorph, E=admin@black-seraph.com"
)

$ErrorActionPreference = "Stop"
if (-not $Password) {
    throw "未提供密码：请 -Password xxx 或先设置环境变量 CSC_KEY_PASSWORD"
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$cert = New-SelfSignedCertificate `
    -Subject $Subject `
    -Type CodeSigningCert `
    -CertStoreLocation "Cert:\CurrentUser\My" `
    -KeyExportPolicy Exportable `
    -KeyAlgorithm RSA -KeyLength 2048 `
    -NotAfter (Get-Date).AddYears(3)

# 加入本机信任根：自签名证书不在 Root 里时签名校验会返回 NotTrusted。
# 注：这只是让本机认这个开发证书；其他机器要认仍需把它装进它们的信任根。
$store = New-Object System.Security.Cryptography.X509Certificates.X509Store("Root", "CurrentUser")
$store.Open("ReadWrite")
$store.Add($cert)
$store.Close()

$pfx = Join-Path $OutDir "black-seraph.pfx"
Export-PfxCertificate -Cert $cert -FilePath $pfx -Password (ConvertTo-SecureString $Password -AsPlainText -Force) | Out-Null

Write-Host "证书已生成：$pfx" -ForegroundColor Green
Write-Host "  主题：$Subject"
Write-Host "  指纹：$($cert.Thumbprint)"
Write-Host "  有效期至：$($cert.NotAfter)"
Write-Host "本机信任根：已加入 CurrentUser\Root（可用 scripts\verify-signature.ps1 验证）"