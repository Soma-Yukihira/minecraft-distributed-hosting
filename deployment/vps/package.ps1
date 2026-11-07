# Build the VPS package on this PC: dist\mdh-server-<version>.tar.gz
#
#   powershell -ExecutionPolicy Bypass -File deployment\vps\package.ps1
#
# The archive holds the control plane and every dependency as Linux wheels (exactly the
# versions tested in .venv), plus install.sh / rollback.sh and the configuration templates.
# The VPS therefore installs offline, from files checked here; nothing is fetched from it.
# Only build\ and dist\ are written (both git-ignored).

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Create the virtualenv first (see README.md)." }
$env:PYTHONPATH = "$root\shared;$root\client"
Set-Location $root

$version = & $python -c "import mdh_shared; print(mdh_shared.__version__)"
$name = "mdh-server-$version"
$work = Join-Path $root "build\vps"
$stage = Join-Path $work $name
$wheels = Join-Path $stage "wheels"
if (Test-Path $work) { Remove-Item -Recurse -Force $work }
New-Item -ItemType Directory -Force $wheels | Out-Null
Write-Host "== $name"

# Our two packages (pure Python).
& $python -m pip wheel --quiet --no-deps --no-build-isolation -w $wheels .\shared .\server
if ($LASTEXITCODE -ne 0) { throw "building the mdh wheels failed" }

# Their dependencies for the VPS (Ubuntu 24.04, CPython 3.12, x86_64), pinned to this venv.
$constraints = Join-Path $work "constraints.txt"
& $python -m pip freeze --exclude-editable | Where-Object { $_ -notmatch "^mdh" -and $_ -notmatch " @ " } |
    Set-Content -Encoding ascii $constraints
$platform = @("--only-binary=:all:", "--implementation", "cp", "--python-version", "3.12",
              "--platform", "manylinux_2_39_x86_64", "--platform", "manylinux_2_28_x86_64",
              "--platform", "manylinux_2_17_x86_64", "--platform", "manylinux2014_x86_64")
& $python -m pip download --quiet @platform -c $constraints --find-links $wheels -d $wheels "mdh-server==$version"
if ($LASTEXITCODE -ne 0) { throw "downloading the Linux dependencies failed" }

# pip evaluated the markers for Windows: add the Linux-only dependencies (uvloop...) until the
# tree, walked as on the VPS, is complete.
for ($round = 0; ; $round++) {
    $absent = @(& $python -I deployment\vps\check_wheels.py $wheels "mdh-server==$version" | Where-Object { $_ })
    if ($LASTEXITCODE -eq 0) { break }
    if ($round -ge 3) { throw "the wheel set is incomplete for Linux: $($absent -join ', ')" }
    Write-Host "Linux-only dependencies: $($absent -join ', ')"
    & $python -m pip download --quiet --no-deps @platform -c $constraints -d $wheels @absent
    if ($LASTEXITCODE -ne 0) { throw "downloading $($absent -join ', ') failed" }
}

# Scripts and templates, with Unix line endings whatever Git did on checkout.
$utf8 = New-Object System.Text.UTF8Encoding $false
foreach ($file in "install.sh", "rollback.sh", "mdh.service", "nginx-mdh.conf", "mdh.env.template") {
    $text = [IO.File]::ReadAllText((Join-Path $PSScriptRoot $file)) -replace "`r`n", "`n"
    [IO.File]::WriteAllText((Join-Path $stage $file), $text, $utf8)
}
[IO.File]::WriteAllText((Join-Path $stage "VERSION"), "$version`n", $utf8)

New-Item -ItemType Directory -Force dist | Out-Null
$archive = Join-Path $root "dist\$name.tar.gz"
& tar.exe -czf $archive -C $work $name
if ($LASTEXITCODE -ne 0) { throw "tar failed" }

Get-ChildItem $wheels | ForEach-Object { Write-Host ("  {0}" -f $_.Name) }
$hash = (Get-FileHash -Algorithm SHA256 $archive).Hash.ToLower()
Write-Host ("Archive: {0} ({1:N1} MB)" -f $archive, ((Get-Item $archive).Length / 1MB))
Write-Host "SHA-256: $hash"
Write-Host ""
Write-Host "Copy to the VPS:  scp -i `$HOME\.ssh\user_vps dist\$name.tar.gz user@87.106.117.55:"
