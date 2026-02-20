import os
import subprocess
import sys


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def main() -> int:
    camera_id = _env("CAMERA_ID")
    if not camera_id:
        print("CAMERA_ID is required", file=sys.stderr)
        return 2

    cmd = [
        sys.executable,
        "-m",
        "app.workers.rtsp_tracking_worker",
        "--camera-id",
        camera_id,
        "--animal-id",
        _env("ANIMAL_ID"),
        "--device",
        _env("WORKER_DEVICE", _env("TRACKING_DEVICE", "cuda:0")),
        "--conf-threshold",
        _env("CONF_THRESHOLD", "0.25"),
        "--iou-threshold",
        _env("IOU_THRESHOLD", "0.45"),
        "--classes-csv",
        _env("CLASSES_CSV", "15,16"),
        "--frame-stride",
        _env("FRAME_STRIDE", "1"),
        "--commit-interval-frames",
        _env("COMMIT_INTERVAL_FRAMES", "30"),
        "--reconnect-retries",
        _env("RECONNECT_RETRIES", "20"),
        "--reconnect-delay-seconds",
        _env("RECONNECT_DELAY_SECONDS", "2.0"),
        "--global-id-mode",
        _env("GLOBAL_ID_MODE", "reid_auto"),
        "--reid-match-threshold",
        _env("REID_MATCH_THRESHOLD", "0.68"),
        "--fallback-animal-id",
        _env("FALLBACK_ANIMAL_ID", "system-reid-auto"),
        "--record-dir",
        _env("RECORD_DIR", "storage/uploads/segments"),
        "--segment-seconds",
        _env("SEGMENT_SECONDS", "20"),
        "--record-codec",
        _env("RECORD_CODEC", "mp4v"),
    ]

    if _env("RECORD_SEGMENTS", "1") == "1":
        cmd.append("--record-segments")

    stream_url = _env("STREAM_URL")
    if stream_url:
        cmd.extend(["--stream-url", stream_url])

    max_frames = _env("MAX_FRAMES")
    if max_frames:
        cmd.extend(["--max-frames", max_frames])

    max_seconds = _env("MAX_SECONDS")
    if max_seconds:
        cmd.extend(["--max-seconds", max_seconds])

    completed = subprocess.run(cmd)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
