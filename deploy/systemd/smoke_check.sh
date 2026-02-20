#!/usr/bin/env bash
set -euo pipefail

# Smoke check for PawLuxe systemd services.
# Usage:
#   sudo bash deploy/systemd/smoke_check.sh --instance cam1
#   sudo bash deploy/systemd/smoke_check.sh --instance cam1 --restart

INSTANCE=""
RESTART=0
FOLLOW_LOGS=0
LOG_LINES=80

while [[ $# -gt 0 ]]; do
  case "$1" in
    --instance)
      INSTANCE="${2:-}"
      shift 2
      ;;
    --restart)
      RESTART=1
      shift
      ;;
    --follow-logs)
      FOLLOW_LOGS=1
      shift
      ;;
    --log-lines)
      LOG_LINES="${2:-80}"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "$INSTANCE" ]]; then
  echo "--instance is required (e.g. --instance cam1)" >&2
  exit 2
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/systemd/smoke_check.sh --instance ${INSTANCE}" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

EXPORT_SERVICE="pawluxe-export-worker.service"
RTSP_SERVICE="pawluxe-rtsp@${INSTANCE}.service"
CAM_ENV_FILE="$ROOT/deploy/systemd/rtsp-worker-${INSTANCE}.env"
COMMON_ENV_FILE="$ROOT/deploy/systemd/rtsp-worker-common.env"

ok() { echo "[OK] $*"; }
warn() { echo "[WARN] $*"; }
err() { echo "[ERROR] $*" >&2; }

if [[ ! -f "$COMMON_ENV_FILE" ]]; then
  err "Missing $COMMON_ENV_FILE"
  exit 1
fi
if [[ ! -f "$CAM_ENV_FILE" ]]; then
  err "Missing $CAM_ENV_FILE"
  exit 1
fi
ok "Env files found"

if ! systemctl list-unit-files | grep -q '^pawluxe-export-worker\.service'; then
  err "Unit not installed: $EXPORT_SERVICE"
  err "Run: sudo bash deploy/systemd/install_systemd.sh"
  exit 1
fi
if ! systemctl list-unit-files | grep -q '^pawluxe-rtsp@\.service'; then
  err "Unit not installed: pawluxe-rtsp@.service"
  err "Run: sudo bash deploy/systemd/install_systemd.sh"
  exit 1
fi
ok "Unit files are installed"

if [[ "$RESTART" -eq 1 ]]; then
  systemctl restart "$EXPORT_SERVICE"
  systemctl restart "$RTSP_SERVICE"
  ok "Services restarted"
fi

# Do not force start by default, but ensure enabled+active state is visible.
EXP_ENABLED="$(systemctl is-enabled "$EXPORT_SERVICE" 2>/dev/null || true)"
RTSP_ENABLED="$(systemctl is-enabled "$RTSP_SERVICE" 2>/dev/null || true)"
EXP_ACTIVE="$(systemctl is-active "$EXPORT_SERVICE" 2>/dev/null || true)"
RTSP_ACTIVE="$(systemctl is-active "$RTSP_SERVICE" 2>/dev/null || true)"

echo "[INFO] $EXPORT_SERVICE enabled=$EXP_ENABLED active=$EXP_ACTIVE"
echo "[INFO] $RTSP_SERVICE enabled=$RTSP_ENABLED active=$RTSP_ACTIVE"

if [[ "$EXP_ACTIVE" != "active" || "$RTSP_ACTIVE" != "active" ]]; then
  warn "One or more services are not active."
  warn "Start with:"
  warn "  sudo systemctl enable --now $EXPORT_SERVICE"
  warn "  sudo systemctl enable --now $RTSP_SERVICE"
fi

echo "[INFO] Last ${LOG_LINES} log lines for $EXPORT_SERVICE"
journalctl -u "$EXPORT_SERVICE" -n "$LOG_LINES" --no-pager || true

echo "[INFO] Last ${LOG_LINES} log lines for $RTSP_SERVICE"
journalctl -u "$RTSP_SERVICE" -n "$LOG_LINES" --no-pager || true

if [[ "$FOLLOW_LOGS" -eq 1 ]]; then
  echo "[INFO] Following logs (Ctrl+C to exit)"
  journalctl -u "$EXPORT_SERVICE" -u "$RTSP_SERVICE" -f
fi

ok "Smoke check completed"
