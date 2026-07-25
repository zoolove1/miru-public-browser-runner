param(
    [int]$Fps = 8,
    [int]$Monitor = 1,
    [string]$TargetTitle = ""
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Broker = Join-Path $Here "miru-frame-broker.exe"
if (-not (Test-Path $Broker)) {
    throw "miru-frame-broker.exe was not found beside this script. Extract the complete artifact ZIP first."
}

$TempRoot = Join-Path $env:TEMP "miru-frame-broker-v010"
New-Item -ItemType Directory -Force -Path $TempRoot | Out-Null
$Cloudflared = Join-Path $TempRoot "cloudflared.exe"

if (-not (Test-Path $Cloudflared)) {
    Write-Host "Downloading the official Cloudflare quick-tunnel client..."
    $Url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Cloudflared
}

$Arguments = @(
    "--fps", "$Fps",
    "--monitor", "$Monitor",
    "--cloudflared", $Cloudflared
)
if ($TargetTitle.Trim().Length -gt 0) {
    $Arguments += @("--target-title", $TargetTitle)
}

Write-Host ""
Write-Host "MIRU PC Frame Broker v0.1.0"
Write-Host "The broker keeps captured frames only in RAM."
Write-Host "When the MIRU_BROKER_URL line appears, copy that one line into the ChatGPT conversation."
Write-Host "Press Ctrl+C to end the test and invalidate the address."
Write-Host ""

& $Broker @Arguments
exit $LASTEXITCODE
