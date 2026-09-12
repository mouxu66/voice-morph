# 校验 exe/dll 的 Authenticode 签名状态。
# 用法：
#   powershell -File scripts/verify-signature.ps1 -Path "web\release2\变声工坊 Setup 0.2.1.exe"
#
# 已知状态含义：
#   Valid        签名有效且证书链受信任（自签证书需已加入本机信任根）
#   NotSigned    未签名（文件上没有任何签名）
#   NotTrusted   有签名但不被信任（证书不在信任根里）
# 退出码 0 = Valid，其余状态退出码 1，方便脚本化判断。
param(
    [Parameter(Mandatory = $true)][string]$Path
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $Path)) { throw "文件不存在：$Path" }

$sig = Get-AuthenticodeSignature -FilePath $Path
Write-Host "文件   : $Path"
Write-Host "状态   : $($sig.Status)  ($($sig.StatusMessage))"
if ($sig.SignerCertificate) {
    Write-Host "签名者 : $($sig.SignerCertificate.Subject)"
    Write-Host "指纹   : $($sig.SignerCertificate.Thumbprint)"
}
if ($sig.TimeStamperCertificate) {
    Write-Host "时间戳 : $($sig.TimeStamperCertificate.Subject)"
} else {
    Write-Host "时间戳 : 无"
}
exit ($(if ($sig.Status -eq "Valid") { 0 } else { 1 }))