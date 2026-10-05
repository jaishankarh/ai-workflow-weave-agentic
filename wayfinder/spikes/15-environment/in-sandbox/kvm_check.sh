#!/usr/bin/env bash
# Does /dev/kvm reach the sandbox, and can the emulator use it? Last line is JSON.
set -uo pipefail
present=false; perms=""; rw=false
if [ -e /dev/kvm ]; then
  present=true
  perms=$(ls -l /dev/kvm)
  { exec 3<>/dev/kvm; } 2>/dev/null && rw=true && exec 3>&-
fi
accel=$(emulator -accel-check 2>&1 | tr '\n' ' ' | sed 's/"/\\"/g')
ok=false; echo "$accel" | grep -qiE 'KVM .*(is installed and usable|usable)' && ok=true
printf '{"ok":%s,"dev_kvm_present":%s,"dev_kvm_ls":"%s","opened_read_write":%s,"emulator_accel_check":"%s"}\n' \
  "$ok" "$present" "$perms" "$rw" "$accel"
