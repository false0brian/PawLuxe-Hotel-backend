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
