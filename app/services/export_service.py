import json
import shutil
import subprocess
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import Session, select

from app.core.config import settings
from app.db.models import Association, MediaSegment, Track


@dataclass
class ExportExcerpt:
    camera_id: str
    segment_id: str
    segment_path: str
    clip_start_ts: datetime
    clip_end_ts: datetime
    offset_start_sec: float
    duration_sec: float

    def to_dict(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "segment_id": self.segment_id,
            "segment_path": self.segment_path,
            "clip_start_ts": self.clip_start_ts.isoformat(),
            "clip_end_ts": self.clip_end_ts.isoformat(),
            "offset_start_sec": round(self.offset_start_sec, 3),
            "duration_sec": round(self.duration_sec, 3),
        }


def _ensure_export_dirs() -> tuple[Path, Path]:
    root = settings.export_dir
    manifests = root / "manifests"
    videos = root / "videos"
    manifests.mkdir(parents=True, exist_ok=True)
    videos.mkdir(parents=True, exist_ok=True)
    return manifests, videos


def _overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> tuple[datetime, datetime] | None:
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    if end <= start:
        return None
    return start, end


def _merge_and_filter_excerpts(
    excerpts: list[ExportExcerpt],
    merge_gap_seconds: float,
    min_duration_seconds: float,
) -> list[ExportExcerpt]:
    if not excerpts:
        return []

    merged: list[ExportExcerpt] = []
    gap = max(merge_gap_seconds, 0.0)
    min_dur = max(min_duration_seconds, 0.0)

    ordered = sorted(excerpts, key=lambda x: x.clip_start_ts)
    current = ordered[0]
    for nxt in ordered[1:]:
        same_segment = current.segment_path == nxt.segment_path
        contiguous = (nxt.clip_start_ts - current.clip_end_ts).total_seconds() <= gap
        if same_segment and contiguous:
            current.clip_end_ts = max(current.clip_end_ts, nxt.clip_end_ts)
            current.duration_sec = max((current.clip_end_ts - current.clip_start_ts).total_seconds(), 0.0)
            continue
        if current.duration_sec >= min_dur:
            merged.append(current)
        current = nxt
    if current.duration_sec >= min_dur:
        merged.append(current)
    return merged


def build_export_plan(
    session: Session,
    global_track_id: str,
    padding_seconds: float,
    merge_gap_seconds: float = 0.2,
    min_duration_seconds: float = 0.3,
) -> tuple[list[ExportExcerpt], dict]:
    associations = list(session.exec(select(Association).where(Association.global_track_id == global_track_id)))
    if not associations:
        raise ValueError("No associations found for global_track_id")

    pad = timedelta(seconds=max(padding_seconds, 0.0))
    excerpts: list[ExportExcerpt] = []

    for assoc in associations:
        track = session.get(Track, assoc.track_id)
        if not track:
            continue

        if not track.end_ts:
            continue

        window_start = track.start_ts - pad
        window_end = track.end_ts + pad

        segments = list(
            session.exec(
                select(MediaSegment)
                .where(MediaSegment.camera_id == track.camera_id)
                .where(MediaSegment.end_ts.is_not(None))
                .where(MediaSegment.start_ts <= window_end)
                .where(MediaSegment.end_ts >= window_start)
                .order_by(MediaSegment.start_ts)
            )
        )

        for segment in segments:
            if not segment.end_ts:
                continue
            overlap = _overlap(window_start, window_end, segment.start_ts, segment.end_ts)
            if not overlap:
                continue
            clip_start, clip_end = overlap
            duration = (clip_end - clip_start).total_seconds()
            if duration <= 0.0:
                continue

            offset = (clip_start - segment.start_ts).total_seconds()
            excerpts.append(
                ExportExcerpt(
                    camera_id=track.camera_id,
                    segment_id=segment.segment_id,
                    segment_path=segment.path,
                    clip_start_ts=clip_start,
                    clip_end_ts=clip_end,
                    offset_start_sec=max(offset, 0.0),
                    duration_sec=duration,
                )
            )

    excerpts = _merge_and_filter_excerpts(
        excerpts=excerpts,
        merge_gap_seconds=merge_gap_seconds,
        min_duration_seconds=min_duration_seconds,
    )

    summary = {
        "global_track_id": global_track_id,
        "padding_seconds": max(padding_seconds, 0.0),
        "merge_gap_seconds": max(merge_gap_seconds, 0.0),
        "min_duration_seconds": max(min_duration_seconds, 0.0),
        "association_count": len(associations),
        "excerpt_count": len(excerpts),
    }
    return excerpts, summary


def build_highlight_plan(
    excerpts: list[ExportExcerpt],
    target_seconds: float = 30.0,
    per_clip_seconds: float = 4.0,
) -> list[ExportExcerpt]:
    remaining = max(target_seconds, 0.0)
    clip_cap = max(per_clip_seconds, 0.1)
    if remaining <= 0.0 or not excerpts:
        return []

    candidates = list(excerpts)
    camera_count: dict[str, int] = defaultdict(int)
    bucket_count: dict[str, int] = defaultdict(int)
    selected: list[ExportExcerpt] = []
    while candidates and remaining > 0.0:
        best_idx = -1
        best_score = -1e9
        for idx, item in enumerate(candidates):
            bucket_key = item.clip_start_ts.strftime("%Y%m%d%H%M")
            duration_score = min(item.duration_sec, clip_cap) / clip_cap
            camera_penalty = camera_count[item.camera_id] * 0.25
            bucket_penalty = bucket_count[bucket_key] * 0.15
            score = duration_score - camera_penalty - bucket_penalty
            if score > best_score:
                best_score = score
                best_idx = idx

        if best_idx < 0:
            break
        item = candidates.pop(best_idx)
        if remaining <= 0.0:
            break
        take = min(item.duration_sec, clip_cap, remaining)
        if take <= 0.0:
            continue
        bucket_key = item.clip_start_ts.strftime("%Y%m%d%H%M")
        selected.append(
            ExportExcerpt(
                camera_id=item.camera_id,
                segment_id=item.segment_id,
                segment_path=item.segment_path,
                clip_start_ts=item.clip_start_ts,
                clip_end_ts=item.clip_start_ts + timedelta(seconds=take),
                offset_start_sec=item.offset_start_sec,
                duration_sec=take,
            )
        )
        camera_count[item.camera_id] += 1
        bucket_count[bucket_key] += 1
        remaining -= take
    selected.sort(key=lambda x: x.clip_start_ts)
    return selected


def save_manifest(global_track_id: str, summary: dict, excerpts: list[ExportExcerpt]) -> tuple[str, Path]:
    manifests_dir, _ = _ensure_export_dirs()
    export_id = str(uuid.uuid4())
    path = manifests_dir / f"{export_id}.json"

    payload = {
        "export_id": export_id,
        "global_track_id": global_track_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "excerpts": [item.to_dict() for item in excerpts],
    }
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    return export_id, path


def _run_ffmpeg(cmd: list[str], timeout_seconds: float | None = None) -> None:
    timeout = None
    if timeout_seconds is not None and timeout_seconds > 0:
        timeout = timeout_seconds
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "ffmpeg failed")


def render_export_video(
    export_id: str,
    excerpts: list[ExportExcerpt],
    ffmpeg_timeout_seconds: float | None = None,
) -> Path:
    _, videos_dir = _ensure_export_dirs()
    work_dir = videos_dir / f"{export_id}_parts"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        part_paths: list[Path] = []
        for idx, excerpt in enumerate(excerpts):
            src = Path(excerpt.segment_path)
            if not src.exists():
                continue

            part = work_dir / f"part_{idx:04d}.mp4"
            cmd = [
                settings.ffmpeg_bin,
                "-y",
                "-ss",
                f"{excerpt.offset_start_sec:.3f}",
                "-i",
                str(src),
                "-t",
                f"{excerpt.duration_sec:.3f}",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "24",
                str(part),
            ]
            _run_ffmpeg(cmd, timeout_seconds=ffmpeg_timeout_seconds)
            part_paths.append(part)

        if not part_paths:
            raise RuntimeError("No valid segment files found to render")

        concat_txt = work_dir / "concat.txt"
        concat_txt.write_text("\n".join([f"file '{p.resolve()}'" for p in part_paths]), encoding="utf-8")

        output = videos_dir / f"{export_id}.mp4"
        cmd_concat = [
            settings.ffmpeg_bin,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_txt),
            "-c",
            "copy",
            str(output),
        ]
        _run_ffmpeg(cmd_concat, timeout_seconds=ffmpeg_timeout_seconds)
        return output
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def manifest_path_for_export(export_id: str) -> Path:
    manifests_dir, _ = _ensure_export_dirs()
    return manifests_dir / f"{export_id}.json"


def video_path_for_export(export_id: str) -> Path:
    _, videos_dir = _ensure_export_dirs()
    return videos_dir / f"{export_id}.mp4"


def load_manifest(export_id: str) -> dict:
    path = manifest_path_for_export(export_id)
    if not path.exists():
        raise FileNotFoundError(str(path))
    return json.loads(path.read_text(encoding="utf-8"))
