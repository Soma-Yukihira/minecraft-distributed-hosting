# Build the Windows client: PyInstaller folder, self-test of the frozen app, then the installer.
#
#   powershell -ExecutionPolicy Bypass -File deployment\windows\build.ps1
#
# Output: dist\MinecraftDistributedHosting\ (the program) and, when Inno Setup 6 is installed,
# dist\installer\MinecraftDistributedHosting-Setup-<version>.exe, plus its signature for the
# automatic updates (<installer>.json) when the update key exists (sign_release.py keygen).
# The Mac .zip of the same version, if it is in dist\macos\, is signed at the same time.
#   -Notes "..."  short text shown to the players with the update
# Only build\ and dist\ are written (both git-ignored); nothing is installed on this PC.

param([string]$Notes = "")
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Create the virtualenv first (see README.md)." }
$env:PYTHONPATH = "$root\shared;$root\client"
Set-Location $root

$version = & $python -c "import mdh_shared; print(mdh_shared.__version__)"
Write-Host "== Minecraft Distributed Hosting $version"

New-Item -ItemType Directory -Force build | Out-Null
& $python deployment\windows\make_icon.py build\app.ico
if ($LASTEXITCODE -ne 0) { throw "icon generation failed" }

& $python -m PyInstaller --noconfirm --clean --log-level WARN `
    --distpath dist --workpath build\pyinstaller deployment\windows\mdh-client.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$exe = "dist\MinecraftDistributedHosting\MinecraftDistributedHosting.exe"
$report = Join-Path $root "build\self-test.json"
Remove-Item $report -ErrorAction SilentlyContinue
$p = Start-Process -FilePath $exe -ArgumentList "--self-test", "`"$report`"" -Wait -PassThru
$result = Get-Content $report -Raw
Write-Host "Self-test: $result"
if ($p.ExitCode -ne 0) { throw "the packaged application failed its self-test" }
$size = (Get-ChildItem -Recurse "dist\MinecraftDistributedHosting" | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("Program folder: {0:N0} MB" -f $size)

$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    Write-Warning "Inno Setup 6 not found: program built, installer skipped (https://jrsoftware.org/isinfo.php)."
    exit 0
}
& $iscc /Q "/DAppVersion=$version" deployment\windows\installer.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
$installer = "dist\installer\MinecraftDistributedHosting-Setup-$version.exe"
Write-Host ("Installer: {0} ({1:N0} MB)" -f (Resolve-Path $installer), ((Get-Item $installer).Length / 1MB))

# Every version is published for Windows and for Mac together. The Mac .zip is built on a Mac
# (or by GitHub Actions, .github/workflows/build-macos.yml) and copied into dist\macos\.
$macZip = "dist\macos\MinecraftDistributedHosting-$version-macos-arm64.zip"
$hasMac = Test-Path $macZip
if ($hasMac) {
    # Signing vouches for the file: it must be the one GitHub built (fingerprint shown in the
    # summary of the run, and in SHA256SUMS next to it), not a copy changed on the way.
    $macHash = (Get-FileHash -Algorithm SHA256 $macZip).Hash.ToLower()
    Write-Host "Mac zip SHA-256: $macHash (compare with the summary of the GitHub run)"
    $sums = "dist\macos\SHA256SUMS"
    if (Test-Path $sums) {
        $line = Select-String -Path $sums -SimpleMatch (Split-Path $macZip -Leaf) | Select-Object -First 1
        if (-not $line -or $line.Line.Split(" ")[0].ToLower() -ne $macHash) {
            throw "the Mac zip does not match SHA256SUMS from the GitHub build: not signed"
        }
    } else {
        Write-Warning "No dist\macos\SHA256SUMS: check the fingerprint above against the GitHub run by hand."
    }
}
if (Test-Path (Join-Path $HOME ".mdh-release\update-signing-key.pem")) {
    $toSign = @($installer)
    if ($hasMac) { $toSign += $macZip }
    foreach ($file in $toSign) {
        $signArgs = @("deployment\windows\sign_release.py", "sign", $file)
        if ($Notes) { $signArgs += @("--notes", $Notes) }  # an empty argument would be dropped
        & $python @signArgs
        if ($LASTEXITCODE -ne 0) { throw "signing failed: $file" }
    }
} else {
    Write-Warning "No update key (deployment\windows\sign_release.py keygen): installer not signed, it cannot be published as an update."
}
$files = "dist\installer\MinecraftDistributedHosting-Setup-$version.exe*"
$publish = "~/MinecraftDistributedHosting-Setup-$version.exe"
if ($hasMac) {
    $files += " $macZip*"
    $publish += " ~/MinecraftDistributedHosting-$version-macos-arm64.zip"
} else {
    Write-Warning ("No Mac version $version in dist\macos: the Macs will not be updated. Get " +
                   "MinecraftDistributedHosting-$version-macos-arm64.zip (GitHub Actions or a Mac), put it in " +
                   "dist\macos and run this script again.")
}
Write-Host ""
Write-Host "Publish (from the folder of the VPS archive):"
Write-Host "  scp -i `$HOME\.ssh\user_vps $files user@87.106.117.55:"
Write-Host "  sudo bash install.sh publish-client $publish"
