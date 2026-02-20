#!/usr/bin/env bash
set -euo pipefail

API_BASE="${API_BASE:-http://localhost:8001/api/v1}"
API_KEY="${API_KEY:-change-me}"
ROLE_ADMIN_HEADER=(-H "x-role: admin" -H "x-user-id: admin-1")
ROLE_STAFF_HEADER=(-H "x-role: staff" -H "x-user-id: staff-1")

json_get() {
  local key="$1"
  python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1],""))' "$key"
}

echo "[1/7] create cameras"
room_cam_resp=$(curl -sS -X POST "$API_BASE/cameras" -H "x-api-key: $API_KEY" "${ROLE_ADMIN_HEADER[@]}" -H "Content-Type: application/json" -d '{"location_zone":"S1-ROOM-101"}')
play_cam_resp=$(curl -sS -X POST "$API_BASE/cameras" -H "x-api-key: $API_KEY" "${ROLE_ADMIN_HEADER[@]}" -H "Content-Type: application/json" -d '{"location_zone":"S1-PLAY-A"}')
iso_cam_resp=$(curl -sS -X POST "$API_BASE/cameras" -H "x-api-key: $API_KEY" "${ROLE_ADMIN_HEADER[@]}" -H "Content-Type: application/json" -d '{"location_zone":"S1-ISOLATION-1"}')
room_cam_id=$(echo "$room_cam_resp" | json_get camera_id)
play_cam_id=$(echo "$play_cam_resp" | json_get camera_id)
iso_cam_id=$(echo "$iso_cam_resp" | json_get camera_id)
echo "room=$room_cam_id play=$play_cam_id isolation=$iso_cam_id"

echo "[2/7] create pet + booking"
owner_id="owner-smoke-$(date +%s)"
pet_resp=$(curl -sS -X POST "$API_BASE/animals" -H "x-api-key: $API_KEY" "${ROLE_ADMIN_HEADER[@]}" -H "Content-Type: application/json" -d "{\"species\":\"dog\",\"name\":\"Milo-smoke\",\"owner_id\":\"$owner_id\"}")
pet_id=$(echo "$pet_resp" | json_get animal_id)
start_at=$(date -u -d '-5 minutes' +%Y-%m-%dT%H:%M:%SZ)
end_at=$(date -u -d '+2 hours' +%Y-%m-%dT%H:%M:%SZ)
booking_resp=$(curl -sS -X POST "$API_BASE/bookings" -H "x-api-key: $API_KEY" "${ROLE_ADMIN_HEADER[@]}" -H "Content-Type: application/json" -d "{\"owner_id\":\"$owner_id\",\"pet_id\":\"$pet_id\",\"start_at\":\"$start_at\",\"end_at\":\"$end_at\",\"room_zone_id\":\"S1-ROOM-101\",\"status\":\"checked_in\"}")
booking_id=$(echo "$booking_resp" | json_get booking_id)
echo "pet=$pet_id booking=$booking_id"

echo "[3/7] owner status (room)"
status_room=$(curl -sS "$API_BASE/pets/$pet_id/status?owner_id=$owner_id" -H "x-api-key: $API_KEY" -H "x-role: owner" -H "x-user-id: $owner_id")
python3 -c 'import json,sys;j=json.loads(sys.argv[1]);assert j.get("current_zone_id")=="S1-ROOM-101",j;print("zone ok:",j.get("current_zone_id"),"cams:",j.get("cam_ids"))' "$status_room"

echo "[4/7] move zone to PLAY and verify token policy"
curl -sS -X POST "$API_BASE/staff/move-zone" -H "x-api-key: $API_KEY" "${ROLE_STAFF_HEADER[@]}" -H "Content-Type: application/json" -d "{\"pet_id\":\"$pet_id\",\"to_zone_id\":\"S1-PLAY-A\"}" >/dev/null

owner_token_code=$(curl -s -o /tmp/owner_token_resp.json -w '%{http_code}' -X POST "$API_BASE/auth/stream-token" -H "x-api-key: $API_KEY" -H "x-role: owner" -H "x-user-id: $owner_id" -H "Content-Type: application/json" -d "{\"owner_id\":\"$owner_id\",\"booking_id\":\"$booking_id\",\"pet_id\":\"$pet_id\"}")
if [[ "$owner_token_code" != "403" ]]; then
  echo "expected owner PLAY token to be 403, got $owner_token_code"
  cat /tmp/owner_token_resp.json
  exit 1
fi

echo "owner PLAY token denied as expected"
admin_token_resp=$(curl -sS -X POST "$API_BASE/auth/stream-token" -H "x-api-key: $API_KEY" "${ROLE_ADMIN_HEADER[@]}" -H "Content-Type: application/json" -d "{\"owner_id\":\"$owner_id\",\"booking_id\":\"$booking_id\",\"pet_id\":\"$pet_id\"}")
python3 -c 'import json,sys;j=json.loads(sys.argv[1]);assert j.get("cam_ids"),j;print("admin token cam_ids:",j.get("cam_ids"))' "$admin_token_resp"

echo "[5/7] care log + staff board"
curl -sS -X POST "$API_BASE/staff/logs" -H "x-api-key: $API_KEY" "${ROLE_STAFF_HEADER[@]}" -H "Content-Type: application/json" -d "{\"pet_id\":\"$pet_id\",\"booking_id\":\"$booking_id\",\"type\":\"feeding\",\"value\":\"ate 80%\",\"staff_id\":\"staff-1\"}" >/dev/null
board_resp=$(curl -sS "$API_BASE/staff/today-board" -H "x-api-key: $API_KEY" "${ROLE_STAFF_HEADER[@]}")
python3 -c 'import json,sys;j=json.loads(sys.argv[1]);assert j.get("total_active_bookings",0)>=1,j;print("staff board active:",j.get("total_active_bookings"))' "$board_resp"

echo "[6/7] camera health"
now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
for cam in "$room_cam_id" "$play_cam_id" "$iso_cam_id"; do
  curl -sS -X POST "$API_BASE/system/camera-health" -H "x-api-key: $API_KEY" -H "x-role: system" -H "Content-Type: application/json" -d "{\"camera_id\":\"$cam\",\"status\":\"healthy\",\"fps\":24.0,\"latency_ms\":120.0,\"last_frame_at\":\"$now\"}" >/dev/null
done
health_resp=$(curl -sS "$API_BASE/admin/camera-health" -H "x-api-key: $API_KEY" "${ROLE_STAFF_HEADER[@]}")
python3 -c 'import json,sys;j=json.loads(sys.argv[1]);assert len(j)>=3,j;print("camera health rows:",len(j))' "$health_resp"

echo "[7/8] ingest + live tracks endpoint smoke"
ingest_resp=$(curl -sS -X POST "$API_BASE/system/live-tracks/ingest" -H "x-api-key: $API_KEY" -H "x-role: system" -H "Content-Type: application/json" -d "{\"camera_id\":\"$room_cam_id\",\"detections\":[{\"source_track_id\":1,\"bbox_xyxy\":[10,20,90,140],\"conf\":0.91,\"animal_id\":\"$pet_id\"}]}")
python3 -c 'import json,sys;j=json.loads(sys.argv[1]);assert j.get("created_observations",0)>=1,j;print("ingest observations:",j.get("created_observations"))' "$ingest_resp"

live_resp=$(curl -sS "$API_BASE/live/tracks/latest?camera_id=$room_cam_id&limit=10" -H "x-api-key: $API_KEY" "${ROLE_STAFF_HEADER[@]}")
python3 -c 'import json,sys;j=json.loads(sys.argv[1]);assert "tracks" in j,j;print("live track count:",j.get("count",0))' "$live_resp"

echo "[8/8] alert evaluate + list"
eval_resp=$(curl -sS -X POST "$API_BASE/system/alerts/evaluate" -H "x-api-key: $API_KEY" -H "x-role: system")
python3 -c 'import json,sys;j=json.loads(sys.argv[1]);assert j.get("ok") is True,j;print("alert evaluate:",j.get("created_or_touched",0))' "$eval_resp"
alerts_resp=$(curl -sS "$API_BASE/staff/alerts?status=open&limit=10" -H "x-api-key: $API_KEY" "${ROLE_STAFF_HEADER[@]}")
python3 -c 'import json,sys;j=json.loads(sys.argv[1]);assert isinstance(j,list),j;print("open alerts:",len(j))' "$alerts_resp"

echo "✅ multi-camera smoke passed"
