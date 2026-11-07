# Try the whole thing on this PC: a local control plane + the Windows client.
# Everything stays in .\data (git-ignored); delete that folder to start from scratch.
#
#   powershell -ExecutionPolicy Bypass -File deployment\scripts\dev-local.ps1
#
# Nothing here touches the VPS, the firewall or the system configuration.

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Create the virtualenv first (see README.md)." }

$env:PYTHONPATH = "$root\shared;$root\server;$root\client"

# Local control plane on 127.0.0.1:8080, open registration (local tests only).
# Minecraft relay on 127.0.0.1:25566 (25565 is the Minecraft server of this PC). The domain
# 127.0.0.1.nip.io is a public wildcard name for 127.0.0.1: personal addresses
# (<token>.127.0.0.1.nip.io) work without touching any DNS.
$serverEnv = @{
    MDH_DATABASE_PATH = "$root\data\dev-control-plane\mdh.sqlite3"
    MDH_STORAGE_DIR = "$root\data\dev-control-plane\storage"
    MDH_REGISTRATION_MODE = "open"
    MDH_EXPOSE_DOCS = "1"
    MDH_RELAY_BIND = "127.0.0.1"
    MDH_RELAY_PORT = "25566"
    MDH_RELAY_DOMAIN = "127.0.0.1.nip.io"
}
$serverEnv.GetEnumerator() | ForEach-Object { Set-Item "env:$($_.Key)" $_.Value }
$server = Start-Process -FilePath $python -ArgumentList "-m", "mdh_server", "serve", "--bind", "127.0.0.1:8080" `
    -WorkingDirectory $root -PassThru -WindowStyle Minimized
Write-Host "Control plane started (PID $($server.Id)) - API docs: http://127.0.0.1:8080/api/v1/docs"

# Client data next to the project (reuses Java/Paper already downloaded by the tests).
$env:MDH_CLIENT_HOME = "$root\data\client-dev"
try {
    & $python -m mdh_client
} finally {
    Stop-Process -Id $server.Id -ErrorAction SilentlyContinue
    Write-Host "Control plane stopped."
}
