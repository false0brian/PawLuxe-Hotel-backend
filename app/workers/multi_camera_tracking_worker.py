import argparse
import json
import signal
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path


def _parse_csv(values: str) -> list[str]:
    return [item.strip() for item in values.split(",") if item.strip()]


def _load_map(raw: str) -> dict[str, str]:
    payload = raw.strip()
    if not payload:
        return {}
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("camera_animal_map must be a JSON object")
    result: dict[str, str] = {}
    for key, value in parsed.items():
        if value is None:
            continue
        result[str(key)] = str(value)
    return result


def _start_worker(
    camera_id: str,
    animal_id: str,
    args: argparse.Namespace,
) -> subprocess.Popen:
    cmd: list[str] = [
        sys.executable,
        "-m",
        "app.workers.rtsp_tracking_worker",
        "--camera-id",
        camera_id,
        "--device",
        args.device,
        "--conf-threshold",
        str(args.conf_threshold),
        "--iou-threshold",
        str(args.iou_threshold),
        "--classes-csv",
        args.classes_csv,
        "--frame-stride",
        str(args.frame_stride),
        "--commit-interval-frames",
        str(args.commit_interval_frames),
        "--reconnect-retries",
        str(args.reconnect_retries),
        "--reconnect-delay-seconds",
        str(args.reconnect_delay_seconds),
        "--max-frames",
        str(args.max_frames),
        "--max-seconds",
        str(args.max_seconds),
        "--global-id-mode",
        args.global_id_mode,
        "--reid-match-threshold",
        str(args.reid_match_threshold),
        "--fallback-animal-id",
        args.fallback_animal_id,
    ]
    if args.record_segments:
        cmd.extend(
            [
                "--record-segments",
                "--record-dir",
                args.record_dir,
                "--segment-seconds",
                str(args.segment_seconds),
                "--record-codec",
                args.record_codec,
            ]
        )

    if animal_id:
        cmd.extend(["--animal-id", animal_id])
    if args.stream_url:
        cmd.extend(["--stream-url", args.stream_url])

    return subprocess.Popen(cmd)


def _terminate_all(processes: Iterable[subprocess.Popen]) -> None:
    for proc in processes:
        if proc.poll() is None:
            proc.terminate()


def run(args: argparse.Namespace) -> None:
    camera_ids = _parse_csv(args.camera_ids)
    if not camera_ids:
        raise RuntimeError("camera_ids is empty")

    camera_animal_map = _load_map(args.camera_animal_map)

    processes: dict[str, subprocess.Popen] = {}
    try:
        for camera_id in camera_ids:
            animal_id = camera_animal_map.get(camera_id, "")
            proc = _start_worker(camera_id=camera_id, animal_id=animal_id, args=args)
            processes[camera_id] = proc
            print(f"started camera_id={camera_id} pid={proc.pid}")

        while True:
            all_done = True
            for camera_id, proc in processes.items():
                code = proc.poll()
                if code is None:
                    all_done = False
                    continue
                if code != 0:
                    raise RuntimeError(f"worker failed for camera_id={camera_id} code={code}")
            if all_done:
                break
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        _terminate_all(processes.values())
        time.sleep(0.5)
        for proc in processes.values():
            if proc.poll() is None:
                proc.send_signal(signal.SIGKILL)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch RTSP tracking workers for multiple cameras")
    parser.add_argument("--camera-ids", required=True, help="Comma separated camera IDs")
    parser.add_argument(
        "--camera-animal-map",
        default="",
        help='JSON map, e.g. {"<camera_id_1>":"<animal_id>","<camera_id_2>":"<animal_id>"}',
    )
    parser.add_argument("--stream-url", default="", help="Optional single stream URL override for all cameras")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--conf-threshold", type=float, default=0.25)
    parser.add_argument("--iou-threshold", type=float, default=0.45)
    parser.add_argument("--classes-csv", default="15,16")
    parser.add_argument("--frame-stride", type=int, default=1)
    parser.add_argument("--commit-interval-frames", type=int, default=30)
    parser.add_argument("--reconnect-retries", type=int, default=20)
    parser.add_argument("--reconnect-delay-seconds", type=float, default=2.0)
    parser.add_argument("--max-frames", type=int, default=0)
    parser.add_argument("--max-seconds", type=int, default=0)
    parser.add_argument("--record-segments", action="store_true")
    parser.add_argument("--record-dir", default=str(Path("storage/uploads/segments")))
    parser.add_argument("--segment-seconds", type=int, default=20)
    parser.add_argument("--record-codec", default="mp4v")
    parser.add_argument(
        "--global-id-mode",
        choices=["animal", "camera_track", "reid_auto"],
        default="animal",
        help="When animal IDs are provided, 'animal' keeps one global_track_id across cameras.",
    )
    parser.add_argument("--reid-match-threshold", type=float, default=0.68)
    parser.add_argument(
        "--fallback-animal-id",
        default="system-reid-auto",
        help="Used when reid_auto assigns cross-camera IDs without explicit animal map.",
    )
    return parser


if __name__ == "__main__":
    run(build_parser().parse_args())
