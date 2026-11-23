#!/bin/bash
# Construit Quai.app sans Xcode (swiftc + bundle à la main) et l'installe dans /Applications/Outils maison.
# Le moteur côté Ally (ally/quai_ally.py) est embarqué : l'app le redéploie sur l'Ally s'il a changé.
set -euo pipefail
cd "$(dirname "$0")"
APP="build/Quai.app"; RES="$APP/Contents/Resources"
echo "→ nettoyage"; rm -rf build && mkdir -p "$APP/Contents/MacOS" "$RES"
echo "→ vérification du moteur"; python3 -m py_compile ally/quai_ally.py
echo "→ compilation"
swiftc -O -swift-version 5 -parse-as-library -module-name Quai -framework AppKit -framework SwiftUI \
    App/Quai.swift -o "$APP/Contents/MacOS/Quai"
cp ally/quai_ally.py "$RES/quai_ally.py"
[ -f icone/AppIcon.icns ] && cp icone/AppIcon.icns "$RES/AppIcon.icns"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Quai</string>
    <key>CFBundleDisplayName</key><string>Quai</string>
    <key>CFBundleExecutable</key><string>Quai</string>
    <key>CFBundleIdentifier</key><string>com.quai.app</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
    <key>NSPrincipalClass</key><string>SilentApplication</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSHumanReadableCopyright</key><string>Quai — outil personnel</string>
</dict>
</plist>
PLIST
printf 'APPL????' > "$APP/Contents/PkgInfo"
IDENTITE="Outils maison"
echo "→ signature"
if security find-identity -p codesigning 2>/dev/null | grep -q "$IDENTITE"; then
    codesign --force --deep --sign "$IDENTITE" "$APP" 2>&1 | sed 's/^/   /'
else
    codesign --force --deep --sign - "$APP" 2>&1 | sed 's/^/   /'
    echo "   certificat « $IDENTITE » introuvable : signature ad hoc"
fi
echo "→ installation"
DEST="/Applications/Outils maison"
mkdir -p "$DEST"
pkill -x Quai 2>/dev/null || true
rm -rf "$DEST/Quai.app"
cp -R "$APP" "$DEST/Quai.app"
touch "$DEST/Quai.app"
echo "→ terminé : $DEST/Quai.app"
