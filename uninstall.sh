#!/bin/bash
set -euo pipefail

PURGE=0
[[ ${1:-} == "--purge" ]] && PURGE=1

if command -v systemctl >/dev/null; then
  systemctl --user disable --now omarchy-yolo.service >/dev/null 2>&1 || true
fi
rm -f "$HOME/.config/systemd/user/omarchy-yolo.service"
rm -f "$HOME/.local/bin/yolo" "$HOME/.local/bin/omarchy-yolo"
rm -rf "$HOME/.local/share/omarchy-yolo"
rm -rf "$HOME/.config/omarchy/plugins/dev.aether.yolo"

if command -v omarchy-shell >/dev/null; then
  omarchy-shell -q shell rescanPlugins || true
fi
if command -v systemctl >/dev/null; then
  systemctl --user daemon-reload || true
fi

if (( PURGE )); then
  rm -rf "$HOME/.config/omarchy-yolo" "$HOME/.local/state/omarchy-yolo"
  echo "Uninstalled and purged Omarchy YOLO."
else
  echo "Uninstalled Omarchy YOLO. Config/state preserved; use --purge to remove them."
fi
