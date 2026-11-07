# Simulate a second PC on this machine: a second, independent client connected to the local
# control plane started by dev-local.ps1 (run that one first, in another window).
#
#   powershell -ExecutionPolicy Bypass -File deployment\scripts\dev-second-pc.ps1
#
# Its data (settings, credentials, Java, worlds) lives in .\data\client-dev-2, fully separate
# from the first client, so the world really goes through the control plane to get here.
# Nothing here touches the VPS, the firewall or the system configuration.

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Create the virtualenv first (see README.md)." }

try {
    Invoke-WebRequest -Uri "http://127.0.0.1:8080/api/v1/health" -UseBasicParsing -TimeoutSec 3 | Out-Null
} catch {
    throw "The local control plane is not running: start deployment\scripts\dev-local.ps1 first."
}

$env:PYTHONPATH = "$root\shared;$root\server;$root\client"
$env:MDH_CLIENT_HOME = "$root\data\client-dev-2"
& $python -m mdh_client
