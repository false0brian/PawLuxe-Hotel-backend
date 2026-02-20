import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Animal(SQLModel, table=True):
    __tablename__ = "animals"

    animal_id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    species: str
    name: str
    owner_id: str | None = None
    active: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class User(SQLModel, table=True):
    __tablename__ = "users"

    user_id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    role: str = Field(default="owner", index=True)  # owner | staff | admin | system
    email: str | None = Field(default=None, index=True)
    display_name: str | None = None
    active: bool = True
    created_at: datetime = Field(default_factory=utcnow, index=True)


class AuthSession(SQLModel, table=True):
    __tablename__ = "auth_sessions"

    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    user_id: str = Field(foreign_key="users.user_id", index=True)
    token_hash: str = Field(index=True)
    exp: datetime = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)
    last_seen_at: datetime = Field(default_factory=utcnow, index=True)


class Collar(SQLModel, table=True):
    __tablename__ = "collars"

    collar_id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    animal_id: str = Field(foreign_key="animals.animal_id", index=True)
    marker_id: str | None = Field(default=None, index=True)
    ble_id: str | None = Field(default=None, index=True)
    uwb_id: str | None = Field(default=None, index=True)
    start_ts: datetime = Field(default_factory=utcnow)
    end_ts: datetime | None = None


class Camera(SQLModel, table=True):
    __tablename__ = "cameras"

    camera_id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    location_zone: str
    intrinsics_version: str | None = None
    stream_url: str | None = None
    installed_height_m: float | None = None
    tilt_deg: float | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Booking(SQLModel, table=True):
    __tablename__ = "bookings"

    booking_id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    owner_id: str = Field(index=True)
    pet_id: str = Field(foreign_key="animals.animal_id", index=True)
    start_at: datetime
    end_at: datetime
    room_zone_id: str = Field(index=True)
    status: str = Field(default="reserved", index=True)  # reserved | checked_in | checked_out | canceled
    created_at: datetime = Field(default_factory=utcnow, index=True)


class PetZoneEvent(SQLModel, table=True):
    __tablename__ = "pet_zone_events"

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    pet_id: str = Field(foreign_key="animals.animal_id", index=True)
    from_zone_id: str | None = Field(default=None, index=True)
    to_zone_id: str = Field(index=True)
    at: datetime = Field(default_factory=utcnow, index=True)
    by_staff_id: str | None = Field(default=None, index=True)


class Track(SQLModel, table=True):
    __tablename__ = "tracks"

    track_id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    camera_id: str = Field(foreign_key="cameras.camera_id", index=True)
    start_ts: datetime = Field(default_factory=utcnow)
    end_ts: datetime | None = None
    quality_score: float | None = None


class TrackObservation(SQLModel, table=True):
    __tablename__ = "track_observations"

    observation_id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    track_id: str = Field(foreign_key="tracks.track_id", index=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    bbox: str
    marker_id_read: str | None = None
    appearance_vec_ref: str | None = None


class Position(SQLModel, table=True):
    __tablename__ = "positions"

    position_id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    animal_id: str = Field(foreign_key="animals.animal_id", index=True)
    ts: datetime = Field(default_factory=utcnow, index=True)
    x_m: float
    y_m: float
    z_m: float | None = None
    method: str = Field(default="Ble")
    cov_matrix: str | None = None


class Association(SQLModel, table=True):
    __tablename__ = "associations"

    association_id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    global_track_id: str = Field(index=True)
    track_id: str = Field(foreign_key="tracks.track_id", index=True)
    animal_id: str = Field(foreign_key="animals.animal_id", index=True)
    confidence: float = 0.0
    created_at: datetime = Field(default_factory=utcnow)


class GlobalTrackProfile(SQLModel, table=True):
    __tablename__ = "global_track_profiles"

    global_track_id: str = Field(primary_key=True)
    class_id: int = Field(index=True)
    embedding_json: str
    sample_count: int = 1
    updated_at: datetime = Field(default_factory=utcnow, index=True)


class GlobalIdentity(SQLModel, table=True):
    __tablename__ = "global_identities"

    global_track_id: str = Field(primary_key=True)
    animal_id: str | None = Field(default=None, foreign_key="animals.animal_id", index=True)
    state: str = Field(default="unknown", index=True)  # unknown | confirmed
    source: str = Field(default="reid_auto")  # reid_auto | manual | marker
    last_confidence: float | None = None
    updated_at: datetime = Field(default_factory=utcnow, index=True)


class Event(SQLModel, table=True):
    __tablename__ = "events"

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    animal_id: str = Field(foreign_key="animals.animal_id", index=True)
    start_ts: datetime = Field(default_factory=utcnow, index=True)
    end_ts: datetime | None = None
    type: str
    severity: str = Field(default="info")
    created_at: datetime = Field(default_factory=utcnow)


class MediaSegment(SQLModel, table=True):
    __tablename__ = "media_segments"

    segment_id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    camera_id: str | None = Field(default=None, foreign_key="cameras.camera_id", index=True)
    start_ts: datetime = Field(default_factory=utcnow, index=True)
    end_ts: datetime | None = None
    path: str
    codec: str | None = None
    avg_bitrate: float | None = None


class Clip(SQLModel, table=True):
    __tablename__ = "clips"

    clip_id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    event_id: str | None = Field(default=None, foreign_key="events.event_id", index=True)
    path: str
    start_ts: datetime = Field(default_factory=utcnow, index=True)
    end_ts: datetime | None = None
    derived_from_segments: str | None = None


class AccessToken(SQLModel, table=True):
    __tablename__ = "access_tokens"

    token_id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    owner_id: str = Field(index=True)
    booking_id: str = Field(foreign_key="bookings.booking_id", index=True)
    pet_id: str = Field(foreign_key="animals.animal_id", index=True)
    cam_id: str = Field(foreign_key="cameras.camera_id", index=True)
    exp: datetime = Field(index=True)
    sessions: int = 1
    created_at: datetime = Field(default_factory=utcnow, index=True)


class CareLog(SQLModel, table=True):
    __tablename__ = "care_logs"

    log_id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    pet_id: str = Field(foreign_key="animals.animal_id", index=True)
    booking_id: str = Field(foreign_key="bookings.booking_id", index=True)
    type: str = Field(index=True)  # feeding | potty | walk | medication | note
    at: datetime = Field(default_factory=utcnow, index=True)
    value: str
    value_json: str | None = None
    staff_id: str = Field(index=True)


class StreamAuditLog(SQLModel, table=True):
    __tablename__ = "stream_audit_logs"

    log_id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    at: datetime = Field(default_factory=utcnow, index=True)
    action: str = Field(index=True)  # issue | verify | deny
    request_role: str = Field(index=True)
    request_user_id: str | None = Field(default=None, index=True)
    owner_id: str | None = Field(default=None, index=True)
    booking_id: str | None = Field(default=None, index=True)
    pet_id: str | None = Field(default=None, index=True)
    zone_id: str | None = Field(default=None, index=True)
    cam_id: str | None = Field(default=None, index=True)
    result: str = Field(default="ok", index=True)  # ok | denied
    reason: str | None = None


class CameraHealth(SQLModel, table=True):
    __tablename__ = "camera_health"

    camera_id: str = Field(foreign_key="cameras.camera_id", primary_key=True)
    status: str = Field(default="unknown", index=True)  # healthy | degraded | down | unknown
    fps: float | None = None
    latency_ms: float | None = None
    last_frame_at: datetime | None = Field(default=None, index=True)
    reconnect_count: int = 0
    message: str | None = None
    updated_at: datetime = Field(default_factory=utcnow, index=True)


class VideoAnalysis(SQLModel, table=True):
    __tablename__ = "video_analyses"

    video_id: str = Field(primary_key=True)
    animal_id: str | None = Field(default=None, foreign_key="animals.animal_id", index=True)
    camera_id: str | None = Field(default=None, foreign_key="cameras.camera_id", index=True)
    filename: str
    uploaded_path: str
    encrypted_analysis_path: str
    duration_seconds: float
    fps: float
    total_frames: int
    sampled_frames: int
    avg_motion_score: float
    avg_brightness: float
    created_at: datetime = Field(default_factory=utcnow, index=True)


class ExportJob(SQLModel, table=True):
    __tablename__ = "export_jobs"

    job_id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    global_track_id: str = Field(index=True)
    mode: str = Field(default="full")  # full | highlights
    status: str = Field(default="pending", index=True)  # pending | running | done | failed | canceled
    payload_json: str
    retry_count: int = 0
    max_retries: int = 3
    next_run_at: datetime | None = Field(default_factory=utcnow, index=True)
    canceled_at: datetime | None = None
    export_id: str | None = Field(default=None, index=True)
    manifest_path: str | None = None
    video_path: str | None = None
    error_message: str | None = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
    started_at: datetime | None = None
    finished_at: datetime | None = None
