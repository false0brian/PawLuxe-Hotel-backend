from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AnimalCreate(BaseModel):
    species: str
    name: str
    owner_id: str | None = None
    active: bool = True


class CameraCreate(BaseModel):
    location_zone: str
    intrinsics_version: str | None = None
    stream_url: str | None = None
    installed_height_m: float | None = None
    tilt_deg: float | None = None


class CollarCreate(BaseModel):
    animal_id: str
    marker_id: str | None = None
    ble_id: str | None = None
    uwb_id: str | None = None
    start_ts: datetime | None = None
    end_ts: datetime | None = None


class TrackCreate(BaseModel):
    camera_id: str
    start_ts: datetime | None = None
    end_ts: datetime | None = None
    quality_score: float | None = None


class TrackObservationCreate(BaseModel):
    ts: datetime | None = None
    bbox: str
    marker_id_read: str | None = None
    appearance_vec_ref: str | None = None


class AssociationCreate(BaseModel):
    global_track_id: str
    track_id: str
    animal_id: str
    confidence: float = 0.0


class EventCreate(BaseModel):
    animal_id: str
    type: str
    severity: str = "info"
    start_ts: datetime | None = None
    end_ts: datetime | None = None


class PositionCreate(BaseModel):
    animal_id: str
    x_m: float
    y_m: float
    z_m: float | None = None
    method: str = "Ble"
    cov_matrix: str | None = None
    ts: datetime | None = None


class MediaSegmentCreate(BaseModel):
    camera_id: str | None = None
    start_ts: datetime | None = None
    end_ts: datetime | None = None
    path: str
    codec: str | None = None
    avg_bitrate: float | None = None


class ClipCreate(BaseModel):
    event_id: str | None = None
    path: str
    start_ts: datetime | None = None
    end_ts: datetime | None = None
    derived_from_segments: str | None = None


class ExportRequest(BaseModel):
    padding_seconds: float = 3.0
    merge_gap_seconds: float = 0.2
    min_duration_seconds: float = 0.3
    render_video: bool = False


class HighlightRequest(BaseModel):
    padding_seconds: float = 2.0
    target_seconds: float = 30.0
    per_clip_seconds: float = 4.0
    merge_gap_seconds: float = 0.2
    min_duration_seconds: float = 0.3


class ExportJobCreate(BaseModel):
    mode: str = "full"  # full | highlights
    padding_seconds: float = 3.0
    merge_gap_seconds: float = 0.2
    min_duration_seconds: float = 0.3
    render_video: bool = True
    target_seconds: float = 30.0
    per_clip_seconds: float = 4.0
    timeout_seconds: float = 600.0
    max_retries: int = 3
    dedupe: bool = True


class IdentityUpsert(BaseModel):
    animal_id: str | None = None
    state: str = "confirmed"
    source: str = "manual"


class BookingCreate(BaseModel):
    owner_id: str
    pet_id: str
    start_at: datetime
    end_at: datetime
    room_zone_id: str
    status: str = "reserved"


class PetZoneMoveCreate(BaseModel):
    pet_id: str
    to_zone_id: str
    by_staff_id: str | None = None
    at: datetime | None = None


class CareLogCreate(BaseModel):
    pet_id: str
    booking_id: str
    type: str
    value: str = ""
    details: dict[str, Any] | None = None
    staff_id: str = ""
    at: datetime | None = None


class StreamTokenRequest(BaseModel):
    owner_id: str
    booking_id: str
    pet_id: str
    max_sessions: int = 2
    ttl_seconds: int | None = None


class StreamVerifyRequest(BaseModel):
    token: str
    cam_id: str | None = None


class UserCreate(BaseModel):
    role: str
    email: str | None = None
    display_name: str | None = None
    active: bool = True


class SessionCreateRequest(BaseModel):
    user_id: str
    ttl_minutes: int = 480


class CameraHealthUpsert(BaseModel):
    camera_id: str
    status: str = "healthy"
    fps: float | None = None
    latency_ms: float | None = None
    last_frame_at: datetime | None = None
    reconnect_count: int = 0
    message: str | None = None
