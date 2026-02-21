import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db.session import init_db
from app.main import app

client = TestClient(app)
init_db()


def _headers(role: str, user_id: str = "") -> dict[str, str]:
    out = {"x-api-key": "change-me", "x-role": role}
    if user_id:
        out["x-user-id"] = user_id
    return out


def test_db_session_token_auth_and_camera_health() -> None:
    suffix = str(uuid.uuid4())[:8]

    owner_user = client.post(
        "/api/v1/auth/users",
        json={"role": "owner", "email": f"owner-{suffix}@pawluxe.test", "display_name": "Owner"},
        headers=_headers("system"),
    )
    assert owner_user.status_code == 200
    owner_user_id = owner_user.json()["user_id"]

    owner_session = client.post(
        "/api/v1/auth/sessions",
        json={"user_id": owner_user_id, "ttl_minutes": 60},
        headers=_headers("system"),
    )
    assert owner_session.status_code == 200
    session_token = owner_session.json()["session_token"]

    animal_resp = client.post(
        "/api/v1/animals",
        json={"species": "dog", "name": f"Milo-{suffix}", "owner_id": owner_user_id},
        headers={"x-api-key": "change-me", "x-session-token": session_token},
    )
    assert animal_resp.status_code == 200
    pet_id = animal_resp.json()["animal_id"]

    room_zone = f"S1-ROOM-{suffix}"
    cam_resp = client.post(
        "/api/v1/cameras",
        json={"location_zone": room_zone},
        headers=_headers("admin", "admin-1"),
    )
    assert cam_resp.status_code == 200
    cam_id = cam_resp.json()["camera_id"]

    booking_resp = client.post(
        "/api/v1/bookings",
        json={
            "owner_id": owner_user_id,
            "pet_id": pet_id,
            "start_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            "end_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "room_zone_id": room_zone,
            "status": "checked_in",
        },
        headers=_headers("admin", "admin-1"),
    )
    assert booking_resp.status_code == 200

    status_resp = client.get(
        f"/api/v1/pets/{pet_id}/status",
        headers={"x-api-key": "change-me", "x-session-token": session_token},
    )
    assert status_resp.status_code == 200
    assert status_resp.json()["owner_id"] == owner_user_id

    health_upsert = client.post(
        "/api/v1/system/camera-health",
        json={
            "camera_id": cam_id,
            "status": "healthy",
            "fps": 23.5,
            "latency_ms": 120.0,
            "last_frame_at": datetime.now(timezone.utc).isoformat(),
            "reconnect_count": 0,
        },
        headers=_headers("system"),
    )
    assert health_upsert.status_code == 200

    health_list = client.get(
        "/api/v1/admin/camera-health",
        headers=_headers("staff", "staff-1"),
    )
    assert health_list.status_code == 200
    assert any(row["camera_id"] == cam_id for row in health_list.json())


def test_stream_audit_log_records_issue_and_verify() -> None:
    suffix = str(uuid.uuid4())[:8]
    owner_id = f"owner-{suffix}"

    animal_resp = client.post(
        "/api/v1/animals",
        json={"species": "cat", "name": f"Nabi-{suffix}", "owner_id": owner_id},
        headers=_headers("admin", "admin-1"),
    )
    pet_id = animal_resp.json()["animal_id"]

    zone_id = f"S1-ROOM-{suffix}"
    cam_resp = client.post(
        "/api/v1/cameras",
        json={"location_zone": zone_id},
        headers=_headers("admin", "admin-1"),
    )
    cam_id = cam_resp.json()["camera_id"]

    booking_resp = client.post(
        "/api/v1/bookings",
        json={
            "owner_id": owner_id,
            "pet_id": pet_id,
            "start_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            "end_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "room_zone_id": zone_id,
            "status": "checked_in",
        },
        headers=_headers("admin", "admin-1"),
    )
    booking_id = booking_resp.json()["booking_id"]

    token_resp = client.post(
        "/api/v1/auth/stream-token",
        json={"owner_id": owner_id, "booking_id": booking_id, "pet_id": pet_id},
        headers=_headers("owner", owner_id),
    )
    assert token_resp.status_code == 200
    token = token_resp.json()["token"]

    verify_resp = client.get(
        "/api/v1/auth/stream-verify-hook",
        params={"token": token, "cam_id": cam_id},
        headers=_headers("system"),
    )
    assert verify_resp.status_code == 200

    logs_resp = client.get(
        "/api/v1/admin/stream-audit-logs",
        headers=_headers("admin", "admin-1"),
    )
    assert logs_resp.status_code == 200
    actions = [row["action"] for row in logs_resp.json()]
    assert "issue" in actions
    assert "verify" in actions


def test_staff_today_board_returns_active_items() -> None:
    suffix = str(uuid.uuid4())[:8]
    owner_id = f"owner-{suffix}"

    animal_resp = client.post(
        "/api/v1/animals",
        json={"species": "dog", "name": f"Milo-{suffix}", "owner_id": owner_id},
        headers=_headers("admin", "admin-1"),
    )
    assert animal_resp.status_code == 200
    pet_id = animal_resp.json()["animal_id"]

    zone = f"S1-ROOM-{suffix}"
    booking_resp = client.post(
        "/api/v1/bookings",
        json={
            "owner_id": owner_id,
            "pet_id": pet_id,
            "start_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
            "end_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "room_zone_id": zone,
            "status": "checked_in",
        },
        headers=_headers("admin", "admin-1"),
    )
    assert booking_resp.status_code == 200
    booking_id = booking_resp.json()["booking_id"]

    log_resp = client.post(
        "/api/v1/staff/logs",
        json={
            "pet_id": pet_id,
            "booking_id": booking_id,
            "type": "feeding",
            "value": "ate all",
            "staff_id": "staff-1",
        },
        headers=_headers("staff", "staff-1"),
    )
    assert log_resp.status_code == 200

    board_resp = client.get("/api/v1/staff/today-board", headers=_headers("staff", "staff-1"))
    assert board_resp.status_code == 200
    body = board_resp.json()
    assert body["total_active_bookings"] >= 1
    assert len(body["items"]) >= 1
    assert "alerts_summary" in body
    assert "open_count" in body["alerts_summary"]
    assert "critical_open_count" in body["alerts_summary"]
    matched = [row for row in body["items"] if row["booking_id"] == booking_id]
    assert matched
    assert matched[0]["next_action"] == "potty_check"


def test_owner_dashboard_and_booking_report() -> None:
    suffix = str(uuid.uuid4())[:8]
    owner_id = f"owner-{suffix}"

    animal_resp = client.post(
        "/api/v1/animals",
        json={"species": "cat", "name": f"Nabi-{suffix}", "owner_id": owner_id},
        headers=_headers("admin", "admin-1"),
    )
    assert animal_resp.status_code == 200
    pet_id = animal_resp.json()["animal_id"]

    zone = f"S1-ROOM-{suffix}"
    camera_resp = client.post(
        "/api/v1/cameras",
        json={"location_zone": zone},
        headers=_headers("admin", "admin-1"),
    )
    assert camera_resp.status_code == 200

    booking_resp = client.post(
        "/api/v1/bookings",
        json={
            "owner_id": owner_id,
            "pet_id": pet_id,
            "start_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
            "end_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "room_zone_id": zone,
            "status": "checked_in",
        },
        headers=_headers("admin", "admin-1"),
    )
    assert booking_resp.status_code == 200
    booking_id = booking_resp.json()["booking_id"]

    move_resp = client.post(
        "/api/v1/staff/move-zone",
        json={"pet_id": pet_id, "to_zone_id": zone},
        headers=_headers("staff", "staff-1"),
    )
    assert move_resp.status_code == 200

    log_resp = client.post(
        "/api/v1/staff/logs",
        json={
            "pet_id": pet_id,
            "booking_id": booking_id,
            "type": "medication",
            "value": "pill 1",
            "staff_id": "staff-1",
        },
        headers=_headers("staff", "staff-1"),
    )
    assert log_resp.status_code == 200

    event_resp = client.post(
        "/api/v1/events",
        json={"animal_id": pet_id, "type": "check", "severity": "info"},
        headers=_headers("admin", "admin-1"),
    )
    assert event_resp.status_code == 200
    event_id = event_resp.json()["event_id"]

    clip_resp = client.post(
        "/api/v1/clips",
        json={"event_id": event_id, "path": f"storage/clips/{suffix}.mp4"},
        headers=_headers("admin", "admin-1"),
    )
    assert clip_resp.status_code == 200

    dashboard_resp = client.get(
        "/api/v1/owner/dashboard",
        params={"owner_id": owner_id},
        headers=_headers("owner", owner_id),
    )
    assert dashboard_resp.status_code == 200
    dashboard = dashboard_resp.json()
    assert dashboard["active_booking_count"] >= 1
    assert any(row["booking_id"] == booking_id for row in dashboard["cards"])

    report_resp = client.get(
        f"/api/v1/reports/bookings/{booking_id}",
        headers=_headers("owner", owner_id),
    )
    assert report_resp.status_code == 200
    report = report_resp.json()
    assert report["summary"]["care_log_count"] >= 1
    assert report["summary"]["clip_count"] >= 1
    assert report["clips"][0]["event_type"] == "check"

    playback_resp = client.get(
        f"/api/v1/clips/{report['clips'][0]['clip_id']}/playback-url",
        headers=_headers("owner", owner_id),
    )
    assert playback_resp.status_code == 200
    pb = playback_resp.json()
    assert pb["clip_id"] == report["clips"][0]["clip_id"]


def test_camera_playback_url_endpoint() -> None:
    suffix = str(uuid.uuid4())[:8]
    cam_resp = client.post(
        "/api/v1/cameras",
        json={"location_zone": f"S1-ROOM-{suffix}"},
        headers=_headers("admin", "admin-1"),
    )
    assert cam_resp.status_code == 200
    cam_id = cam_resp.json()["camera_id"]

    playback_resp = client.get(
        f"/api/v1/live/cameras/{cam_id}/playback-url",
        headers=_headers("staff", "staff-1"),
    )
    assert playback_resp.status_code == 200
    body = playback_resp.json()
    assert body["camera_id"] == cam_id
    assert body["playback_url"].endswith(f"/{cam_id}")


def test_live_zone_summary_endpoint() -> None:
    suffix = str(uuid.uuid4())[:8]
    owner_id = f"owner-{suffix}"

    animal_resp = client.post(
        "/api/v1/animals",
        json={"species": "dog", "name": f"Milo-{suffix}", "owner_id": owner_id},
        headers=_headers("admin", "admin-1"),
    )
    assert animal_resp.status_code == 200
    pet_id = animal_resp.json()["animal_id"]

    room_zone = f"S1-ROOM-{suffix}"
    play_zone = f"S1-PLAY-{suffix}"
    cam_room = client.post("/api/v1/cameras", json={"location_zone": room_zone}, headers=_headers("admin", "admin-1"))
    cam_play = client.post("/api/v1/cameras", json={"location_zone": play_zone}, headers=_headers("admin", "admin-1"))
    assert cam_room.status_code == 200
    assert cam_play.status_code == 200
    cam_room_id = cam_room.json()["camera_id"]
    cam_play_id = cam_play.json()["camera_id"]

    track_room = client.post("/api/v1/tracks", json={"camera_id": cam_room_id}, headers=_headers("admin", "admin-1"))
    track_play = client.post("/api/v1/tracks", json={"camera_id": cam_play_id}, headers=_headers("admin", "admin-1"))
    assert track_room.status_code == 200
    assert track_play.status_code == 200
    room_track_id = track_room.json()["track_id"]
    play_track_id = track_play.json()["track_id"]

    obs_room_1 = client.post(
        f"/api/v1/tracks/{room_track_id}/observations",
        json={"bbox": "[100,120,220,260]", "ts": datetime.now(timezone.utc).isoformat()},
        headers=_headers("admin", "admin-1"),
    )
    obs_room_2 = client.post(
        f"/api/v1/tracks/{room_track_id}/observations",
        json={"bbox": "[110,125,230,265]", "ts": datetime.now(timezone.utc).isoformat()},
        headers=_headers("admin", "admin-1"),
    )
    obs_play = client.post(
        f"/api/v1/tracks/{play_track_id}/observations",
        json={"bbox": "[10,20,80,120]", "ts": datetime.now(timezone.utc).isoformat()},
        headers=_headers("admin", "admin-1"),
    )
    assert obs_room_1.status_code == 200
    assert obs_room_2.status_code == 200
    assert obs_play.status_code == 200

    assoc_resp = client.post(
        "/api/v1/associations",
        json={
            "global_track_id": f"global-{suffix}",
            "track_id": room_track_id,
            "animal_id": pet_id,
            "confidence": 0.95,
        },
        headers=_headers("admin", "admin-1"),
    )
    assert assoc_resp.status_code == 200

    summary_resp = client.get(
        "/api/v1/live/zones/summary?window_seconds=60",
        headers=_headers("staff", "staff-1"),
    )
    assert summary_resp.status_code == 200
    body = summary_resp.json()
    assert body["zone_count"] >= 2
    assert body["total_observations"] >= 3
    room_rows = [z for z in body["zones"] if z["zone_id"] == room_zone]
    assert room_rows
    assert room_rows[0]["observation_count"] >= 2
    assert room_rows[0]["track_count"] == 1
    assert room_rows[0]["animal_count"] == 1


def test_live_zone_heatmap_endpoint() -> None:
    suffix = str(uuid.uuid4())[:8]
    owner_id = f"owner-{suffix}"
    animal_resp = client.post(
        "/api/v1/animals",
        json={"species": "dog", "name": f"Coco-{suffix}", "owner_id": owner_id},
        headers=_headers("admin", "admin-1"),
    )
    assert animal_resp.status_code == 200

    zone = f"S1-ROOM-{suffix}"
    cam_resp = client.post(
        "/api/v1/cameras",
        json={"location_zone": zone},
        headers=_headers("admin", "admin-1"),
    )
    assert cam_resp.status_code == 200
    cam_id = cam_resp.json()["camera_id"]

    track_resp = client.post("/api/v1/tracks", json={"camera_id": cam_id}, headers=_headers("admin", "admin-1"))
    assert track_resp.status_code == 200
    track_id = track_resp.json()["track_id"]

    now = datetime.now(timezone.utc)
    for sec in [2, 8, 14, 21]:
        obs_resp = client.post(
            f"/api/v1/tracks/{track_id}/observations",
            json={"bbox": "[1,2,30,40]", "ts": (now - timedelta(seconds=sec)).isoformat()},
            headers=_headers("admin", "admin-1"),
        )
        assert obs_resp.status_code == 200

    heatmap_resp = client.get(
        "/api/v1/live/zones/heatmap?window_seconds=30&bucket_seconds=5",
        headers=_headers("staff", "staff-1"),
    )
    assert heatmap_resp.status_code == 200
    body = heatmap_resp.json()
    assert body["bucket_count"] == 6
    zones = [z for z in body["zones"] if z["zone_id"] == zone]
    assert zones
    assert len(zones[0]["counts"]) == body["bucket_count"]
    assert zones[0]["total_observations"] >= 4


def test_stream_verify_enforces_session_limit() -> None:
    suffix = str(uuid.uuid4())[:8]
    owner_id = f"owner-{suffix}"

    pet_resp = client.post(
        "/api/v1/animals",
        json={"species": "dog", "name": f"Buddy-{suffix}", "owner_id": owner_id},
        headers=_headers("admin", "admin-1"),
    )
    assert pet_resp.status_code == 200
    pet_id = pet_resp.json()["animal_id"]
    cam_resp = client.post(
        "/api/v1/cameras",
        json={"location_zone": f"S1-ROOM-{suffix}"},
        headers=_headers("admin", "admin-1"),
    )
    cam_id = cam_resp.json()["camera_id"]
    booking_resp = client.post(
        "/api/v1/bookings",
        json={
            "owner_id": owner_id,
            "pet_id": pet_id,
            "start_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            "end_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "room_zone_id": f"S1-ROOM-{suffix}",
            "status": "checked_in",
        },
        headers=_headers("admin", "admin-1"),
    )
    booking_id = booking_resp.json()["booking_id"]
    token_resp = client.post(
        "/api/v1/auth/stream-token",
        json={"owner_id": owner_id, "booking_id": booking_id, "pet_id": pet_id, "max_sessions": 1},
        headers=_headers("owner", owner_id),
    )
    assert token_resp.status_code == 200
    token = token_resp.json()["token"]

    v1 = client.post(
        "/api/v1/auth/stream-verify",
        json={"token": token, "cam_id": cam_id, "viewer_session_id": "device-a"},
        headers=_headers("system"),
    )
    assert v1.status_code == 200
    assert v1.json()["active_sessions"] == 1

    v2 = client.post(
        "/api/v1/auth/stream-verify",
        json={"token": token, "cam_id": cam_id, "viewer_session_id": "device-b"},
        headers=_headers("system"),
    )
    assert v2.status_code == 403

    close_resp = client.post(
        "/api/v1/auth/stream-session/close",
        json={"token": token, "cam_id": cam_id, "viewer_session_id": "device-a"},
        headers=_headers("system"),
    )
    assert close_resp.status_code == 200
    assert close_resp.json()["closed"] is True

    v3 = client.post(
        "/api/v1/auth/stream-verify",
        json={"token": token, "cam_id": cam_id, "viewer_session_id": "device-b"},
        headers=_headers("system"),
    )
    assert v3.status_code == 200


def test_system_ingest_and_alert_evaluate_flow() -> None:
    suffix = str(uuid.uuid4())[:8]
    owner_id = f"owner-{suffix}"
    pet_resp = client.post(
        "/api/v1/animals",
        json={"species": "cat", "name": f"Nabi-{suffix}", "owner_id": owner_id},
        headers=_headers("admin", "admin-1"),
    )
    assert pet_resp.status_code == 200
    pet_id = pet_resp.json()["animal_id"]

    zone_id = f"S1-ROOM-{suffix}"
    cam_resp = client.post(
        "/api/v1/cameras",
        json={"location_zone": zone_id},
        headers=_headers("admin", "admin-1"),
    )
    assert cam_resp.status_code == 200
    cam_id = cam_resp.json()["camera_id"]

    ingest_resp = client.post(
        "/api/v1/system/live-tracks/ingest",
        json={
            "camera_id": cam_id,
            "detections": [
                {
                    "source_track_id": 7,
                    "bbox_xyxy": [12.0, 20.0, 80.0, 120.0],
                    "conf": 0.88,
                    "animal_id": pet_id,
                }
            ],
        },
        headers=_headers("system"),
    )
    assert ingest_resp.status_code == 200
    assert ingest_resp.json()["created_observations"] >= 1

    booking_resp = client.post(
        "/api/v1/bookings",
        json={
            "owner_id": owner_id,
            "pet_id": pet_id,
            "start_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
            "end_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "room_zone_id": zone_id,
            "status": "checked_in",
        },
        headers=_headers("admin", "admin-1"),
    )
    assert booking_resp.status_code == 200

    health_resp = client.post(
        "/api/v1/system/camera-health",
        json={"camera_id": cam_id, "status": "down", "reconnect_count": 3},
        headers=_headers("system"),
    )
    assert health_resp.status_code == 200

    eval_resp = client.post("/api/v1/system/alerts/evaluate", headers=_headers("system"))
    assert eval_resp.status_code == 200

    alerts_resp = client.get("/api/v1/staff/alerts?status=open", headers=_headers("staff", "staff-1"))
    assert alerts_resp.status_code == 200
    alerts = alerts_resp.json()
    assert any(row["camera_id"] == cam_id for row in alerts)


def test_system_auto_generate_clips() -> None:
    suffix = str(uuid.uuid4())[:8]
    owner_id = f"owner-{suffix}"
    pet_resp = client.post(
        "/api/v1/animals",
        json={"species": "dog", "name": f"Milo-{suffix}", "owner_id": owner_id},
        headers=_headers("admin", "admin-1"),
    )
    assert pet_resp.status_code == 200
    pet_id = pet_resp.json()["animal_id"]

    cam_resp = client.post(
        "/api/v1/cameras",
        json={"location_zone": f"S1-PLAY-{suffix}"},
        headers=_headers("admin", "admin-1"),
    )
    assert cam_resp.status_code == 200
    cam_id = cam_resp.json()["camera_id"]

    track_resp = client.post("/api/v1/tracks", json={"camera_id": cam_id}, headers=_headers("admin", "admin-1"))
    assert track_resp.status_code == 200
    track_id = track_resp.json()["track_id"]

    seg_resp = client.post(
        "/api/v1/media-segments",
        json={
            "camera_id": cam_id,
            "path": f"storage/uploads/segments/{suffix}.mp4",
            "start_ts": (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat(),
            "end_ts": (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat(),
        },
        headers=_headers("admin", "admin-1"),
    )
    assert seg_resp.status_code == 200

    obs_resp = client.post(
        f"/api/v1/tracks/{track_id}/observations",
        json={"bbox": "[11,22,120,180]", "ts": datetime.now(timezone.utc).isoformat()},
        headers=_headers("admin", "admin-1"),
    )
    assert obs_resp.status_code == 200

    assoc_resp = client.post(
        "/api/v1/associations",
        json={
            "global_track_id": f"animal:{pet_id}",
            "track_id": track_id,
            "animal_id": pet_id,
            "confidence": 0.9,
        },
        headers=_headers("admin", "admin-1"),
    )
    assert assoc_resp.status_code == 200

    auto_resp = client.post(
        "/api/v1/system/clips/auto-generate?window_seconds=300&max_clips=3",
        headers=_headers("system"),
    )
    assert auto_resp.status_code == 200
    body = auto_resp.json()
    assert body["created_count"] >= 1
    assert len(body["clips"]) >= 1
    assert body["clips"][0]["animal_id"] == pet_id


def test_staff_alerts_websocket_stream() -> None:
    suffix = str(uuid.uuid4())[:8]
    cam_resp = client.post(
        "/api/v1/cameras",
        json={"location_zone": f"S1-ROOM-{suffix}"},
        headers=_headers("admin", "admin-1"),
    )
    cam_id = cam_resp.json()["camera_id"]
    health_resp = client.post(
        "/api/v1/system/camera-health",
        json={"camera_id": cam_id, "status": "down", "reconnect_count": 1},
        headers=_headers("system"),
    )
    assert health_resp.status_code == 200
    eval_resp = client.post("/api/v1/system/alerts/evaluate", headers=_headers("system"))
    assert eval_resp.status_code == 200

    with client.websocket_connect(
        f"/api/v1/ws/staff-alerts?api_key=change-me&role=staff&user_id=staff-1&interval_ms=500"
    ) as ws:
        payload = ws.receive_json()
        assert payload["type"] == "staff_alerts"
        assert isinstance(payload["alerts"], list)
