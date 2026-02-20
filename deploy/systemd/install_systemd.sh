#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SYSTEMD_DIR="/etc/systemd/system"
TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || true)}"

if [[ -z "${TARGET_USER}" ]]; then
  TARGET_USER="$(id -un)"
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/systemd/install_systemd.sh" >&2
  exit 1
fi

sed \
  -e "s|__PAWLUXE_USER__|$TARGET_USER|g" \
  -e "s|__PAWLUXE_ROOT__|$ROOT|g" \
  "$ROOT/deploy/systemd/pawluxe-export-worker.service" \
  > "$SYSTEMD_DIR/pawluxe-export-worker.service"

sed \
  -e "s|__PAWLUXE_USER__|$TARGET_USER|g" \
  -e "s|__PAWLUXE_ROOT__|$ROOT|g" \
  "$ROOT/deploy/systemd/pawluxe-rtsp@.service" \
  > "$SYSTEMD_DIR/pawluxe-rtsp@.service"

chmod 0644 "$SYSTEMD_DIR/pawluxe-export-worker.service" "$SYSTEMD_DIR/pawluxe-rtsp@.service"

systemctl daemon-reload

echo "Installed systemd units."
echo "Resolved target user: $TARGET_USER"
echo "Resolved project root: $ROOT"
echo "Next steps:"
echo "1) edit $ROOT/deploy/systemd/rtsp-worker-common.env"
echo "2) cp $ROOT/deploy/systemd/rtsp-worker-example.env $ROOT/deploy/systemd/rtsp-worker-cam1.env"
echo "3) edit CAMERA_ID in rtsp-worker-cam1.env"
echo "4) systemctl enable --now pawluxe-export-worker.service"
echo "5) systemctl enable --now pawluxe-rtsp@cam1.service"
