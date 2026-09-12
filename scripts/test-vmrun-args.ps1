# test-vmrun-args.ps1
# Verify vmrun calling both branches keep full space-containing path.
# Branch1 (Wait): & $mock @vmArgs  (no quotes, splatting)
# Branch2 (NoWait): Start-Process -ArgumentList $quoted (manual quotes)
# Run: pwsh -ExecutionPolicy Bypass -File scripts/test-vmrun-args.ps1

$ErrorActionPreference = "Stop"

$tmp = New-Item -ItemType Directory -Force -Path (Join-Path $env:TEMP "vmrun_args_test") | Select-Object -ExpandProperty FullName
$mock = Join-Path $tmp "mockvmrun.cmd"
$recvFile = Join-Path $tmp "received.txt"

# mock vmrun: write all received args (%*) to received.txt. recvFile has no spaces, no quotes needed.
$mockLines = @("@echo off", "echo %* > $recvFile")
Set-Content -Path $mock -Value $mockLines -Encoding ascii

function Format-VmrunArgument([string]$Arg) {
  if ($Arg -match '\s') { return '"' + $Arg + '"' } else { return $Arg }
}

$results = [System.Collections.Generic.List[string]]::new()
function Check([string]$label, [string]$expected) {
  $got = if (Test-Path $recvFile) { (Get-Content $recvFile -Raw).Trim() } else { "" }
  $got = $got -replace '"', ''
  $ok = $got -like "*$expected*"
  $results.Add(("[{0}] {1} -- received: {2}" -f $(if ($ok) { "PASS" } else { "FAIL" }), $label, $got))
  return $ok
}

$VmxPath = "D:\Virtual Machines\Windows 10 x64\Windows 10 x64.vmx"
$vmArgs = @("-gu", "user", "-gp", "pass", "runProgramInGuest", $VmxPath,
            "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", "BASE64==")

# Branch1: Wait branch uses & @vmArgs (no quotes)
Remove-Item $recvFile -Force -ErrorAction SilentlyContinue
& $mock @vmArgs
$ok1 = Check "Wait branch ampersand splat keeps full path" $VmxPath

# Branch2: NoWait branch uses Start-Process -ArgumentList (manual quotes)
Remove-Item $recvFile -Force -ErrorAction SilentlyContinue
$quotedArgs = $vmArgs | ForEach-Object { Format-VmrunArgument $_ }
Start-Process -NoNewWindow -Wait -FilePath $mock -ArgumentList $quotedArgs | Out-Null
$ok2 = Check "NoWait branch Start-Process ArgumentList keeps full path" $VmxPath

Write-Host ""
Write-Host "===== test-vmrun-args ====="
foreach ($r in $results) { Write-Host $r }
Write-Host "==========================="
if ($ok1 -and $ok2) { Write-Host "RESULT: PASS"; exit 0 } else { Write-Host "RESULT: FAIL"; exit 1 }
