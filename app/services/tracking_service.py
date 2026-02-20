import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from app.core.config import settings


@dataclass
class _TrackState:
    source_track_id: int
    start_frame: int
    end_frame: int
    class_id: int
    confidence_sum: float = 0.0
    confidence_count: int = 0
    observations: list[dict[str, Any]] = field(default_factory=list)

    def add_observation(
        self,
        frame_index: int,
        ts_seconds: float,
        bbox_xyxy: list[float],
        class_id: int,
        confidence: float,
    ) -> None:
        self.end_frame = frame_index
        self.class_id = class_id
        self.confidence_sum += confidence
        self.confidence_count += 1
        self.observations.append(
            {
                "frame_index": frame_index,
                "ts_seconds": ts_seconds,
                "bbox_xyxy": bbox_xyxy,
                "class_id": class_id,
                "conf": round(confidence, 6),
            }
        )

    @property
    def avg_confidence(self) -> float:
        if self.confidence_count == 0:
            return 0.0
        return self.confidence_sum / self.confidence_count


def _prepare_engine_imports() -> None:
    engine_root = settings.engine_root.resolve()
    deep_sort_reid_root = (engine_root / "deep_sort" / "deep" / "reid").resolve()

    for path in (engine_root, deep_sort_reid_root):
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)


def _build_tracker(device: str):
    _prepare_engine_imports()

    try:
        import torch
        from deep_sort.deep_sort import DeepSort
    except Exception as exc:  # pragma: no cover - depends on runtime deps
        raise RuntimeError(
            "DeepSort import failed. Ensure Engine path and tracking dependencies are installed."
        ) from exc

    model_name_or_path = settings.deep_sort_model.strip()
    if not model_name_or_path:
        raise RuntimeError("DEEP_SORT_MODEL must be configured")

    # Engine DeepSort downloads named models under this relative directory.
    Path("deep_sort/deep/checkpoint").mkdir(parents=True, exist_ok=True)

    deep_sort_device = device
    if device.startswith("cuda") and not torch.cuda.is_available():
        deep_sort_device = "cpu"

    return DeepSort(
        model=model_name_or_path,
        device=deep_sort_device,
        max_dist=settings.deep_sort_max_dist,
        max_iou_distance=settings.deep_sort_max_iou_distance,
        max_age=settings.deep_sort_max_age,
        n_init=settings.deep_sort_n_init,
        nn_budget=settings.deep_sort_nn_budget,
    )


def _build_detector():
    try:
        from ultralytics import YOLO
    except Exception as exc:  # pragma: no cover - depends on runtime deps
        raise RuntimeError(
            "Ultralytics import failed. Install tracking dependencies to enable YOLO inference."
        ) from exc

    return YOLO(settings.yolo_model)


def _xyxy_to_xywh(xyxy: np.ndarray) -> np.ndarray:
    out = np.zeros((xyxy.shape[0], 4), dtype=np.float32)
    out[:, 0] = (xyxy[:, 0] + xyxy[:, 2]) / 2.0
    out[:, 1] = (xyxy[:, 1] + xyxy[:, 3]) / 2.0
    out[:, 2] = xyxy[:, 2] - xyxy[:, 0]
    out[:, 3] = xyxy[:, 3] - xyxy[:, 1]
    return out


class YoloDeepSortTracker:
    def __init__(self, device: str | None = None) -> None:
        self.device = device or settings.tracking_device
        self.detector = _build_detector()
        self.tracker = _build_tracker(self.device)

    def process_frame(
        self,
        frame: np.ndarray,
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.45,
        classes: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        result = self.detector.predict(
            source=frame,
            conf=conf_threshold,
            iou=iou_threshold,
            classes=classes,
            device=self.device,
            verbose=False,
        )[0]

        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            self.tracker.increment_ages()
            return []

        xyxy = boxes.xyxy.detach().cpu().numpy().astype(np.float32)
        confs = boxes.conf.detach().cpu().numpy().astype(np.float32)
        class_ids = boxes.cls.detach().cpu().numpy().astype(np.int32)
        xywh = _xyxy_to_xywh(xyxy)

        outputs = self.tracker.update(
            bbox_xywh=xywh,
            confidences=confs,
            classes=class_ids,
            ori_img=frame,
        )
        if not isinstance(outputs, np.ndarray) or outputs.size == 0:
            return []

        parsed: list[dict[str, Any]] = []
        for row in outputs:
            x1, y1, x2, y2, source_track_id, class_id, conf = row.tolist()
            parsed.append(
                {
                    "source_track_id": int(source_track_id),
                    "class_id": int(class_id),
                    "conf": float(conf),
                    "bbox_xyxy": [float(x1), float(y1), float(x2), float(y2)],
                }
            )
        self._attach_embeddings(frame, parsed)
        return parsed

    def _attach_embeddings(self, frame: np.ndarray, outputs: list[dict[str, Any]]) -> None:
        if not outputs:
            return

        crops: list[np.ndarray] = []
        valid_indexes: list[int] = []
        height, width = frame.shape[:2]

        for idx, output in enumerate(outputs):
            x1, y1, x2, y2 = output["bbox_xyxy"]
            xi1 = max(int(x1), 0)
            yi1 = max(int(y1), 0)
            xi2 = min(int(x2), width - 1)
            yi2 = min(int(y2), height - 1)
            if xi2 <= xi1 or yi2 <= yi1:
                continue
            crop = frame[yi1:yi2, xi1:xi2]
            if crop.size == 0:
                continue
            crops.append(crop)
            valid_indexes.append(idx)

        if not crops:
            return

        try:
            features = self.tracker.extractor(crops)
        except Exception:
            return

        for feature_idx, out_idx in enumerate(valid_indexes):
            vector = features[feature_idx]
            outputs[out_idx]["embedding"] = [float(v) for v in vector.tolist()]


def track_video_with_yolo_deepsort(
    video_path: Path,
    conf_threshold: float = 0.25,
    iou_threshold: float = 0.45,
    frame_stride: int = 1,
    max_frames: int = 0,
    classes: list[int] | None = None,
) -> dict[str, Any]:
    runtime = YoloDeepSortTracker(device=settings.tracking_device)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError("Failed to open video file")

    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

    frame_index = 0
    processed_frames = 0
    total_detections = 0
    states: dict[int, _TrackState] = {}

    while True:
        ok, frame = cap.read()
        if not ok:
            break

        if frame_stride > 1 and frame_index % frame_stride != 0:
            frame_index += 1
            continue

        if max_frames > 0 and processed_frames >= max_frames:
            break

        outputs = runtime.process_frame(
            frame=frame,
            conf_threshold=conf_threshold,
            iou_threshold=iou_threshold,
            classes=classes,
        )
        total_detections += len(outputs)
        if outputs:
            for output in outputs:
                source_track_id = int(output["source_track_id"])
                class_id = int(output["class_id"])
                conf = float(output["conf"])
                state = states.get(source_track_id)
                if state is None:
                    state = _TrackState(
                        source_track_id=source_track_id,
                        start_frame=frame_index,
                        end_frame=frame_index,
                        class_id=class_id,
                    )
                    states[source_track_id] = state

                ts_seconds = (frame_index / fps) if fps > 0 else 0.0
                state.add_observation(
                    frame_index=frame_index,
                    ts_seconds=ts_seconds,
                    bbox_xyxy=output["bbox_xyxy"],
                    class_id=class_id,
                    confidence=conf,
                )

        frame_index += 1
        processed_frames += 1

    cap.release()

    duration_seconds = (total_frames / fps) if fps > 0 else 0.0
    tracks = []
    for state in states.values():
        tracks.append(
            {
                "source_track_id": state.source_track_id,
                "start_frame": state.start_frame,
                "end_frame": state.end_frame,
                "class_id": state.class_id,
                "avg_confidence": round(state.avg_confidence, 6),
                "observation_count": len(state.observations),
                "observations": state.observations,
            }
        )

    tracks.sort(key=lambda item: item["source_track_id"])

    return {
        "source_path": str(video_path),
        "fps": round(fps, 6),
        "total_frames": total_frames,
        "processed_frames": processed_frames,
        "duration_seconds": round(duration_seconds, 6),
        "total_detections": total_detections,
        "track_count": len(tracks),
        "tracks": tracks,
    }
