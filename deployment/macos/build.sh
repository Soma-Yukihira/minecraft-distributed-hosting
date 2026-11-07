#!/usr/bin/env bash
# Build the macOS client (Apple chips), on a Mac: the .app, its self-test, then
#   - MinecraftDistributedHosting-<version>-macos-arm64.zip  for the automatic updates
#   - MinecraftDistributedHosting-<version>-macos-arm64.dmg  for the first installation
#
#   bash deployment/macos/build.sh
#
# Needs macOS 12 or later on an Apple chip, and Python 3.12 (python.org or Homebrew); creates
# .venv-mac the first time. Only build/, dist/ and .venv-mac/ are written (git-ignored).
# Not signed by Apple (no developer account): PyInstaller signs "ad hoc", and the first launch
# needs "Ouvrir quand même" (LISEZMOI.txt, copied into the .dmg).
# The update signature is made afterwards on the Windows PC, where the key stays:
#   python deployment\windows\sign_release.py sign MinecraftDistributedHosting-<version>-macos-arm64.zip
set -euo pipefail
cd "$(dirname "$0")/../.."
[ "$(uname -s)" = Darwin ] || { echo "À lancer sur un Mac." >&2; exit 1; }
[ "$(uname -m)" = arm64 ] || { echo "Mac à puce Apple (M1 ou plus récent) attendu." >&2; exit 1; }
PY="${PYTHON:-python3.12}"
command -v "$PY" >/dev/null || { echo "Python 3.12 introuvable : https://www.python.org/downloads/macos/" >&2; exit 1; }

if [ ! -x .venv-mac/bin/python ]; then
    "$PY" -m venv .venv-mac
    # Exact versions and SHA-256 of every package (lock_requirements.py): the same as the Windows
    # build, and pip refuses any other file. The project itself comes from this checkout.
    .venv-mac/bin/python -m pip install --quiet --require-hashes --only-binary=:all: --no-deps \
        -r deployment/macos/requirements.txt
    .venv-mac/bin/python -m pip install --quiet --no-deps --no-build-isolation -e ./shared -e ./client
fi
python=.venv-mac/bin/python
export PYTHONPATH="$PWD/shared:$PWD/client"
version="$("$python" -c 'import mdh_shared; print(mdh_shared.__version__)')"
export MDH_VERSION="$version"
echo "== Minecraft Distributed Hosting $version (macOS, puces Apple)"
mkdir -p build dist/macos

# Icon: the same drawing as on Windows, at every size macOS wants.
"$python" deployment/windows/make_icon.py build/app.png
iconset=build/app.iconset
rm -rf "$iconset" && mkdir "$iconset"
for s in 16 32 128 256 512; do
    sips -z "$s" "$s" build/app.png --out "$iconset/icon_${s}x${s}.png" >/dev/null
    sips -z $((s * 2)) $((s * 2)) build/app.png --out "$iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$iconset" -o build/app.icns

"$python" -m PyInstaller --noconfirm --clean --log-level WARN \
    --distpath dist/macos --workpath build/pyinstaller-mac deployment/macos/mdh-client-mac.spec
app="dist/macos/Minecraft Distributed Hosting.app"

report="$PWD/build/self-test-mac.json"
rm -f "$report"
if ! "$app/Contents/MacOS/MinecraftDistributedHosting" --self-test "$report"; then
    cat "$report" 2>/dev/null || true
    echo "L'application construite a raté son auto-test." >&2
    exit 1
fi
echo "Self-test: $(cat "$report")"
du -sh "$app"

# Updates: ditto keeps the links and permissions inside the .app (zip would not).
zip="dist/macos/MinecraftDistributedHosting-$version-macos-arm64.zip"
rm -f "$zip"
ditto -c -k --sequesterRsrc --keepParent "$app" "$zip"

# First installation: drag the application onto "Applications".
dmg="dist/macos/MinecraftDistributedHosting-$version-macos-arm64.dmg"
stage=build/dmg
rm -rf "$stage" && mkdir -p "$stage"
cp -R "$app" "$stage/"
ln -s /Applications "$stage/Applications"
cp deployment/macos/LISEZMOI.txt "$stage/"
rm -f "$dmg"
hdiutil create -volname "Minecraft Distributed Hosting" -srcfolder "$stage" -ov -format UDZO "$dmg" >/dev/null

echo
echo "Première installation : $dmg"
echo "Mises à jour          : $zip"
echo "Ensuite, copie le .zip sur le PC Windows et signe-le là-bas (la clé n'en sort jamais) :"
echo "  python deployment\\windows\\sign_release.py sign $(basename "$zip") --notes \"...\""
