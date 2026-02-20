import argparse
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from sqlmodel import Session, select

from app.core.config import settings
from app.db.models import Animal, Association, Camera, GlobalIdentity, GlobalTrackProfile, Track, TrackObservation, utcnow
from app.db.models import MediaSegment
from app.db.session import engine
from app.services.tracking_service import YoloDeepSortTracker


@dataclass
class ActiveTrack:
    track_id: str
    track_row: Track
    last_seen_frame: int
    observation_count: int = 0
    association_created: bool = False


@dataclass
class SegmentRecorder:
    camera_id: str
    base_dir: Path
    segment_seconds: int
    fps: float
    frame_size: tuple[int, int]
    codec: str = "mp4v"
    writer: cv2.VideoWriter | None = None
    segment_path: Path | None = None
    segment_start: object | None = None

    def _build_path(self, now) -> Path:
        day = now.strftime("%Y%m%d")
        hour = now.strftime("%H")
        root = self.base_dir / self.camera_id / day / hour
        root.mkdir(parents=True, exist_ok=True)
        filename = now.strftime("%Y%m%dT%H%M%S") + ".mp4"
        return root / filename

    def _open_writer(self, now) -> None:
        self.segment_path = self._build_path(now)
        fourcc = cv2.VideoWriter_fourcc(*self.codec)
        self.writer = cv2.VideoWriter(
            str(self.segment_path),
            fourcc,
            max(self.fps, 1.0),
            self.frame_size,
        )
        self.segment_start = now

    def write(self, frame, now):
        if self.writer is None:
            self._open_writer(now)
        assert self.writer is not None
        self.writer.write(frame)

        elapsed = (now - self.segment_start).total_seconds() if self.segment_start else 0.0
        if elapsed >= self.segment_seconds:
            return self.flush(now)
        return None

    def flush(self, now):
        if self.writer is None or self.segment_path is None or self.segment_start is None:
            return None
        self.writer.release()
        finished = {
            "path": str(self.segment_path),
            "start_ts": self.segment_start,
            "end_ts": now,
        }
        self.writer = None
        self.segment_path = None
        self.segment_start = None
        return finished


def _build_global_track_id(global_id_mode: str, camera_id: str, source_track_id: int, animal_id: str) -> str:
    if global_id_mode == "animal" and animal_id:
        return f"animal:{animal_id}"
    if global_id_mode == "reid_auto":
        return f"camera:{camera_id}:{source_track_id}"
    return f"{camera_id}:{source_track_id}"


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom <= 0.0:
        return -1.0
    return float(np.dot(a, b) / denom)


def _parse_embedding(raw: str) -> np.ndarray | None:
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, list) or not data:
        return None
    return np.asarray([float(v) for v in data], dtype=np.float32)


def _find_or_create_reid_global_id(
    session: Session,
    class_id: int,
    embedding: list[float],
    match_threshold: float,
) -> str:
    target = np.asarray(embedding, dtype=np.float32)
    best_id = ""
    best_score = -1.0

    profiles = list(session.exec(select(GlobalTrackProfile).where(GlobalTrackProfile.class_id == class_id)))
    for profile in profiles:
        profile_vec = _parse_embedding(profile.embedding_json)
        if profile_vec is None or profile_vec.shape != target.shape:
            continue
        score = _cosine_similarity(target, profile_vec)
        if score > best_score:
            best_score = score
            best_id = profile.global_track_id

    if best_id and best_score >= match_threshold:
        profile = session.get(GlobalTrackProfile, best_id)
        if profile:
            old_vec = _parse_embedding(profile.embedding_json)
            if old_vec is not None and old_vec.shape == target.shape:
                count = max(int(profile.sample_count), 1)
                updated = (old_vec * count + target) / float(count + 1)
                profile.embedding_json = json.dumps([float(v) for v in updated.tolist()], ensure_ascii=True)
                profile.sample_count = count + 1
                profile.updated_at = utcnow()
        return best_id

    global_track_id = f"reid:{uuid.uuid4()}"
    row = GlobalTrackProfile(
        global_track_id=global_track_id,
        class_id=class_id,
        embedding_json=json.dumps([float(v) for v in target.tolist()], ensure_ascii=True),
        sample_count=1,
        updated_at=utcnow(),
    )
    session.add(row)
    return global_track_id


def _ensure_animal_exists(session: Session, animal_id: str) -> None:
    if session.get(Animal, animal_id):
        return
    row = Animal(
        animal_id=animal_id,
        species="unknown",
        name=f"Auto-{animal_id}",
        owner_id="system",
        active=True,
    )
    session.add(row)
    session.flush()


def _upsert_identity(
    session: Session,
    global_track_id: str,
    animal_id: str | None,
    source: str,
    confidence: float | None,
) -> None:
    row = session.get(GlobalIdentity, global_track_id)
    if row is None:
        row = GlobalIdentity(
            global_track_id=global_track_id,
            animal_id=animal_id,
            state="confirmed" if animal_id else "unknown",
            source=source,
            last_confidence=confidence,
            updated_at=utcnow(),
        )
        session.add(row)
        return

    # Keep confirmed mapping unless explicitly replaced by known animal.
    if animal_id:
        row.animal_id = animal_id
        row.state = "confirmed"
    elif row.animal_id is None:
        row.state = "unknown"
    row.source = source
    row.last_confidence = confidence
    row.updated_at = utcnow()
    session.add(row)


def _parse_classes(classes_csv: str) -> list[int] | None:
    raw = classes_csv.strip()
    if not raw:
        return None
    return [int(value.strip()) for value in raw.split(",") if value.strip()]


def _open_capture_with_retry(stream_url: str, retries: int, retry_delay_seconds: float) -> cv2.VideoCapture:
    last_error: str | None = None
    for _ in range(max(retries, 1)):
        cap = cv2.VideoCapture(stream_url)
        if cap.isOpened():
            return cap
        last_error = "Unable to open stream"
        cap.release()
        time.sleep(max(retry_delay_seconds, 0.1))
    raise RuntimeError(last_error or "Failed to open stream")


def run(args: argparse.Namespace) -> None:
    classes = _parse_classes(args.classes_csv)
    started_at = utcnow()
    fallback_animal_id = args.fallback_animal_id.strip() or "system-reid-auto"
    observation_stride = max(int(getattr(args, "observation_stride", 1)), 1)
    min_track_observations = max(int(getattr(args, "min_track_observations", 1)), 1)
    track_stale_frames = int(getattr(args, "track_stale_frames", 0))

    with Session(engine) as session:
        camera = session.get(Camera, args.camera_id)
        if not camera:
            raise RuntimeError(f"Camera not found: {args.camera_id}")

        stream_url = args.stream_url.strip() if args.stream_url else (camera.stream_url or "")
        if not stream_url:
            raise RuntimeError("Camera stream_url is empty. Set camera.stream_url or pass --stream-url")

        runtime = YoloDeepSortTracker(device=args.device)
        if args.global_id_mode == "reid_auto":
            _ensure_animal_exists(session, args.animal_id or fallback_animal_id)
        cap = _open_capture_with_retry(stream_url, args.reconnect_retries, args.reconnect_delay_seconds)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 15.0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

        recorder = None
        if args.record_segments:
            if width <= 0 or height <= 0:
                raise RuntimeError("Failed to determine stream frame size for recording")
            record_root = Path(args.record_dir).expanduser()
            recorder = SegmentRecorder(
                camera_id=args.camera_id,
                base_dir=record_root,
                segment_seconds=max(args.segment_seconds, 5),
                fps=max(fps, 1.0),
                frame_size=(width, height),
                codec=args.record_codec,
            )

        frame_index = 0
        processed_frames = 0
        total_tracks_written = 0
        total_observations_written = 0
        active_tracks: dict[int, ActiveTrack] = {}

        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    cap.release()
                    cap = _open_capture_with_retry(
                        stream_url,
                        args.reconnect_retries,
                        args.reconnect_delay_seconds,
                    )
                    continue

                if args.frame_stride > 1 and frame_index % args.frame_stride != 0:
                    frame_index += 1
                    continue

                now = utcnow()
                if recorder is not None:
                    finished = recorder.write(frame=frame, now=now)
                    if finished:
                        session.add(
                            MediaSegment(
                                camera_id=args.camera_id,
                                start_ts=finished["start_ts"],
                                end_ts=finished["end_ts"],
                                path=finished["path"],
                                codec="video/mp4",
                            )
                        )
                detections = runtime.process_frame(
                    frame=frame,
                    conf_threshold=args.conf_threshold,
                    iou_threshold=args.iou_threshold,
                    classes=classes,
                )

                for det in detections:
                    source_track_id = int(det["source_track_id"])
                    active = active_tracks.get(source_track_id)
                    if active is None:
                        row = Track(
                            camera_id=args.camera_id,
                            start_ts=now,
                            end_ts=now,
                            quality_score=float(det["conf"]),
                        )
                        session.add(row)
                        session.flush()
                        active = ActiveTrack(track_id=row.track_id, track_row=row, last_seen_frame=frame_index)
                        active_tracks[source_track_id] = active
                        total_tracks_written += 1

                    track_row = active.track_row
                    active.last_seen_frame = frame_index
                    track_row.end_ts = now
                    prev = float(track_row.quality_score or 0.0)
                    track_row.quality_score = round((prev + float(det["conf"])) / 2.0, 6)
                    session.add(track_row)

                    active.observation_count += 1
                    should_write_observation = (
                        active.observation_count == 1
                        or (active.observation_count % observation_stride == 0)
                    )
                    if should_write_observation:
                        obs = TrackObservation(
                            track_id=active.track_id,
                            ts=now,
                            bbox=json.dumps([round(float(v), 3) for v in det["bbox_xyxy"]], ensure_ascii=True),
                            marker_id_read=None,
                            appearance_vec_ref=(
                                f"src:{source_track_id};class:{int(det['class_id'])};conf:{float(det['conf']):.6f}"
                            ),
                        )
                        session.add(obs)
                        total_observations_written += 1

                    if (
                        args.animal_id
                        and not active.association_created
                        and active.observation_count >= min_track_observations
                    ):
                        global_track_id = _build_global_track_id(
                            args.global_id_mode,
                            args.camera_id,
                            source_track_id,
                            args.animal_id,
                        )
                        assoc = Association(
                            global_track_id=global_track_id,
                            track_id=active.track_id,
                            animal_id=args.animal_id,
                            confidence=float(det["conf"]),
                            created_at=now,
                        )
                        session.add(assoc)
                        _upsert_identity(
                            session=session,
                            global_track_id=global_track_id,
                            animal_id=args.animal_id,
                            source="manual",
                            confidence=float(det["conf"]),
                        )
                        active.association_created = True
                    elif (
                        args.global_id_mode == "reid_auto"
                        and not active.association_created
                        and active.observation_count >= min_track_observations
                    ):
                        embedding = det.get("embedding")
                        if embedding:
                            global_track_id = _find_or_create_reid_global_id(
                                session=session,
                                class_id=int(det["class_id"]),
                                embedding=embedding,
                                match_threshold=args.reid_match_threshold,
                            )
                        else:
                            global_track_id = f"camera:{args.camera_id}:{source_track_id}"

                        assoc = Association(
                            global_track_id=global_track_id,
                            track_id=active.track_id,
                            animal_id=args.animal_id or fallback_animal_id,
                            confidence=float(det["conf"]),
                            created_at=now,
                        )
                        session.add(assoc)
                        _upsert_identity(
                            session=session,
                            global_track_id=global_track_id,
                            animal_id=args.animal_id if args.animal_id else None,
                            source="reid_auto",
                            confidence=float(det["conf"]),
                        )
                        active.association_created = True

                if track_stale_frames > 0:
                    stale_ids = [
                        source_id
                        for source_id, active in active_tracks.items()
                        if (frame_index - active.last_seen_frame) > track_stale_frames
                    ]
                    for source_id in stale_ids:
                        active_tracks.pop(source_id, None)

                frame_index += 1
                processed_frames += 1

                if processed_frames % max(args.commit_interval_frames, 1) == 0:
                    session.commit()

                if args.max_frames > 0 and processed_frames >= args.max_frames:
                    break

                if args.max_seconds > 0:
                    elapsed = (utcnow() - started_at).total_seconds()
                    if elapsed >= args.max_seconds:
                        break
        except KeyboardInterrupt:
            pass
        finally:
            if recorder is not None:
                finished = recorder.flush(utcnow())
                if finished:
                    session.add(
                        MediaSegment(
                            camera_id=args.camera_id,
                            start_ts=finished["start_ts"],
                            end_ts=finished["end_ts"],
                            path=finished["path"],
                            codec="video/mp4",
                        )
                    )
            session.commit()
            cap.release()

    print(
        json.dumps(
            {
                "camera_id": args.camera_id,
                "animal_id": args.animal_id,
                "processed_frames": processed_frames,
                "tracks_written": total_tracks_written,
                "observations_written": total_observations_written,
            },
            ensure_ascii=True,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RTSP YOLO+DeepSort tracking worker")
    parser.add_argument("--camera-id", required=True, help="Camera ID in DB")
    parser.add_argument("--animal-id", default="", help="Optional known animal_id for association")
    parser.add_argument("--stream-url", default="", help="Optional RTSP URL override")
    parser.add_argument("--device", default="cuda:0", help="Inference device, e.g. cuda:0 or cpu")
    parser.add_argument("--conf-threshold", type=float, default=0.25)
    parser.add_argument("--iou-threshold", type=float, default=0.45)
    parser.add_argument("--classes-csv", default="15,16", help="COCO class IDs, default cat/dog")
    parser.add_argument("--frame-stride", type=int, default=1)
    parser.add_argument(
        "--observation-stride",
        type=int,
        default=1,
        help="Persist one observation every N matched detections per track.",
    )
    parser.add_argument(
        "--min-track-observations",
        type=int,
        default=2,
        help="Create first association only after this many observations for the track.",
    )
    parser.add_argument(
        "--track-stale-frames",
        type=int,
        default=90,
        help="Drop active track state if not seen for this many processed frames (0 disables).",
    )
    parser.add_argument("--commit-interval-frames", type=int, default=30)
    parser.add_argument("--reconnect-retries", type=int, default=20)
    parser.add_argument("--reconnect-delay-seconds", type=float, default=2.0)
    parser.add_argument("--max-frames", type=int, default=0)
    parser.add_argument("--max-seconds", type=int, default=0)
    parser.add_argument("--record-segments", action="store_true", help="Record raw stream into rotating local mp4 segments")
    parser.add_argument("--record-dir", default=str(settings.upload_dir / "segments"), help="Base directory for recorded segments")
    parser.add_argument("--segment-seconds", type=int, default=20, help="Duration of each recorded segment")
    parser.add_argument("--record-codec", default="mp4v", help="OpenCV fourcc codec for recording")
    parser.add_argument(
        "--global-id-mode",
        choices=["animal", "camera_track", "reid_auto"],
        default="animal",
        help="Association global_track_id mode. 'animal' merges by animal_id across cameras.",
    )
    parser.add_argument(
        "--reid-match-threshold",
        type=float,
        default=0.68,
        help="Cosine similarity threshold for reid_auto global ID assignment.",
    )
    parser.add_argument(
        "--fallback-animal-id",
        default="",
        help="When reid_auto is used without animal_id, this value is stored in associations.animal_id.",
    )
    return parser


if __name__ == "__main__":
    run(build_parser().parse_args())
