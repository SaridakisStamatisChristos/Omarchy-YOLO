#!/bin/bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/omarchy-yolo"
BIN_DIR="$HOME/.local/bin"
CONFIG_DIR="$HOME/.config/omarchy-yolo"
PLUGIN_DIR="$HOME/.config/omarchy/plugins/dev.aether.yolo"
SERVICE_DIR="$HOME/.config/systemd/user"

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
python3 - <<'PY'
import sys
if sys.version_info < (3, 12):
    raise SystemExit("Python 3.12+ is required")
PY

mkdir -p "$APP_DIR/src" "$BIN_DIR" "$CONFIG_DIR" "$SERVICE_DIR"
rm -rf "$APP_DIR/src/omarchy_yolo"
cp -a "$ROOT/src/omarchy_yolo" "$APP_DIR/src/omarchy_yolo"
cp "$ROOT/pyproject.toml" "$APP_DIR/pyproject.toml"

if [[ ! -f "$CONFIG_DIR/config.toml" ]]; then
  cp "$ROOT/config.example.toml" "$CONFIG_DIR/config.toml"
fi
if [[ ! -f "$CONFIG_DIR/env" ]]; then
  : > "$CONFIG_DIR/env"
  chmod 600 "$CONFIG_DIR/env"
fi

cat > "$BIN_DIR/yolo" <<'WRAPPER'
#!/bin/bash
set -euo pipefail
export PYTHONPATH="$HOME/.local/share/omarchy-yolo/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m omarchy_yolo "$@"
WRAPPER
chmod 755 "$BIN_DIR/yolo"
ln -sfn "$BIN_DIR/yolo" "$BIN_DIR/omarchy-yolo"

cp "$ROOT/systemd/omarchy-yolo.service" "$SERVICE_DIR/omarchy-yolo.service"

rm -rf "$PLUGIN_DIR"
mkdir -p "$PLUGIN_DIR"
cp -a "$ROOT/shell-plugin/." "$PLUGIN_DIR/"

PLUGIN_VALID=1
if command -v omarchy >/dev/null; then
  if ! omarchy plugin validate "$PLUGIN_DIR"; then
    PLUGIN_VALID=0
    echo "Omarchy rejected the shell plugin manifest; leaving it installed but disabled." >&2
  fi
fi
if command -v omarchy-shell >/dev/null; then
  omarchy-shell -q shell rescanPlugins || true
fi
if (( PLUGIN_VALID )) && command -v omarchy >/dev/null; then
  omarchy plugin enable dev.aether.yolo >/dev/null 2>&1 || true
fi

if command -v systemctl >/dev/null; then
  systemctl --user daemon-reload
  systemctl --user enable omarchy-yolo.service >/dev/null
  systemctl --user restart omarchy-yolo.service
fi

echo "Installed Omarchy YOLO 1.0.1"
echo "Run: yolo doctor"
echo "Then: yolo run --watch \"Audit this repo, fix every material defect, and leave a release-ready candidate\""
