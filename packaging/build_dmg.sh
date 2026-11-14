#!/bin/bash
# Build Sift.app and Sift.dmg from a clean checkout (spec section 14). arm64 only.
#   packaging/build_dmg.sh            -> build/dist/Sift.app, build/Sift-VERSION.dmg
# SIFT_TEACHING=path/to/teaching.json bundles the owner's teaching (default local/teaching.json
# when it exists). It goes into this build only and is never committed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export SIFT_ROOT="$ROOT"
export SIFT_VERSION="$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)"
TEACHING="${SIFT_TEACHING:-$ROOT/local/teaching.json}"
OUT="$ROOT/build/package"
rm -rf "$OUT"
mkdir -p "$OUT"

echo "==> web UI"
(cd web && npm ci --silent && npm run build --silent)

echo "==> Python dependencies (locked)"
uv sync --locked --quiet

STAGE="$OUT/teaching"
export SIFT_STARTER_TEACHING=""
if [ -f "$TEACHING" ]; then
  mkdir -p "$STAGE"
  cp "$TEACHING" "$STAGE/starter.json"
  export SIFT_STARTER_TEACHING="$STAGE/starter.json"
  echo "==> bundling starter teaching from $TEACHING"
fi

echo "==> PyInstaller"
uv run --with "pyinstaller==6.22.3" pyinstaller --noconfirm --clean \
  --distpath "$OUT/dist" --workpath "$OUT/work" packaging/pyinstaller.spec

APP="$OUT/dist/Sift.app"
echo "==> ad-hoc signing"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

echo "==> dmg"
DMG="$OUT/Sift-$SIFT_VERSION.dmg"
SRC="$OUT/dmg-src"
mkdir -p "$SRC"
cp -R "$APP" "$SRC/"
if command -v create-dmg >/dev/null; then
  create-dmg --volname "Sift" --app-drop-link 400 150 "$DMG" "$SRC"
else
  ln -s /Applications "$SRC/Applications"
  hdiutil create -quiet -volname "Sift" -srcfolder "$SRC" -ov -format UDZO "$DMG"
fi
du -sh "$APP" "$DMG"
echo "Built $DMG"
