#!/usr/bin/env bash
# Create a throwaway AVD and cold-boot it headless; wait for sys.boot_completed.
# Last line is JSON. spike.py times the call.
set -uo pipefail
export ANDROID_AVD_HOME=/tmp/avd
mkdir -p "$ANDROID_AVD_HOME"
echo no | avdmanager create avd -n spike -k "$SPIKE_SYSTEM_IMAGE" -d pixel_6 --force >/tmp/avd-create.log 2>&1 \
  || { cat /tmp/avd-create.log; echo '{"ok":false,"stage":"create"}'; exit 1; }

nohup emulator -avd spike -no-window -no-audio -no-boot-anim -no-snapshot \
  -gpu swiftshader_indirect -memory 3072 -cores 2 >/tmp/emulator.log 2>&1 &
echo $! > /tmp/emulator.pid

adb start-server >/dev/null 2>&1
for _ in $(seq 1 600); do
  if [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ]; then
    adb shell input keyevent 82 >/dev/null 2>&1 || true   # dismiss the lock screen
    printf '{"ok":true,"api_level":"%s","abi":"%s"}\n' \
      "$(adb shell getprop ro.build.version.sdk | tr -d '\r')" "$(adb shell getprop ro.product.cpu.abi | tr -d '\r')"
    exit 0
  fi
  if ! kill -0 "$(cat /tmp/emulator.pid)" 2>/dev/null; then break; fi
  sleep 1
done
tail -n 40 /tmp/emulator.log
echo '{"ok":false,"stage":"boot"}'
exit 1
