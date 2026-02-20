from pathlib import Path

import cv2
import numpy as np


def analyze_video(video_path: Path, sample_interval_seconds: float = 1.0) -> dict:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError("Failed to open video file")

    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = round(total_frames / fps, 3) if fps > 0 else 0.0
    sample_step = max(int(fps * sample_interval_seconds), 1) if fps > 0 else 1

    frame_idx = 0
    sampled_frames = 0
    brightness_scores: list[float] = []
    motion_scores: list[float] = []
    prev_gray: np.ndarray | None = None

    while True:
        ok, frame = cap.read()
        if not ok:
            break

        if frame_idx % sample_step == 0:
            sampled_frames += 1
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            brightness_scores.append(float(np.mean(gray)))

            if prev_gray is not None:
                diff = cv2.absdiff(gray, prev_gray)
                motion_scores.append(float(np.mean(diff)))

            prev_gray = gray

        frame_idx += 1

    cap.release()

    avg_brightness = round(float(np.mean(brightness_scores)) if brightness_scores else 0.0, 4)
    avg_motion = round(float(np.mean(motion_scores)) if motion_scores else 0.0, 4)

    return {
        "source_path": str(video_path),
        "fps": round(fps, 3),
        "total_frames": total_frames,
        "duration_seconds": duration,
        "sample_interval_seconds": sample_interval_seconds,
        "sampled_frames": sampled_frames,
        "avg_brightness": avg_brightness,
        "avg_motion_score": avg_motion,
    }
