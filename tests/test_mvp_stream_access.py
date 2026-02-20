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


def test_owner_stream_token_flow_with_zone_move() -> None:
    suffix = str(uuid.uuid4())[:8]
    owner_id = f"owner-{suffix}"

    animal_resp = client.post(
        "/api/v1/animals",
        json={"species": "dog", "name": f"Milo-{suffix}", "owner_id": owner_id},
        headers=_headers("owner", owner_id),
    )
    assert animal_resp.status_code == 200
    pet_id = animal_resp.json()["animal_id"]

    room_zone = f"S1-ROOM-{suffix}"
    isolation_zone = f"S1-ISOLATION-{suffix}"

    room_cam_resp = client.post(
        "/api/v1/cameras",
        json={"location_zone": room_zone, "stream_url": "rtsp://room"},
        headers=_headers("admin", "admin-1"),
    )
    assert room_cam_resp.status_code == 200

    isolation_cam_resp = client.post(
        "/api/v1/cameras",
        json={"location_zone": isolation_zone, "stream_url": "rtsp://isolation"},
        headers=_headers("admin", "admin-1"),
    )
    assert isolation_cam_resp.status_code == 200
    isolation_cam_id = isolation_cam_resp.json()["camera_id"]

    booking_resp = client.post(
        "/api/v1/bookings",
        json={
            "owner_id": owner_id,
            "pet_id": pet_id,
            "start_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            "end_at": (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat(),
            "room_zone_id": room_zone,
            "status": "checked_in",
        },
        headers=_headers("admin", "admin-1"),
    )
    assert booking_resp.status_code == 200
    booking_id = booking_resp.json()["booking_id"]

    status_before = client.get(
        f"/api/v1/pets/{pet_id}/status",
        params={"owner_id": owner_id},
        headers=_headers("owner", owner_id),
    )
    assert status_before.status_code == 200
    assert status_before.json()["current_zone_id"] == room_zone

    move_resp = client.post(
        "/api/v1/staff/move-zone",
        json={"pet_id": pet_id, "to_zone_id": isolation_zone, "by_staff_id": f"staff-{suffix}"},
        headers=_headers("staff", f"staff-{suffix}"),
    )
    assert move_resp.status_code == 200

    log_resp = client.post(
        "/api/v1/staff/logs",
        json={
            "pet_id": pet_id,
            "booking_id": booking_id,
            "type": "feeding",
            "value": "ate 80%",
            "staff_id": f"staff-{suffix}",
        },
        headers=_headers("staff", f"staff-{suffix}"),
    )
    assert log_resp.status_code == 200

    status_after = client.get(
        f"/api/v1/pets/{pet_id}/status",
        params={"owner_id": owner_id},
        headers=_headers("owner", owner_id),
    )
    assert status_after.status_code == 200
    assert status_after.json()["current_zone_id"] == isolation_zone
    assert isolation_cam_id in status_after.json()["cam_ids"]
    assert status_after.json()["last_care_log"]["type"] == "feeding"

    token_resp = client.post(
        "/api/v1/auth/stream-token",
        json={
            "owner_id": owner_id,
            "booking_id": booking_id,
            "pet_id": pet_id,
            "max_sessions": 2,
            "ttl_seconds": 120,
        },
        headers=_headers("owner", owner_id),
    )
    assert token_resp.status_code == 200
    body = token_resp.json()
    assert body["zone_id"] == isolation_zone
    assert isolation_cam_id in body["cam_ids"]
    assert len(body["stream_urls"]) >= 1


def test_stream_token_rejects_wrong_owner() -> None:
    suffix = str(uuid.uuid4())[:8]

    animal_resp = client.post(
        "/api/v1/animals",
        json={"species": "cat", "name": f"Nabi-{suffix}", "owner_id": f"owner-{suffix}"},
        headers=_headers("admin", "admin-1"),
    )
    pet_id = animal_resp.json()["animal_id"]

    camera_resp = client.post(
        "/api/v1/cameras",
        json={"location_zone": f"S1-ROOM-{suffix}"},
        headers=_headers("admin", "admin-1"),
    )
    assert camera_resp.status_code == 200

    booking_resp = client.post(
        "/api/v1/bookings",
        json={
            "owner_id": f"owner-{suffix}",
            "pet_id": pet_id,
            "start_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
            "end_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "room_zone_id": f"S1-ROOM-{suffix}",
            "status": "checked_in",
        },
        headers=_headers("admin", "admin-1"),
    )
    booking_id = booking_resp.json()["booking_id"]

    token_resp = client.post(
        "/api/v1/auth/stream-token",
        json={
            "owner_id": f"owner-{suffix}",
            "booking_id": booking_id,
            "pet_id": pet_id,
        },
        headers=_headers("owner", "owner-wrong"),
    )
    assert token_resp.status_code == 403


def test_play_live_blocked_for_owner_but_allowed_for_admin() -> None:
    suffix = str(uuid.uuid4())[:8]
    owner_id = f"owner-{suffix}"

    animal_resp = client.post(
        "/api/v1/animals",
        json={"species": "dog", "name": f"Coco-{suffix}", "owner_id": owner_id},
        headers=_headers("admin", "admin-1"),
    )
    pet_id = animal_resp.json()["animal_id"]

    room_zone = f"S1-ROOM-{suffix}"
    play_zone = f"S1-PLAY-{suffix}"

    client.post("/api/v1/cameras", json={"location_zone": room_zone}, headers=_headers("admin", "admin-1"))
    play_cam_resp = client.post(
        "/api/v1/cameras", json={"location_zone": play_zone}, headers=_headers("admin", "admin-1")
    )
    play_cam_id = play_cam_resp.json()["camera_id"]

    booking_resp = client.post(
        "/api/v1/bookings",
        json={
            "owner_id": owner_id,
            "pet_id": pet_id,
            "start_at": (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(),
            "end_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "room_zone_id": room_zone,
            "status": "checked_in",
        },
        headers=_headers("admin", "admin-1"),
    )
    booking_id = booking_resp.json()["booking_id"]

    move_resp = client.post(
        "/api/v1/staff/move-zone",
        json={"pet_id": pet_id, "to_zone_id": play_zone},
        headers=_headers("staff", "staff-1"),
    )
    assert move_resp.status_code == 200

    owner_token_resp = client.post(
        "/api/v1/auth/stream-token",
        json={"owner_id": owner_id, "booking_id": booking_id, "pet_id": pet_id},
        headers=_headers("owner", owner_id),
    )
    assert owner_token_resp.status_code == 403

    admin_token_resp = client.post(
        "/api/v1/auth/stream-token",
        json={"owner_id": owner_id, "booking_id": booking_id, "pet_id": pet_id},
        headers=_headers("admin", "admin-1"),
    )
    assert admin_token_resp.status_code == 200
    assert play_cam_id in admin_token_resp.json()["cam_ids"]


def test_stream_verify_requires_system_or_admin() -> None:
    suffix = str(uuid.uuid4())[:8]
    owner_id = f"owner-{suffix}"

    animal_resp = client.post(
        "/api/v1/animals",
        json={"species": "dog", "name": f"Leo-{suffix}", "owner_id": owner_id},
        headers=_headers("admin", "admin-1"),
    )
    pet_id = animal_resp.json()["animal_id"]

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
            "start_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
            "end_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "room_zone_id": f"S1-ROOM-{suffix}",
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

    verify_fail = client.post(
        "/api/v1/auth/stream-verify",
        json={"token": token, "cam_id": cam_id},
        headers=_headers("owner", owner_id),
    )
    assert verify_fail.status_code == 403

    verify_ok = client.post(
        "/api/v1/auth/stream-verify",
        json={"token": token, "cam_id": cam_id},
        headers=_headers("system"),
    )
    assert verify_ok.status_code == 200
    assert verify_ok.json()["ok"] is True
