# check-update-source.ps1 —— 在 Win10 虚拟机内运行，验证能访问主机更新源。
#
# 用法（VM 内 PowerShell）：
#   powershell -ExecutionPolicy Bypass -File scripts/check-update-source.ps1 -HostIP 192.168.19.1 -Port 9000
#   或读取同目录 vm-test.config.ps1 的 $HostIP / $UpdatePort：
#   powershell -ExecutionPolicy Bypass -File scripts/check-update-source.ps1 -ConfigFile scripts\vm-test.config.ps1
#
# 退出码 0 = 可达且清单合法；非 0 = 不可达或清单缺字段。

param(
  [string]$HostIP = "",
  [int]$Port = 9000,
  [string]$ConfigFile = "$PSScriptRoot\vm-test.config.ps1"
)

if ($HostIP -eq "" -and (Test-Path $ConfigFile)) { . $ConfigFile }
if ($HostIP -eq "") {
  Write-Error "请通过 -HostIP 或 -ConfigFile 指定主机 IP。"
  exit 1
}

$url = "http://${HostIP}:${Port}/latest.json"
Write-Host "GET $url"
try {
  $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 15
} catch {
  Write-Host "FAIL: 无法连接更新源 —— $($_.Exception.Message)"
  exit 1
}
if ($r.StatusCode -ne 200) {
  Write-Host "FAIL: HTTP $($r.StatusCode)"
  exit 1
}
try {
  $m = $r.Content | ConvertFrom-Json
} catch {
  Write-Host "FAIL: 清单不是合法 JSON —— $($_.Exception.Message)"
  exit 1
}
$miss = @("version", "url", "sha256") | Where-Object { -not $m.$_ }
if ($miss.Count) {
  Write-Host "FAIL: 清单缺少字段：$($miss -join ', ')"
  exit 1
}
Write-Host "PASS: 更新源可达，版本 $($m.version)，sha256 已提供"
exit 0
