#!/usr/bin/env bash
# ponytail: copy (not symlink) — Chaquopy packaging follows real files reliably.
# Re-copy the reused engine into the app after editing the root modules.
set -e
here="$(cd "$(dirname "$0")" && pwd)"; root="$(cd "$here/.." && pwd)"
dst="$here/app/src/main/python"
cp "$root/mqtt_cat.py" "$root/mqtt_forward.py" "$root/mqtt_wormhole.py" "$dst/"
rm -rf "$dst/_vendor"; cp -r "$root/_vendor" "$dst/_vendor"
echo "synced mqtt_cat.py, mqtt_forward.py, _vendor -> $dst"
