import json
import uuid
from argparse import Namespace
from pathlib import Path

import cv2
import numpy as np
from sqlmodel import Session, select

from app.db.models import Animal, Association, Camera, MediaSegment, Track
from app.db.session import engine, init_db
from app.workers import rtsp_tracking_worker


def _make_test_video(path: Path, width: int = 320, height: int = 240, frames: int = 24) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 12.0, (width, height))
    assert writer.isOpened()
    for idx in range(frames):
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        cv2.putText(frame, f"F{idx}", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
        writer.write(frame)
    writer.release()


class _FakeTracker:
    def __init__(self, device: str) -> None:
        self.counter = 0

    def process_frame(self, frame, conf_threshold=0.25, iou_threshold=0.45, classes=None):
        self.counter += 1
        return [
            {
                "source_track_id": 1,
                "class_id": 16,
                "conf": 0.91,
                "bbox_xyxy": [20.0, 20.0, 120.0, 140.0],
                "embedding": [0.1, 0.2, 0.3, 0.4],
            }
        ]


def test_rtsp_worker_writes_tracks_associations_and_segments(monkeypatch) -> None:
    init_db()
    suffix = str(uuid.uuid4())[:8]

    video_path = Path("storage/uploads") / f"rtsp-e2e-{suffix}.mp4"
    _make_test_video(video_path)

    camera_id = f"cam-e2e-{suffix}"
    animal_id = f"animal-e2e-{suffix}"

    with Session(engine) as session:
        session.add(Camera(camera_id=camera_id, location_zone="e2e-zone", stream_url=str(video_path)))
        session.add(Animal(animal_id=animal_id, species="dog", name="E2E"))
        session.commit()

    monkeypatch.setattr(rtsp_tracking_worker, "YoloDeepSortTracker", _FakeTracker)

    args = Namespace(
        camera_id=camera_id,
        animal_id=animal_id,
        stream_url="",
        device="cpu",
        conf_threshold=0.25,
        iou_threshold=0.45,
        classes_csv="15,16",
        frame_stride=1,
        commit_interval_frames=5,
        reconnect_retries=1,
        reconnect_delay_seconds=0.1,
        max_frames=8,
        max_seconds=0,
        global_id_mode="animal",
        reid_match_threshold=0.68,
        fallback_animal_id="system-reid-auto",
        record_segments=True,
        record_dir="storage/uploads/segments-test",
        segment_seconds=2,
        record_codec="mp4v",
    )

    rtsp_tracking_worker.run(args)

    with Session(engine) as session:
        tracks = list(session.exec(select(Track).where(Track.camera_id == camera_id)))
        assert len(tracks) >= 1

        assocs = list(
            session.exec(
                select(Association)
                .where(Association.track_id == tracks[0].track_id)
                .where(Association.animal_id == animal_id)
            )
        )
        assert len(assocs) >= 1
        assert assocs[0].global_track_id == f"animal:{animal_id}"

        segments = list(session.exec(select(MediaSegment).where(MediaSegment.camera_id == camera_id)))
        assert len(segments) >= 1
        assert Path(segments[0].path).exists()

        # observation payload shape sanity via encoded metadata path in DB relationships
        assert tracks[0].quality_score is not None
