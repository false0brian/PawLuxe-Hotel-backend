import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db.session import init_db
from app.main import app

client = TestClient(app)
HEADERS = {"x-api-key": "change-me"}

init_db()


def test_export_download_manifest_flow() -> None:
    suffix = str(uuid.uuid4())[:8]

    animal = client.post(
        "/api/v1/animals",
        json={"species": "dog", "name": f"Dog-{suffix}"},
        headers=HEADERS,
    ).json()
    camera = client.post(
        "/api/v1/cameras",
        json={"location_zone": f"z-{suffix}"},
        headers=HEADERS,
    ).json()

    track = client.post(
        "/api/v1/tracks",
        json={
            "camera_id": camera["camera_id"],
            "start_ts": (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(),
            "end_ts": (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(),
        },
        headers=HEADERS,
    ).json()

    global_track_id = f"exp-{suffix}"
    assoc_resp = client.post(
        "/api/v1/associations",
        json={
            "global_track_id": global_track_id,
            "track_id": track["track_id"],
            "animal_id": animal["animal_id"],
            "confidence": 0.9,
        },
        headers=HEADERS,
    )
    assert assoc_resp.status_code == 200

    segment_resp = client.post(
        "/api/v1/media-segments",
        json={
            "camera_id": camera["camera_id"],
            "path": f"storage/uploads/{suffix}.mp4",
            "codec": "video/mp4",
            "start_ts": (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(),
            "end_ts": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
        },
        headers=HEADERS,
    )
    assert segment_resp.status_code == 200

    export_resp = client.post(
        f"/api/v1/exports/global-track/{global_track_id}",
        json={"padding_seconds": 1.0, "render_video": False},
        headers=HEADERS,
    )
    assert export_resp.status_code == 200
    export_id = export_resp.json()["export_id"]

    meta_resp = client.get(f"/api/v1/exports/{export_id}", headers=HEADERS)
    assert meta_resp.status_code == 200
    assert meta_resp.json()["export_id"] == export_id
    assert meta_resp.json()["manifest"] is not None

    manifest_resp = client.get(
        f"/api/v1/exports/{export_id}?download=manifest",
        headers=HEADERS,
    )
    assert manifest_resp.status_code == 200
    assert manifest_resp.headers.get("content-type", "").startswith("application/json")


def test_export_download_video_404_when_not_rendered() -> None:
    resp = client.get(
        f"/api/v1/exports/{uuid.uuid4()}?download=video",
        headers=HEADERS,
    )
    assert resp.status_code == 404
