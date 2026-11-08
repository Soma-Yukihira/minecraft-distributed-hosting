# The checks of the "Tests" workflow, on this PC (no GitHub minutes): the whole test suite, then
# known vulnerabilities in the dependencies. About 10 minutes.
#
#   powershell -ExecutionPolicy Bypass -File deployment\scripts\check.ps1
#   powershell -ExecutionPolicy Bypass -File deployment\scripts\check.ps1 -Quick   (tests only)
#
# Changes nothing: no file of the project, no package, no configuration. Exit code 0 = all good.

param([switch]$Quick)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Create the virtualenv first (see README.md)." }
Set-Location $root

# Like on GitHub: Qt without a screen, so that no window opens during the GUI tests.
$env:QT_QPA_PLATFORM = "offscreen"
$failed = @()
$started = Get-Date

Write-Host "== Tests"
& $python -m pytest -q -p no:cacheprovider
if ($LASTEXITCODE -ne 0) { $failed += "tests (code $LASTEXITCODE)" }

if (-not $Quick) {
    Write-Host ""
    Write-Host "== Vulnerabilites connues (pip-audit)"
    & $python -m pip_audit --local --skip-editable
    if ($LASTEXITCODE -ne 0) { $failed += "pip-audit (code $LASTEXITCODE)" }
}

$minutes = [math]::Round(((Get-Date) - $started).TotalMinutes, 1)
Write-Host ""
if ($failed) {
    Write-Host "ECHEC en $minutes min : $($failed -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host "Tout est bon ($minutes min)." -ForegroundColor Green
