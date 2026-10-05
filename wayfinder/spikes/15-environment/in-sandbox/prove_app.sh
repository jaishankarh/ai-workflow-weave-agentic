#!/usr/bin/env bash
# Install the APK, launch it, wait for the screen to settle, take a screenshot,
# and check the on-screen text against the expected greeting.
#   prove_app.sh <apk> <package> <expected-text-or-empty>
# Writes /tmp/proof/screen.png and /tmp/proof/ui.xml. Last line is JSON.
set -uo pipefail
APK="$1"; PKG="$2"; EXPECT="${3:-}"
mkdir -p /tmp/proof

before=$(adb shell pm list packages -3 2>/dev/null | tr -d '\r' | sort)
adb install -r -g "$APK" >/tmp/proof/install.log 2>&1 || { cat /tmp/proof/install.log; echo '{"ok":false,"stage":"install"}'; exit 1; }
# If the build step could not read the package name, take the one the install added.
if [ -z "$PKG" ]; then
  PKG=$(comm -13 <(echo "$before") <(adb shell pm list packages -3 | tr -d '\r' | sort) | head -1 | sed 's/^package://')
fi
[ -n "$PKG" ] || { echo '{"ok":false,"stage":"package-name"}'; exit 1; }
adb shell monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1

found=false
for _ in $(seq 1 45); do
  sleep 2
  adb shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1 && adb pull /sdcard/ui.xml /tmp/proof/ui.xml >/dev/null 2>&1
  if [ -n "$EXPECT" ] && grep -qF "$EXPECT" /tmp/proof/ui.xml 2>/dev/null; then found=true; break; fi
  [ -z "$EXPECT" ] && [ "$_" -ge 8 ] && break   # real app: just give it ~16s to draw
done
adb exec-out screencap -p > /tmp/proof/screen.png
on_screen=$(grep -o 'text="[^"]*"' /tmp/proof/ui.xml 2>/dev/null | head -20 | tr '\n' ' ' | sed 's/"/\\"/g')
ok=$found; [ -z "$EXPECT" ] && ok=true
printf '{"ok":%s,"package":"%s","expected_text_on_screen":%s,"screenshot_bytes":%s,"visible_text":"%s"}\n' \
  "$ok" "$PKG" "$found" "$(stat -c %s /tmp/proof/screen.png 2>/dev/null || echo 0)" "$on_screen"
