#!/usr/bin/env bash
set -euo pipefail

# Usage:
# API_KEY=... CAM1_RTSP=rtsp://... CAM2_RTSP=rtsp://... ./scripts/run_multicam_reid.sh

API_URL="${API_URL:-http://127.0.0.1:8000}"
API_KEY="${API_KEY:-replace-with-strong-api-key}"
DEVICE="${DEVICE:-cuda:0}"

CAM1_ZONE="${CAM1_ZONE:-room-a}"
CAM2_ZONE="${CAM2_ZONE:-room-b}"
CAM1_RTSP="${CAM1_RTSP:-}"
CAM2_RTSP="${CAM2_RTSP:-}"

CONF_THRESHOLD="${CONF_THRESHOLD:-0.25}"
IOU_THRESHOLD="${IOU_THRESHOLD:-0.45}"
CLASSES_CSV="${CLASSES_CSV:-15,16}"
FRAME_STRIDE="${FRAME_STRIDE:-1}"
REID_MATCH_THRESHOLD="${REID_MATCH_THRESHOLD:-0.68}"
FALLBACK_ANIMAL_ID="${FALLBACK_ANIMAL_ID:-system-reid-auto}"
RECORD_SEGMENTS="${RECORD_SEGMENTS:-1}"
RECORD_DIR="${RECORD_DIR:-storage/uploads/segments}"
SEGMENT_SECONDS="${SEGMENT_SECONDS:-20}"

if [[ -z "$CAM1_RTSP" || -z "$CAM2_RTSP" ]]; then
  echo "[ERROR] CAM1_RTSP and CAM2_RTSP are required." >&2
  echo "Example:" >&2
  echo "API_KEY=... CAM1_RTSP=rtsp://cam1 CAM2_RTSP=rtsp://cam2 ./scripts/run_multicam_reid.sh" >&2
  exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
  echo "[ERROR] .venv not found. Create/activate venv first." >&2
  exit 1
fi

create_camera() {
  local zone="$1"
  local rtsp="$2"

  local payload
  payload=$(cat <<JSON
{"location_zone":"$zone","stream_url":"$rtsp","installed_height_m":2.8,"tilt_deg":25.0}
JSON
)

  curl -sS -X POST "$API_URL/api/v1/cameras" \
    -H "x-api-key: $API_KEY" \
    -H "Content-Type: application/json" \
    -d "$payload"
}

echo "[1/3] Creating camera rows..."
CAM1_JSON="$(create_camera "$CAM1_ZONE" "$CAM1_RTSP")"
CAM2_JSON="$(create_camera "$CAM2_ZONE" "$CAM2_RTSP")"

CAM1_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["camera_id"])' <<< "$CAM1_JSON")"
CAM2_ID="$(python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["camera_id"])' <<< "$CAM2_JSON")"

echo "CAM1_ID=$CAM1_ID"
echo "CAM2_ID=$CAM2_ID"

echo "[2/3] Installing tracking extras (if needed)..."
.venv/bin/pip -q install -r requirements-tracking.txt

echo "[3/3] Starting multi-camera worker (reid_auto)..."
CMD=(.venv/bin/python -m app.workers.multi_camera_tracking_worker
  --camera-ids "$CAM1_ID,$CAM2_ID"
  --device "$DEVICE"
  --conf-threshold "$CONF_THRESHOLD"
  --iou-threshold "$IOU_THRESHOLD"
  --classes-csv "$CLASSES_CSV"
  --frame-stride "$FRAME_STRIDE"
  --global-id-mode reid_auto
  --reid-match-threshold "$REID_MATCH_THRESHOLD"
  --fallback-animal-id "$FALLBACK_ANIMAL_ID"
)

if [[ "$RECORD_SEGMENTS" == "1" ]]; then
  CMD+=(--record-segments --record-dir "$RECORD_DIR" --segment-seconds "$SEGMENT_SECONDS")
fi

exec "${CMD[@]}"
