#!/usr/bin/env bash
# Build a React Native release APK (x86_64 only, to match the emulator).
#   build_apk.sh sample        generate a fresh RN app whose screen shows GET http://10.0.2.2:8080/hello
#   build_apk.sh /src-app      copy a real RN app mounted read-only at /src-app and build it as-is
# Last line of output is JSON with the APK path, package name and RN version.
set -euo pipefail
MODE="${1:-sample}"
WORK=/tmp/rn
rm -rf "$WORK" && mkdir -p "$WORK"

if [ "$MODE" = sample ]; then
  cd "$WORK"
  npx --yes @react-native-community/cli@latest init SpikeEnv --skip-git-init --install-pods false >/tmp/rn-init.log 2>&1 \
    || { tail -n 40 /tmp/rn-init.log; exit 1; }
  APP="$WORK/SpikeEnv"
  # Screen: fetch the seeded greeting from the Environment's backend and show it.
  APP_FILE="$APP/App.tsx"; [ -f "$APP_FILE" ] || APP_FILE="$APP/App.js"
  cat > "$APP_FILE" <<'TSX'
import React, {useEffect, useState} from 'react';
import {SafeAreaView, Text} from 'react-native';

export default function App() {
  const [text, setText] = useState('loading');
  useEffect(() => {
    fetch('http://10.0.2.2:8080/hello')
      .then(r => r.json())
      .then(j => setText(j.greeting))
      .catch(e => setText('error: ' + String(e)));
  }, []);
  return (
    <SafeAreaView style={{flex: 1, justifyContent: 'center', alignItems: 'center'}}>
      <Text testID="greeting" style={{fontSize: 22}}>{text}</Text>
    </SafeAreaView>
  );
}
TSX
  # Release builds block cleartext HTTP; the Environment's backend is plain HTTP.
  # (A finding in itself: mobile Run recipes must allow cleartext to the Environment.)
  python3 - "$APP/android/app/src/main/AndroidManifest.xml" <<'PY'
import re, sys
p = sys.argv[1]; s = open(p).read()
if 'usesCleartextTraffic' in s:
    s = re.sub(r'android:usesCleartextTraffic="[^"]*"', 'android:usesCleartextTraffic="true"', s)
else:
    s = s.replace('<application', '<application android:usesCleartextTraffic="true"', 1)
open(p, 'w').write(s)
PY
else
  cp -a "$MODE" "$WORK/app"
  rm -rf "$WORK/app/node_modules" "$WORK/app/android/.gradle" "$WORK/app/android/app/build"
  APP="$WORK/app"
  cd "$APP"
  if [ -f yarn.lock ]; then corepack enable >/dev/null 2>&1 || true; yarn install --frozen-lockfile >/tmp/rn-install.log 2>&1 || yarn install >/tmp/rn-install.log 2>&1
  else npm ci >/tmp/rn-install.log 2>&1 || npm install >/tmp/rn-install.log 2>&1; fi
fi

cd "$APP/android"
./gradlew --no-daemon assembleRelease -PreactNativeArchitectures=x86_64 >/tmp/gradle.log 2>&1 \
  || { tail -n 60 /tmp/gradle.log; exit 1; }

APK=$(ls -1 "$APP"/android/app/build/outputs/apk/release/*.apk | head -1)
# Several build-tools versions may be installed (Gradle adds its own), so pick one aapt2.
AAPT2=$(ls -1d "$ANDROID_HOME"/build-tools/*/aapt2 2>/dev/null | sort -V | tail -1)
PKG=$("$AAPT2" dump packagename "$APK" 2>/dev/null | head -1 || true)
# Fallback: the applicationId in the app's Gradle file.
[ -n "$PKG" ] || PKG=$(grep -hoE 'applicationId[ =]+"[^"]+"' "$APP"/android/app/build.gradle* 2>/dev/null | head -1 | sed -E 's/.*"([^"]+)"/\1/' || true)
RNV=$(node -p "require('$APP/node_modules/react-native/package.json').version" 2>/dev/null || echo unknown)
printf '{"ok":true,"apk":"%s","apk_bytes":%s,"package":"%s","react_native":"%s"}\n' \
  "$APK" "$(stat -c %s "$APK")" "$PKG" "$RNV"
