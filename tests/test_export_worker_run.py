import json
import uuid
from argparse import Namespace
from datetime import timedelta
from pathlib import Path

from sqlmodel import Session, select

from app.db.models import Animal, Association, Camera, ExportJob, MediaSegment, Track, utcnow
from app.db.session import engine, init_db
from app.workers import export_job_worker


def _seed_job(mode: str = "highlights") -> str:
    suffix = str(uuid.uuid4())[:8]
    global_track_id = f"runjob-{suffix}"
    now = utcnow()

    with Session(engine) as session:
        animal = Animal(animal_id=f"animal-run-{suffix}", species="dog", name="RunJob")
        camera = Camera(camera_id=f"cam-run-{suffix}", location_zone="run-zone")
        session.add(animal)
        session.add(camera)
        session.flush()

        track = Track(
            camera_id=camera.camera_id,
            start_ts=now - timedelta(seconds=30),
            end_ts=now - timedelta(seconds=5),
            quality_score=0.9,
        )
        session.add(track)
        session.flush()

        session.add(
            Association(
                global_track_id=global_track_id,
                track_id=track.track_id,
                animal_id=animal.animal_id,
                confidence=0.9,
            )
        )

        seg = Path("storage/uploads") / f"runjob-{suffix}.mp4"
        seg.parent.mkdir(parents=True, exist_ok=True)
        seg.write_bytes(b"x")
        session.add(
            MediaSegment(
                camera_id=camera.camera_id,
                start_ts=now - timedelta(seconds=60),
                end_ts=now + timedelta(seconds=30),
                path=str(seg),
                codec="video/mp4",
            )
        )

        payload = {
            "padding_seconds": 1.0,
            "merge_gap_seconds": 0.2,
            "min_duration_seconds": 0.1,
            "target_seconds": 8.0,
            "per_clip_seconds": 2.0,
            "render_video": False,
            "max_retries": 0,
        }
        job = ExportJob(
            global_track_id=global_track_id,
            mode=mode,
            status="pending",
            payload_json=json.dumps(payload, ensure_ascii=True),
        )
        session.add(job)
        session.commit()
        return job.job_id


def _mark_existing_pending_done() -> None:
    with Session(engine) as session:
        jobs = list(session.exec(select(ExportJob).where(ExportJob.status == "pending")))
        for job in jobs:
            job.status = "done"
            session.add(job)
        session.commit()


def test_run_once_processes_single_pending_job() -> None:
    init_db()
    _mark_existing_pending_done()
    job_id = _seed_job(mode="highlights")

    export_job_worker.run(Namespace(once=True, poll_seconds=0.1))

    with Session(engine) as session:
        job = session.get(ExportJob, job_id)
        assert job is not None
        assert job.status == "done"
        assert job.export_id is not None
        assert job.manifest_path is not None


def test_run_once_marks_failed_for_invalid_job() -> None:
    init_db()
    _mark_existing_pending_done()
    with Session(engine) as session:
        job = ExportJob(
            global_track_id=f"missing-{uuid.uuid4()}",
            mode="full",
            status="pending",
            payload_json=json.dumps({"padding_seconds": 1.0, "render_video": False}, ensure_ascii=True),
            max_retries=0,
        )
        session.add(job)
        session.commit()
        job_id = job.job_id

    export_job_worker.run(Namespace(once=True, poll_seconds=0.1))

    with Session(engine) as session:
        job = session.get(ExportJob, job_id)
        assert job is not None
        assert job.status == "failed"
        assert job.error_message


def test_run_once_requeues_with_backoff_when_retry_available() -> None:
    init_db()
    _mark_existing_pending_done()
    with Session(engine) as session:
        job = ExportJob(
            global_track_id=f"missing-retry-{uuid.uuid4()}",
            mode="full",
            status="pending",
            payload_json=json.dumps({"padding_seconds": 1.0, "render_video": False}, ensure_ascii=True),
            max_retries=2,
        )
        session.add(job)
        session.commit()
        job_id = job.job_id

    export_job_worker.run(Namespace(once=True, poll_seconds=0.1))

    with Session(engine) as session:
        job = session.get(ExportJob, job_id)
        assert job is not None
        assert job.status == "pending"
        assert job.retry_count == 1
        assert job.next_run_at is not None
        assert job.error_message


def test_run_once_marks_failed_when_timeout(monkeypatch) -> None:
    init_db()
    _mark_existing_pending_done()
    job_id = _seed_job(mode="full")

    with Session(engine) as session:
        job = session.get(ExportJob, job_id)
        assert job is not None
        payload = json.loads(job.payload_json)
        payload["render_video"] = True
        payload["timeout_seconds"] = 0.01
        job.payload_json = json.dumps(payload, ensure_ascii=True)
        job.max_retries = 0
        session.add(job)
        session.commit()

    def _timeout(*args, **kwargs):
        raise TimeoutError("forced timeout")

    monkeypatch.setattr(export_job_worker, "render_export_video", _timeout)

    export_job_worker.run(Namespace(once=True, poll_seconds=0.1))

    with Session(engine) as session:
        job = session.get(ExportJob, job_id)
        assert job is not None
        assert job.status == "failed"
        assert "timeout" in (job.error_message or "").lower()


def test_run_once_skips_canceled_job() -> None:
    init_db()
    with Session(engine) as session:
        job = ExportJob(
            global_track_id=f"canceled-{uuid.uuid4()}",
            mode="full",
            status="pending",
            payload_json=json.dumps({"padding_seconds": 1.0, "render_video": False}, ensure_ascii=True),
            canceled_at=utcnow(),
        )
        session.add(job)
        session.commit()
        job_id = job.job_id

    export_job_worker.run(Namespace(once=True, poll_seconds=0.1))

    with Session(engine) as session:
        job = session.get(ExportJob, job_id)
        assert job is not None
        assert job.status == "pending"
        assert job.started_at is None


def test_run_once_skips_future_next_run_job() -> None:
    init_db()
    with Session(engine) as session:
        job = ExportJob(
            global_track_id=f"future-{uuid.uuid4()}",
            mode="full",
            status="pending",
            payload_json=json.dumps({"padding_seconds": 1.0, "render_video": False}, ensure_ascii=True),
            next_run_at=utcnow() + timedelta(minutes=5),
        )
        session.add(job)
        session.commit()
        job_id = job.job_id

    export_job_worker.run(Namespace(once=True, poll_seconds=0.1))

    with Session(engine) as session:
        job = session.get(ExportJob, job_id)
        assert job is not None
        assert job.status == "pending"
        assert job.started_at is None
