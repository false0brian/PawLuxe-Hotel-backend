import json
import uuid
from datetime import timedelta
from pathlib import Path

from sqlmodel import Session

from app.db.models import Animal, Association, Camera, ExportJob, MediaSegment, Track, utcnow
from app.db.session import engine, init_db
from app.workers.export_job_worker import _process_one


def _seed_track_data(global_track_id: str) -> None:
    now = utcnow()
    suffix = str(uuid.uuid4())[:8]

    with Session(engine) as session:
        animal = Animal(animal_id=f"animal-{suffix}", species="dog", name=f"Dog-{suffix}")
        camera = Camera(camera_id=f"camera-{suffix}", location_zone=f"zone-{suffix}")
        session.add(animal)
        session.add(camera)
        session.flush()

        track = Track(
            camera_id=camera.camera_id,
            start_ts=now - timedelta(seconds=20),
            end_ts=now - timedelta(seconds=5),
            quality_score=0.9,
        )
        session.add(track)
        session.flush()

        assoc = Association(
            global_track_id=global_track_id,
            track_id=track.track_id,
            animal_id=animal.animal_id,
            confidence=0.95,
        )
        session.add(assoc)

        seg_path = Path("storage/uploads") / f"seed-{suffix}.mp4"
        seg_path.parent.mkdir(parents=True, exist_ok=True)
        seg_path.write_bytes(b"dummy")

        segment = MediaSegment(
            camera_id=camera.camera_id,
            start_ts=now - timedelta(seconds=60),
            end_ts=now + timedelta(seconds=60),
            path=str(seg_path),
            codec="video/mp4",
        )
        session.add(segment)
        session.commit()


def test_process_one_success_without_render() -> None:
    init_db()
    global_track_id = f"job-global-{uuid.uuid4()}"
    _seed_track_data(global_track_id)

    job = ExportJob(
        global_track_id=global_track_id,
        mode="highlights",
        status="pending",
        payload_json=json.dumps(
            {
                "padding_seconds": 1.0,
                "target_seconds": 8.0,
                "per_clip_seconds": 2.0,
                "render_video": False,
            },
            ensure_ascii=True,
        ),
    )

    with Session(engine) as session:
        _process_one(session, job)

    assert job.export_id is not None
    assert job.manifest_path is not None
    assert Path(job.manifest_path).exists()
    assert job.video_path is None


def test_process_one_fails_when_no_association() -> None:
    init_db()
    job = ExportJob(
        global_track_id=f"missing-{uuid.uuid4()}",
        mode="full",
        status="pending",
        payload_json=json.dumps({"padding_seconds": 1.0, "render_video": False}, ensure_ascii=True),
    )

    with Session(engine) as session:
        try:
            _process_one(session, job)
            assert False, "Expected ValueError for missing associations"
        except ValueError as exc:
            assert "No associations" in str(exc)
