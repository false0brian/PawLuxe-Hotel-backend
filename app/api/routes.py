import json
import asyncio
from datetime import datetime, timedelta, timezone
import secrets
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from app.core.auth import (
    AuthContext,
    hash_session_token,
    require_admin_or_system,
    require_owner_or_admin,
    require_staff_or_admin,
    verify_api_key,
)
from app.core.config import settings
from app.db.models import (
    AccessToken,
    Animal,
    Association,
    AuthSession,
    Booking,
    Camera,
    CameraHealth,
    CareLog,
    Clip,
    Collar,
    Event,
    ExportJob,
    GlobalIdentity,
    MediaSegment,
    PetZoneEvent,
    Position,
    RealtimeTrackBinding,
    StaffAlert,
    StreamAuditLog,
    StreamPlaybackSession,
    Track,
    TrackObservation,
    User,
    VideoAnalysis,
    utcnow,
)
from app.db.session import engine, get_session
from app.schemas.domain import (
    AnimalCreate,
    AssociationCreate,
    BookingCreate,
    CameraCreate,
    CareLogCreate,
    ClipCreate,
    CollarCreate,
    EventCreate,
    ExportJobCreate,
    ExportRequest,
    HighlightRequest,
    IdentityUpsert,
    LiveTrackIngestRequest,
    MediaSegmentCreate,
    PetZoneMoveCreate,
    PositionCreate,
    SessionCreateRequest,
    StaffAlertAckRequest,
    StreamSessionCloseRequest,
    StreamTokenRequest,
    StreamVerifyRequest,
    TrackCreate,
    TrackObservationCreate,
    UserCreate,
    CameraHealthUpsert,
)
from app.services.export_service import (
    build_export_plan,
    build_highlight_plan,
    load_manifest,
    manifest_path_for_export,
    render_export_video,
    save_manifest,
    video_path_for_export,
)
from app.services.storage_service import (
    read_encrypted_analysis,
    save_upload,
    store_encrypted_analysis,
)
from app.services.stream_auth_service import parse_and_verify, sign_payload
from app.services.tracking_service import track_video_with_yolo_deepsort
from app.services.video_service import analyze_video

router = APIRouter(dependencies=[Depends(verify_api_key)])


def _timeline_item(kind: str, ts: datetime, payload: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "ts": ts,
        "data": payload.model_dump() if hasattr(payload, "model_dump") else payload,
    }


def _build_global_track_id(global_id_mode: str, camera_id: str, source_track_id: int, animal_id: str | None) -> str:
    if global_id_mode == "animal" and animal_id:
        return f"animal:{animal_id}"
    return f"{camera_id}:{source_track_id}"


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _active_booking(session: Session, owner_id: str, booking_id: str, pet_id: str) -> Booking:
    booking = session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.owner_id != owner_id or booking.pet_id != pet_id:
        raise HTTPException(status_code=403, detail="Booking owner/pet mismatch")
    if booking.status not in {"reserved", "checked_in"}:
        raise HTTPException(status_code=403, detail="Booking is not active")
    now = utcnow()
    if _to_utc(booking.start_at) > now or _to_utc(booking.end_at) < now:
        raise HTTPException(status_code=403, detail="Booking not in active time window")
    return booking


def _pet_current_zone(session: Session, pet_id: str, booking: Booking) -> str:
    row = session.exec(
        select(PetZoneEvent).where(PetZoneEvent.pet_id == pet_id).order_by(PetZoneEvent.at.desc()).limit(1)
    ).first()
    if row:
        return row.to_zone_id
    return booking.room_zone_id


def _allowed_cam_ids(session: Session, zone_id: str) -> list[str]:
    rows = list(session.exec(select(Camera).where(Camera.location_zone == zone_id)))
    return [row.camera_id for row in rows]


def _is_play_zone(zone_id: str) -> bool:
    upper = zone_id.upper()
    if upper.startswith("PLAY"):
        return True
    parts = upper.split("-")
    return len(parts) >= 2 and parts[1] == "PLAY"


def _next_action_for_staff(booking: Booking, latest_log: CareLog | None, now: datetime) -> str:
    end_at = _to_utc(booking.end_at)
    remaining_min = (end_at - now).total_seconds() / 60.0
    if remaining_min <= 30:
        return "checkout_prepare"
    if latest_log is None:
        return "feeding_due"
    if latest_log.type == "feeding":
        return "potty_check"
    if latest_log.type == "potty":
        return "walk_due"
    if latest_log.type == "walk":
        return "rest_monitor"
    if latest_log.type == "medication":
        return "medication_followup"
    return "care_check"


def _serialize_care_log(row: CareLog) -> dict[str, Any]:
    details: dict[str, Any] | None = None
    if row.value_json:
        try:
            parsed = json.loads(row.value_json)
            details = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            details = None
    return {
        "log_id": row.log_id,
        "pet_id": row.pet_id,
        "booking_id": row.booking_id,
        "type": row.type,
        "at": row.at,
        "value": row.value,
        "details": details,
        "staff_id": row.staff_id,
    }


def _audit_stream(
    session: Session,
    *,
    action: str,
    auth: AuthContext,
    owner_id: str | None = None,
    booking_id: str | None = None,
    pet_id: str | None = None,
    zone_id: str | None = None,
    cam_id: str | None = None,
    result: str = "ok",
    reason: str | None = None,
) -> None:
    row = StreamAuditLog(
        action=action,
        request_role=auth.role,
        request_user_id=auth.user_id or None,
        owner_id=owner_id,
        booking_id=booking_id,
        pet_id=pet_id,
        zone_id=zone_id,
        cam_id=cam_id,
        result=result,
        reason=reason,
    )
    session.add(row)
    session.commit()


def _ensure_staff_alert(
    session: Session,
    *,
    type: str,
    severity: str,
    message: str,
    zone_id: str | None = None,
    camera_id: str | None = None,
    pet_id: str | None = None,
    booking_id: str | None = None,
    details: dict[str, Any] | None = None,
    dedupe_seconds: int = 120,
) -> StaffAlert:
    since = utcnow() - timedelta(seconds=max(1, dedupe_seconds))
    existing = session.exec(
        select(StaffAlert)
        .where(StaffAlert.type == type)
        .where(StaffAlert.status == "open")
        .where(StaffAlert.at >= since)
        .where(StaffAlert.camera_id == camera_id)
        .where(StaffAlert.pet_id == pet_id)
        .where(StaffAlert.booking_id == booking_id)
        .order_by(StaffAlert.at.desc())
        .limit(1)
    ).first()
    if existing:
        return existing

    row = StaffAlert(
        type=type,
        severity=severity,
        message=message,
        zone_id=zone_id,
        camera_id=camera_id,
        pet_id=pet_id,
        booking_id=booking_id,
        details_json=json.dumps(details, ensure_ascii=True) if details else None,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _serialize_staff_alert(row: StaffAlert) -> dict[str, Any]:
    details = None
    if row.details_json:
        try:
            parsed = json.loads(row.details_json)
            details = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            details = None
    return {
        "alert_id": row.alert_id,
        "at": row.at.isoformat() if row.at else None,
        "type": row.type,
        "severity": row.severity,
        "status": row.status,
        "message": row.message,
        "zone_id": row.zone_id,
        "camera_id": row.camera_id,
        "pet_id": row.pet_id,
        "booking_id": row.booking_id,
        "details": details,
        "acked_by": row.acked_by,
        "acked_at": row.acked_at.isoformat() if row.acked_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _evaluate_staff_alert_rules(
    session: Session,
    *,
    stale_seconds: int,
    isolation_minutes: int,
    idle_seconds: int,
) -> int:
    now = utcnow()
    created = 0

    # Rule 1: camera health degraded/down/stale
    health_rows = list(session.exec(select(CameraHealth)))
    for row in health_rows:
        is_stale = True
        if row.last_frame_at:
            is_stale = (now - _to_utc(row.last_frame_at)).total_seconds() > stale_seconds
        if row.status in {"down", "degraded"} or is_stale:
            sev = "critical" if row.status == "down" else "warning"
            detail = {"status": row.status, "is_stale": is_stale, "last_frame_at": row.last_frame_at}
            _ensure_staff_alert(
                session,
                type="camera_health",
                severity=sev,
                message=f"카메라 상태 이상: {row.camera_id} ({row.status}{', stale' if is_stale else ''})",
                camera_id=row.camera_id,
                details=detail,
                dedupe_seconds=120,
            )
            created += 1

    # Rule 2: recent move to isolation
    isolation_since = now - timedelta(minutes=isolation_minutes)
    iso_rows = list(
        session.exec(
            select(PetZoneEvent)
            .where(PetZoneEvent.at >= isolation_since)
            .where(PetZoneEvent.to_zone_id.contains("ISOLATION"))
            .order_by(PetZoneEvent.at.desc())
            .limit(100)
        )
    )
    for row in iso_rows:
        _ensure_staff_alert(
            session,
            type="isolation_move",
            severity="warning",
            message=f"격리 이동 감지: pet={row.pet_id}, zone={row.to_zone_id}",
            zone_id=row.to_zone_id,
            pet_id=row.pet_id,
            details={"from_zone_id": row.from_zone_id, "by_staff_id": row.by_staff_id, "at": row.at.isoformat()},
            dedupe_seconds=300,
        )
        created += 1

    # Rule 3: active booking pet has no recent track observation
    bookings = list(
        session.exec(
            select(Booking)
            .where(Booking.status.in_(["reserved", "checked_in"]))
            .where(Booking.start_at <= now)
            .where(Booking.end_at >= now)
            .limit(500)
        )
    )
    for booking in bookings:
        assoc_rows = list(
            session.exec(
                select(Association)
                .where(Association.animal_id == booking.pet_id)
                .order_by(Association.created_at.desc())
                .limit(20)
            )
        )
        latest_obs_ts: datetime | None = None
        for assoc in assoc_rows:
            obs = session.exec(
                select(TrackObservation)
                .where(TrackObservation.track_id == assoc.track_id)
                .order_by(TrackObservation.ts.desc())
                .limit(1)
            ).first()
            if obs and (latest_obs_ts is None or obs.ts > latest_obs_ts):
                latest_obs_ts = obs.ts

        if latest_obs_ts is None or (now - _to_utc(latest_obs_ts)).total_seconds() > idle_seconds:
            latest_zone = session.exec(
                select(PetZoneEvent).where(PetZoneEvent.pet_id == booking.pet_id).order_by(PetZoneEvent.at.desc()).limit(1)
            ).first()
            zone_id = latest_zone.to_zone_id if latest_zone else booking.room_zone_id
            _ensure_staff_alert(
                session,
                type="animal_idle",
                severity="warning",
                message=f"트래킹 공백 감지: pet={booking.pet_id}, zone={zone_id}",
                zone_id=zone_id,
                pet_id=booking.pet_id,
                booking_id=booking.booking_id,
                details={"last_observation_ts": latest_obs_ts.isoformat() if latest_obs_ts else None},
                dedupe_seconds=180,
            )
            created += 1
    return created


def _find_segment_for_camera_ts(
    session: Session,
    *,
    camera_id: str,
    ts: datetime,
    slack_seconds: int = 20,
) -> MediaSegment | None:
    ts_utc = _to_utc(ts)
    row = session.exec(
        select(MediaSegment)
        .where(MediaSegment.camera_id == camera_id)
        .where(MediaSegment.start_ts <= ts_utc + timedelta(seconds=slack_seconds))
        .order_by(MediaSegment.start_ts.desc())
        .limit(20)
    ).first()
    if not row:
        return None
    if row.end_ts is None:
        return row
    if _to_utc(row.end_ts) >= ts_utc - timedelta(seconds=slack_seconds):
        return row
    return None


def _parse_bbox_xyxy(raw: str) -> list[float] | None:
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or len(parsed) != 4:
            return None
        vals = [float(v) for v in parsed]
        return vals
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _collect_live_tracks(
    session: Session,
    *,
    camera_id: str | None,
    animal_id: str | None,
    since_ts: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    query = (
        select(TrackObservation, Track)
        .join(Track, TrackObservation.track_id == Track.track_id)
        .order_by(TrackObservation.ts.desc())
        .limit(limit)
    )
    if camera_id:
        query = query.where(Track.camera_id == camera_id)
    if since_ts:
        query = query.where(TrackObservation.ts >= since_ts)

    rows = list(session.exec(query))
    out: list[dict[str, Any]] = []
    for obs, track in rows:
        assoc = session.exec(
            select(Association).where(Association.track_id == track.track_id).order_by(Association.created_at.desc()).limit(1)
        ).first()
        assoc_animal_id = assoc.animal_id if assoc else None
        if animal_id and assoc_animal_id != animal_id:
            continue

        bbox = _parse_bbox_xyxy(obs.bbox)
        if bbox is None:
            continue
        cam = session.get(Camera, track.camera_id)
        out.append(
            {
                "ts": obs.ts,
                "track_id": track.track_id,
                "camera_id": track.camera_id,
                "zone_id": cam.location_zone if cam else None,
                "animal_id": assoc_animal_id,
                "bbox_xyxy": bbox,
                "quality_score": track.quality_score,
            }
        )
    return out


@router.post("/animals")
def create_animal(payload: AnimalCreate, session: Session = Depends(get_session)) -> Animal:
    animal = Animal(**payload.model_dump())
    session.add(animal)
    session.commit()
    session.refresh(animal)
    return animal


@router.get("/animals")
def list_animals(
    active: bool | None = Query(default=None), session: Session = Depends(get_session)
) -> list[Animal]:
    query = select(Animal)
    if active is not None:
        query = query.where(Animal.active == active)
    return list(session.exec(query))


@router.post("/auth/users")
def create_user(
    payload: UserCreate,
    _: AuthContext = Depends(require_admin_or_system),
    session: Session = Depends(get_session),
) -> User:
    role = payload.role.strip().lower()
    if role not in {"owner", "staff", "admin", "system"}:
        raise HTTPException(status_code=400, detail="role must be one of: owner, staff, admin, system")
    user = User(role=role, email=payload.email, display_name=payload.display_name, active=payload.active)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@router.get("/auth/users")
def list_users(
    role: str | None = Query(default=None),
    active: bool | None = Query(default=None),
    _: AuthContext = Depends(require_admin_or_system),
    session: Session = Depends(get_session),
) -> list[User]:
    query = select(User)
    if role:
        query = query.where(User.role == role.strip().lower())
    if active is not None:
        query = query.where(User.active == active)
    return list(session.exec(query.order_by(User.created_at.desc())))


@router.post("/auth/sessions")
def create_auth_session(
    payload: SessionCreateRequest,
    _: AuthContext = Depends(require_admin_or_system),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if payload.ttl_minutes < 5 or payload.ttl_minutes > 1440:
        raise HTTPException(status_code=400, detail="ttl_minutes must be between 5 and 1440")
    user = session.get(User, payload.user_id)
    if not user or not user.active:
        raise HTTPException(status_code=404, detail="Active user not found")

    raw = f"psess_{secrets.token_urlsafe(32)}"
    row = AuthSession(
        user_id=user.user_id,
        token_hash=hash_session_token(raw),
        exp=utcnow() + timedelta(minutes=payload.ttl_minutes),
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return {
        "session_token": raw,
        "session_id": row.session_id,
        "user_id": user.user_id,
        "role": user.role,
        "exp": row.exp,
    }


@router.post("/auth/sessions/{session_id}/revoke")
def revoke_auth_session(
    session_id: str,
    _: AuthContext = Depends(require_admin_or_system),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    row = session.get(AuthSession, session_id)
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    session.delete(row)
    session.commit()
    return {"status": "revoked", "session_id": session_id}


@router.post("/cameras")
def create_camera(payload: CameraCreate, session: Session = Depends(get_session)) -> Camera:
    camera = Camera(**payload.model_dump())
    session.add(camera)
    session.commit()
    session.refresh(camera)
    return camera


@router.get("/cameras")
def list_cameras(session: Session = Depends(get_session)) -> list[Camera]:
    return list(session.exec(select(Camera)))


@router.post("/system/camera-health")
def upsert_camera_health(
    payload: CameraHealthUpsert,
    _: AuthContext = Depends(require_admin_or_system),
    session: Session = Depends(get_session),
) -> CameraHealth:
    if not session.get(Camera, payload.camera_id):
        raise HTTPException(status_code=404, detail="Camera not found")
    status = payload.status.strip().lower()
    if status not in {"healthy", "degraded", "down", "unknown"}:
        raise HTTPException(status_code=400, detail="status must be one of: healthy, degraded, down, unknown")

    row = session.get(CameraHealth, payload.camera_id)
    if not row:
        row = CameraHealth(camera_id=payload.camera_id)
    row.status = status
    row.fps = payload.fps
    row.latency_ms = payload.latency_ms
    row.last_frame_at = payload.last_frame_at
    row.reconnect_count = payload.reconnect_count
    row.message = payload.message
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.get("/admin/camera-health")
def list_camera_health(
    stale_seconds: int = Query(default=10, ge=1, le=600),
    _: AuthContext = Depends(require_staff_or_admin),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    now = utcnow()
    rows = list(session.exec(select(CameraHealth).order_by(CameraHealth.updated_at.desc())))
    out: list[dict[str, Any]] = []
    for row in rows:
        stale = True
        if row.last_frame_at:
            stale = (now - _to_utc(row.last_frame_at)).total_seconds() > stale_seconds
        out.append(
            {
                "camera_id": row.camera_id,
                "status": row.status,
                "fps": row.fps,
                "latency_ms": row.latency_ms,
                "last_frame_at": row.last_frame_at,
                "updated_at": row.updated_at,
                "reconnect_count": row.reconnect_count,
                "message": row.message,
                "is_stale": stale,
            }
        )
    return out


@router.post("/bookings")
def create_booking(payload: BookingCreate, session: Session = Depends(get_session)) -> Booking:
    if not session.get(Animal, payload.pet_id):
        raise HTTPException(status_code=404, detail="Pet not found")
    data = payload.model_dump()
    if data["end_at"] <= data["start_at"]:
        raise HTTPException(status_code=400, detail="end_at must be after start_at")
    booking = Booking(**data)
    session.add(booking)
    session.commit()
    session.refresh(booking)
    return booking


@router.get("/bookings")
def list_bookings(
    owner_id: str | None = Query(default=None),
    pet_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> list[Booking]:
    query = select(Booking)
    if owner_id:
        query = query.where(Booking.owner_id == owner_id)
    if pet_id:
        query = query.where(Booking.pet_id == pet_id)
    if status:
        query = query.where(Booking.status == status)
    return list(session.exec(query.order_by(Booking.start_at.desc())))


@router.post("/staff/move-zone")
def staff_move_zone(
    payload: PetZoneMoveCreate,
    auth: AuthContext = Depends(require_staff_or_admin),
    session: Session = Depends(get_session),
) -> PetZoneEvent:
    if not session.get(Animal, payload.pet_id):
        raise HTTPException(status_code=404, detail="Pet not found")

    prev = session.exec(
        select(PetZoneEvent).where(PetZoneEvent.pet_id == payload.pet_id).order_by(PetZoneEvent.at.desc()).limit(1)
    ).first()
    row = PetZoneEvent(
        pet_id=payload.pet_id,
        from_zone_id=prev.to_zone_id if prev else None,
        to_zone_id=payload.to_zone_id,
        at=payload.at or utcnow(),
        by_staff_id=payload.by_staff_id or auth.user_id,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.get("/staff/today-board")
def get_staff_today_board(
    _: AuthContext = Depends(require_staff_or_admin),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    now = utcnow()
    bookings = list(
        session.exec(
            select(Booking)
            .where(Booking.status.in_(["reserved", "checked_in"]))
            .where(Booking.start_at <= now)
            .where(Booking.end_at >= now)
            .order_by(Booking.start_at.asc())
        )
    )

    items: list[dict[str, Any]] = []
    zone_counts: dict[str, int] = {}
    action_counts = {
        "feeding": 0,
        "potty": 0,
        "walk": 0,
        "medication": 0,
        "note": 0,
    }
    for booking in bookings:
        pet = session.get(Animal, booking.pet_id)
        if not pet:
            continue
        latest_zone = session.exec(
            select(PetZoneEvent).where(PetZoneEvent.pet_id == pet.animal_id).order_by(PetZoneEvent.at.desc()).limit(1)
        ).first()
        current_zone = latest_zone.to_zone_id if latest_zone else booking.room_zone_id

        latest_log = session.exec(
            select(CareLog)
            .where(CareLog.pet_id == pet.animal_id)
            .where(CareLog.booking_id == booking.booking_id)
            .order_by(CareLog.at.desc())
            .limit(1)
        ).first()

        risk_badges: list[str] = []
        if current_zone.upper().find("ISOLATION") >= 0:
            risk_badges.append("격리")
        if latest_log and latest_log.type == "medication":
            risk_badges.append("투약")
        if pet.species.lower() == "cat":
            risk_badges.append("민감")
        if not risk_badges:
            risk_badges.append("정상")

        if latest_log and latest_log.type in action_counts:
            action_counts[latest_log.type] += 1
        zone_counts[current_zone] = zone_counts.get(current_zone, 0) + 1

        items.append(
            {
                "booking_id": booking.booking_id,
                "pet_id": pet.animal_id,
                "pet_name": pet.name,
                "species": pet.species,
                "current_zone": current_zone,
                "risk_badges": risk_badges,
                "last_log": latest_log,
                "next_action": _next_action_for_staff(booking, latest_log, now),
            }
        )

    return {
        "at": now,
        "total_active_bookings": len(items),
        "zone_counts": zone_counts,
        "action_counts": action_counts,
        "items": items,
    }


@router.post("/staff/logs")
def create_care_log(
    payload: CareLogCreate,
    auth: AuthContext = Depends(require_staff_or_admin),
    session: Session = Depends(get_session),
) -> CareLog:
    if not session.get(Animal, payload.pet_id):
        raise HTTPException(status_code=404, detail="Pet not found")
    booking = session.get(Booking, payload.booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if booking.pet_id != payload.pet_id:
        raise HTTPException(status_code=400, detail="booking_id and pet_id mismatch")

    allowed_types = {"feeding", "potty", "walk", "medication", "note"}
    if payload.type not in allowed_types:
        raise HTTPException(status_code=400, detail="type must be one of: feeding, potty, walk, medication, note")
    value = payload.value.strip()
    if not value and payload.details:
        value = payload.type
    if not value:
        raise HTTPException(status_code=400, detail="value or details is required")

    row = CareLog(
        pet_id=payload.pet_id,
        booking_id=payload.booking_id,
        type=payload.type,
        at=payload.at or utcnow(),
        value=value,
        value_json=json.dumps(payload.details, ensure_ascii=True) if payload.details is not None else None,
        staff_id=payload.staff_id or auth.user_id,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.get("/pets/{pet_id}/status")
def get_pet_status(
    pet_id: str,
    owner_id: str | None = Query(default=None),
    auth: AuthContext = Depends(require_owner_or_admin),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    pet = session.get(Animal, pet_id)
    if not pet:
        raise HTTPException(status_code=404, detail="Pet not found")

    if auth.role == "owner":
        if pet.owner_id != auth.user_id:
            raise HTTPException(status_code=403, detail="Owner cannot access this pet")
        if owner_id and owner_id != auth.user_id:
            raise HTTPException(status_code=403, detail="owner_id must match x-user-id")
        owner_id = auth.user_id

    now = utcnow()
    booking_query = (
        select(Booking)
        .where(Booking.pet_id == pet_id)
        .where(Booking.start_at <= now)
        .where(Booking.end_at >= now)
        .where(Booking.status.in_(["reserved", "checked_in"]))
        .order_by(Booking.start_at.desc())
        .limit(1)
    )
    if owner_id:
        booking_query = booking_query.where(Booking.owner_id == owner_id)
    booking = session.exec(booking_query).first()

    latest_zone = session.exec(
        select(PetZoneEvent).where(PetZoneEvent.pet_id == pet_id).order_by(PetZoneEvent.at.desc()).limit(1)
    ).first()
    current_zone = latest_zone.to_zone_id if latest_zone else (booking.room_zone_id if booking else None)
    cam_ids = _allowed_cam_ids(session, current_zone) if current_zone else []

    next_log = session.exec(
        select(CareLog).where(CareLog.pet_id == pet_id).order_by(CareLog.at.desc()).limit(1)
    ).first()
    last_care_log_details: dict[str, Any] | None = None
    if next_log and next_log.value_json:
        try:
            parsed = json.loads(next_log.value_json)
            last_care_log_details = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            last_care_log_details = None

    return {
        "pet_id": pet_id,
        "owner_id": pet.owner_id,
        "booking_id": booking.booking_id if booking else None,
        "status": "active" if booking else "idle",
        "current_zone_id": current_zone,
        "cam_ids": cam_ids,
        "last_zone_event_at": latest_zone.at if latest_zone else None,
        "last_care_log": next_log,
        "last_care_log_details": last_care_log_details,
    }


@router.get("/live/tracks/latest")
def get_live_tracks_latest(
    camera_id: str | None = Query(default=None),
    animal_id: str | None = Query(default=None),
    since_ts: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=300),
    _: AuthContext = Depends(require_staff_or_admin),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    items = _collect_live_tracks(
        session=session,
        camera_id=camera_id,
        animal_id=animal_id,
        since_ts=since_ts,
        limit=limit,
    )
    next_cursor = max((row["ts"] for row in items), default=since_ts)
    return {
        "count": len(items),
        "next_since_ts": next_cursor,
        "tracks": items,
    }


@router.get("/live/zones/summary")
def get_live_zones_summary(
    window_seconds: int = Query(default=10, ge=1, le=120),
    camera_id: str | None = Query(default=None),
    animal_id: str | None = Query(default=None),
    _: AuthContext = Depends(require_staff_or_admin),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    now = utcnow()
    rows = _collect_live_tracks(
        session=session,
        camera_id=camera_id,
        animal_id=animal_id,
        since_ts=now - timedelta(seconds=window_seconds),
        limit=300,
    )

    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        zone_id = row["zone_id"] or "UNKNOWN"
        entry = buckets.get(zone_id)
        if not entry:
            entry = {
                "zone_id": zone_id,
                "track_ids": set(),
                "animal_ids": set(),
                "camera_ids": set(),
                "observation_count": 0,
                "last_ts": None,
            }
            buckets[zone_id] = entry
        entry["observation_count"] += 1
        entry["track_ids"].add(row["track_id"])
        entry["camera_ids"].add(row["camera_id"])
        if row["animal_id"]:
            entry["animal_ids"].add(row["animal_id"])
        ts = row["ts"]
        if entry["last_ts"] is None or ts > entry["last_ts"]:
            entry["last_ts"] = ts

    zones = [
        {
            "zone_id": zone_id,
            "camera_ids": sorted(entry["camera_ids"]),
            "track_count": len(entry["track_ids"]),
            "animal_count": len(entry["animal_ids"]),
            "observation_count": entry["observation_count"],
            "last_ts": entry["last_ts"],
        }
        for zone_id, entry in buckets.items()
    ]
    zones.sort(key=lambda it: (-it["observation_count"], it["zone_id"]))
    return {
        "at": now,
        "window_seconds": window_seconds,
        "zone_count": len(zones),
        "total_observations": len(rows),
        "zones": zones,
    }


@router.get("/live/zones/heatmap")
def get_live_zones_heatmap(
    window_seconds: int = Query(default=300, ge=10, le=3600),
    bucket_seconds: int = Query(default=10, ge=1, le=300),
    camera_id: str | None = Query(default=None),
    animal_id: str | None = Query(default=None),
    _: AuthContext = Depends(require_staff_or_admin),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    now = utcnow()
    bucket_count = max(1, min(240, window_seconds // max(1, bucket_seconds)))
    now_epoch = int(now.timestamp())
    aligned_now_epoch = now_epoch - (now_epoch % bucket_seconds)
    start_epoch = aligned_now_epoch - (bucket_count - 1) * bucket_seconds

    rows = _collect_live_tracks(
        session=session,
        camera_id=camera_id,
        animal_id=animal_id,
        since_ts=datetime.fromtimestamp(start_epoch, tz=timezone.utc),
        limit=5000,
    )

    zone_counts: dict[str, list[int]] = {}
    for row in rows:
        zone_id = row["zone_id"] or "UNKNOWN"
        ts_epoch = int(_to_utc(row["ts"]).timestamp())
        idx = (ts_epoch - start_epoch) // bucket_seconds
        if idx < 0 or idx >= bucket_count:
            continue
        counts = zone_counts.get(zone_id)
        if counts is None:
            counts = [0] * bucket_count
            zone_counts[zone_id] = counts
        counts[idx] += 1

    bucket_starts = [
        datetime.fromtimestamp(start_epoch + i * bucket_seconds, tz=timezone.utc)
        for i in range(bucket_count)
    ]
    zones = []
    for zone_id, counts in zone_counts.items():
        zones.append(
            {
                "zone_id": zone_id,
                "counts": counts,
                "total_observations": sum(counts),
                "max_bucket_count": max(counts) if counts else 0,
            }
        )
    zones.sort(key=lambda it: (-it["total_observations"], it["zone_id"]))
    return {
        "at": now,
        "window_seconds": window_seconds,
        "bucket_seconds": bucket_seconds,
        "bucket_count": bucket_count,
        "bucket_starts": bucket_starts,
        "zones": zones,
    }


@router.get("/live/cameras/{camera_id}/playback-url")
def get_camera_playback_url(
    camera_id: str,
    _: AuthContext = Depends(require_staff_or_admin),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    camera = session.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    return {
        "camera_id": camera_id,
        "zone_id": camera.location_zone,
        "stream_base_url": settings.stream_base_url,
        "playback_url": f"{settings.stream_base_url}/{camera_id}",
    }


@router.websocket("/ws/live-tracks")
async def ws_live_tracks(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        api_key = websocket.query_params.get("api_key", "")
        role = (websocket.query_params.get("role", "staff") or "staff").lower()
        user_id = websocket.query_params.get("user_id", "")
        if api_key != settings.api_key:
            await websocket.send_json({"error": "invalid_api_key"})
            await websocket.close(code=1008)
            return
        if role not in {"staff", "admin", "system"}:
            await websocket.send_json({"error": "role_must_be_staff_admin_system"})
            await websocket.close(code=1008)
            return
        if role != "system" and not user_id.strip():
            await websocket.send_json({"error": "user_id_required"})
            await websocket.close(code=1008)
            return

        camera_id = websocket.query_params.get("camera_id")
        animal_id = websocket.query_params.get("animal_id")
        interval_ms_raw = websocket.query_params.get("interval_ms", "1000")
        try:
            interval_ms = max(200, min(5000, int(interval_ms_raw)))
        except ValueError:
            interval_ms = 1000
        cursor = utcnow() - timedelta(seconds=5)

        while True:
            with Session(engine) as session:
                rows = _collect_live_tracks(
                    session=session,
                    camera_id=camera_id,
                    animal_id=animal_id,
                    since_ts=cursor,
                    limit=100,
                )
            if rows:
                cursor = max(row["ts"] for row in rows)
                await websocket.send_json(
                    {
                        "type": "tracks",
                        "count": len(rows),
                        "tracks": rows,
                    }
                )
            await asyncio.sleep(interval_ms / 1000.0)
    except WebSocketDisconnect:
        return


@router.websocket("/ws/staff-alerts")
async def ws_staff_alerts(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        api_key = websocket.query_params.get("api_key", "")
        role = (websocket.query_params.get("role", "staff") or "staff").lower()
        user_id = websocket.query_params.get("user_id", "")
        status = (websocket.query_params.get("status", "open") or "open").strip()
        limit_raw = websocket.query_params.get("limit", "100")
        interval_ms_raw = websocket.query_params.get("interval_ms", "1500")
        auto_eval = (websocket.query_params.get("auto_eval", "false") or "false").lower() == "true"

        if api_key != settings.api_key:
            await websocket.send_json({"error": "invalid_api_key"})
            await websocket.close(code=1008)
            return
        if role not in {"staff", "admin", "system"}:
            await websocket.send_json({"error": "role_must_be_staff_admin_system"})
            await websocket.close(code=1008)
            return
        if role != "system" and not user_id.strip():
            await websocket.send_json({"error": "user_id_required"})
            await websocket.close(code=1008)
            return

        try:
            limit = max(1, min(500, int(limit_raw)))
        except ValueError:
            limit = 100
        try:
            interval_ms = max(300, min(10000, int(interval_ms_raw)))
        except ValueError:
            interval_ms = 1500

        last_signature = ""
        while True:
            with Session(engine) as session:
                if auto_eval and role in {"admin", "system"}:
                    _evaluate_staff_alert_rules(
                        session,
                        stale_seconds=15,
                        isolation_minutes=15,
                        idle_seconds=180,
                    )
                query = select(StaffAlert)
                if status:
                    query = query.where(StaffAlert.status == status)
                query = query.order_by(StaffAlert.updated_at.desc()).limit(limit)
                rows = list(session.exec(query))
                alerts = [_serialize_staff_alert(row) for row in rows]

            signature = "|".join(f"{it['alert_id']}:{it['updated_at']}:{it['status']}" for it in alerts)
            if signature != last_signature:
                last_signature = signature
                await websocket.send_json({"type": "staff_alerts", "count": len(alerts), "alerts": alerts})
            await asyncio.sleep(interval_ms / 1000.0)
    except WebSocketDisconnect:
        return


@router.post("/system/live-tracks/ingest")
def ingest_live_tracks(
    payload: LiveTrackIngestRequest,
    _: AuthContext = Depends(require_admin_or_system),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    camera = session.get(Camera, payload.camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")
    ts = payload.ts or utcnow()

    created_tracks = 0
    created_observations = 0
    created_associations = 0

    for det in payload.detections:
        if len(det.bbox_xyxy) != 4:
            continue
        source_track_id = str(det.source_track_id)
        binding = session.exec(
            select(RealtimeTrackBinding)
            .where(RealtimeTrackBinding.camera_id == payload.camera_id)
            .where(RealtimeTrackBinding.source_track_id == source_track_id)
            .order_by(RealtimeTrackBinding.last_seen_at.desc())
            .limit(1)
        ).first()
        if binding:
            track = session.get(Track, binding.track_id)
            if track is None:
                binding = None
        if not binding:
            track = Track(
                camera_id=payload.camera_id,
                start_ts=ts,
                end_ts=ts,
                quality_score=float(det.conf),
            )
            session.add(track)
            session.flush()
            binding = RealtimeTrackBinding(
                camera_id=payload.camera_id,
                source_track_id=source_track_id,
                track_id=track.track_id,
                last_seen_at=ts,
            )
            session.add(binding)
            created_tracks += 1
        else:
            track = session.get(Track, binding.track_id)
            if not track:
                continue
            prev_q = float(track.quality_score or 0.0)
            track.quality_score = round((prev_q + float(det.conf)) / 2.0, 6)
            track.end_ts = ts
            binding.last_seen_at = ts
            session.add(binding)

        obs = TrackObservation(
            track_id=track.track_id,
            ts=ts,
            bbox=json.dumps([round(float(v), 3) for v in det.bbox_xyxy], ensure_ascii=True),
            marker_id_read=None,
            appearance_vec_ref=(
                f"ingest;source:{source_track_id};class:{det.class_id if det.class_id is not None else -1};conf:{det.conf:.6f}"
            ),
        )
        session.add(obs)
        created_observations += 1

        if det.animal_id:
            if not session.get(Animal, det.animal_id):
                continue
            assoc = session.exec(
                select(Association)
                .where(Association.track_id == track.track_id)
                .where(Association.animal_id == det.animal_id)
                .order_by(Association.created_at.desc())
                .limit(1)
            ).first()
            if assoc is None:
                assoc = Association(
                    global_track_id=det.global_track_id or f"animal:{det.animal_id}",
                    track_id=track.track_id,
                    animal_id=det.animal_id,
                    confidence=float(det.conf),
                    created_at=ts,
                )
                session.add(assoc)
                created_associations += 1

    # best-effort camera health heartbeat for ingest path
    health = session.get(CameraHealth, payload.camera_id) or CameraHealth(camera_id=payload.camera_id)
    health.status = "healthy"
    health.last_frame_at = ts
    health.updated_at = utcnow()
    session.add(health)
    session.commit()
    return {
        "ok": True,
        "camera_id": payload.camera_id,
        "created_tracks": created_tracks,
        "created_observations": created_observations,
        "created_associations": created_associations,
    }


@router.post("/system/clips/auto-generate")
def generate_auto_clips(
    window_seconds: int = Query(default=180, ge=30, le=3600),
    max_clips: int = Query(default=5, ge=1, le=100),
    per_animal_limit: int = Query(default=2, ge=1, le=10),
    _: AuthContext = Depends(require_admin_or_system),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    now = utcnow()
    since = now - timedelta(seconds=window_seconds)

    rows = list(
        session.exec(
            select(TrackObservation, Track)
            .join(Track, TrackObservation.track_id == Track.track_id)
            .where(TrackObservation.ts >= since)
            .order_by(TrackObservation.ts.desc())
            .limit(1000)
        )
    )
    created: list[dict[str, Any]] = []
    per_animal_counts: dict[str, int] = {}
    seen_tracks: set[str] = set()

    for obs, track in rows:
        if len(created) >= max_clips:
            break
        if track.track_id in seen_tracks:
            continue
        seen_tracks.add(track.track_id)

        assoc = session.exec(
            select(Association)
            .where(Association.track_id == track.track_id)
            .order_by(Association.created_at.desc())
            .limit(1)
        ).first()
        if not assoc or not assoc.animal_id:
            continue
        animal_id = assoc.animal_id
        if per_animal_counts.get(animal_id, 0) >= per_animal_limit:
            continue

        # Avoid clip spam: skip if recent auto highlight already exists for this animal.
        dup = session.exec(
            select(Event)
            .where(Event.animal_id == animal_id)
            .where(Event.type == "auto_highlight")
            .where(Event.start_ts >= obs.ts - timedelta(seconds=45))
            .where(Event.start_ts <= obs.ts + timedelta(seconds=45))
            .limit(1)
        ).first()
        if dup:
            continue

        start_ts = obs.ts - timedelta(seconds=5)
        end_ts = obs.ts + timedelta(seconds=5)
        event = Event(
            animal_id=animal_id,
            type="auto_highlight",
            severity="info",
            start_ts=start_ts,
            end_ts=end_ts,
        )
        session.add(event)
        session.flush()

        segment = _find_segment_for_camera_ts(session, camera_id=track.camera_id, ts=obs.ts)
        if segment:
            path = segment.path
            derived = segment.segment_id
        else:
            path = f"auto://{track.camera_id}/{event.event_id}.mp4"
            derived = None

        clip = Clip(
            event_id=event.event_id,
            path=path,
            start_ts=start_ts,
            end_ts=end_ts,
            derived_from_segments=derived,
        )
        session.add(clip)
        per_animal_counts[animal_id] = per_animal_counts.get(animal_id, 0) + 1
        created.append(
            {
                "clip_id": clip.clip_id,
                "event_id": event.event_id,
                "animal_id": animal_id,
                "camera_id": track.camera_id,
                "track_id": track.track_id,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "path": path,
            }
        )
    session.commit()
    return {
        "ok": True,
        "window_seconds": window_seconds,
        "created_count": len(created),
        "clips": created,
    }


@router.post("/system/alerts/evaluate")
def evaluate_staff_alerts(
    stale_seconds: int = Query(default=15, ge=5, le=600),
    isolation_minutes: int = Query(default=15, ge=1, le=180),
    idle_seconds: int = Query(default=180, ge=30, le=3600),
    _: AuthContext = Depends(require_admin_or_system),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    created = _evaluate_staff_alert_rules(
        session,
        stale_seconds=stale_seconds,
        isolation_minutes=isolation_minutes,
        idle_seconds=idle_seconds,
    )
    return {"ok": True, "created_or_touched": created}


@router.get("/staff/alerts")
def list_staff_alerts(
    status: str | None = Query(default="open"),
    limit: int = Query(default=100, ge=1, le=500),
    _: AuthContext = Depends(require_staff_or_admin),
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    query = select(StaffAlert)
    if status:
        query = query.where(StaffAlert.status == status)
    query = query.order_by(StaffAlert.updated_at.desc()).limit(limit)
    rows = list(session.exec(query))
    return [_serialize_staff_alert(row) for row in rows]


@router.post("/staff/alerts/{alert_id}/ack")
def ack_staff_alert(
    alert_id: str,
    payload: StaffAlertAckRequest,
    auth: AuthContext = Depends(require_staff_or_admin),
    session: Session = Depends(get_session),
) -> StaffAlert:
    row = session.get(StaffAlert, alert_id)
    if not row:
        raise HTTPException(status_code=404, detail="Alert not found")
    status = payload.status.strip().lower()
    if status not in {"acked", "resolved"}:
        raise HTTPException(status_code=400, detail="status must be acked or resolved")
    row.status = status
    row.acked_by = auth.user_id or row.acked_by
    row.acked_at = utcnow()
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.get("/owner/dashboard")
def get_owner_dashboard(
    owner_id: str = Query(...),
    auth: AuthContext = Depends(require_owner_or_admin),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if auth.role == "owner" and auth.user_id != owner_id:
        raise HTTPException(status_code=403, detail="owner_id must match x-user-id")

    now = utcnow()
    bookings = list(
        session.exec(
            select(Booking)
            .where(Booking.owner_id == owner_id)
            .where(Booking.start_at <= now)
            .where(Booking.end_at >= now)
            .where(Booking.status.in_(["reserved", "checked_in"]))
            .order_by(Booking.start_at.asc())
        )
    )

    cards: list[dict[str, Any]] = []
    for booking in bookings:
        pet = session.get(Animal, booking.pet_id)
        if not pet:
            continue
        latest_zone = session.exec(
            select(PetZoneEvent).where(PetZoneEvent.pet_id == pet.animal_id).order_by(PetZoneEvent.at.desc()).limit(1)
        ).first()
        zone_id = latest_zone.to_zone_id if latest_zone else booking.room_zone_id
        cam_ids = _allowed_cam_ids(session, zone_id)

        latest_log = session.exec(
            select(CareLog)
            .where(CareLog.pet_id == pet.animal_id)
            .where(CareLog.booking_id == booking.booking_id)
            .order_by(CareLog.at.desc())
            .limit(1)
        ).first()

        cards.append(
            {
                "booking_id": booking.booking_id,
                "pet_id": pet.animal_id,
                "pet_name": pet.name,
                "species": pet.species,
                "current_zone_id": zone_id,
                "cam_ids": cam_ids,
                "last_care_log": _serialize_care_log(latest_log) if latest_log else None,
                "status": booking.status,
            }
        )

    return {
        "owner_id": owner_id,
        "at": now,
        "active_booking_count": len(cards),
        "cards": cards,
    }


@router.get("/reports/bookings/{booking_id}")
def get_booking_report(
    booking_id: str,
    auth: AuthContext = Depends(require_owner_or_admin),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    booking = session.get(Booking, booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if auth.role == "owner" and auth.user_id != booking.owner_id:
        raise HTTPException(status_code=403, detail="Owner cannot access this booking")

    pet = session.get(Animal, booking.pet_id)
    if not pet:
        raise HTTPException(status_code=404, detail="Pet not found")

    logs = list(
        session.exec(
            select(CareLog)
            .where(CareLog.booking_id == booking_id)
            .order_by(CareLog.at.asc())
        )
    )
    zone_events = list(
        session.exec(
            select(PetZoneEvent)
            .where(PetZoneEvent.pet_id == booking.pet_id)
            .where(PetZoneEvent.at >= booking.start_at)
            .where(PetZoneEvent.at <= booking.end_at)
            .order_by(PetZoneEvent.at.asc())
        )
    )

    events = list(
        session.exec(
            select(Event)
            .where(Event.animal_id == booking.pet_id)
            .where(Event.start_ts >= booking.start_at)
            .where(Event.start_ts <= booking.end_at)
            .order_by(Event.start_ts.asc())
        )
    )

    clip_items: list[dict[str, Any]] = []
    if events:
        event_ids = [row.event_id for row in events]
        event_type_by_id = {row.event_id: row.type for row in events}
        clips = list(
            session.exec(
                select(Clip)
                .where(Clip.event_id.in_(event_ids))
                .order_by(Clip.start_ts.asc())
            )
        )
        clip_items = [
            {
                "clip_id": row.clip_id,
                "event_id": row.event_id,
                "event_type": event_type_by_id.get(row.event_id or "", "unknown"),
                "path": row.path,
                "start_ts": row.start_ts,
                "end_ts": row.end_ts,
            }
            for row in clips
        ]

    type_counts: dict[str, int] = {}
    for row in logs:
        type_counts[row.type] = type_counts.get(row.type, 0) + 1

    return {
        "booking": booking,
        "pet": pet,
        "summary": {
            "care_log_count": len(logs),
            "care_type_counts": type_counts,
            "zone_move_count": len(zone_events),
            "event_count": len(events),
            "clip_count": len(clip_items),
        },
        "care_logs": [_serialize_care_log(row) for row in logs],
        "zone_events": zone_events,
        "events": events,
        "clips": clip_items,
    }


@router.post("/auth/stream-token")
def create_stream_token(
    payload: StreamTokenRequest,
    auth: AuthContext = Depends(require_owner_or_admin),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if auth.role == "owner" and payload.owner_id != auth.user_id:
        _audit_stream(
            session,
            action="deny",
            auth=auth,
            owner_id=payload.owner_id,
            booking_id=payload.booking_id,
            pet_id=payload.pet_id,
            result="denied",
            reason="owner_id_mismatch",
        )
        raise HTTPException(status_code=403, detail="owner_id must match x-user-id")
    if payload.max_sessions < 1 or payload.max_sessions > 4:
        raise HTTPException(status_code=400, detail="max_sessions must be between 1 and 4")
    if not session.get(Animal, payload.pet_id):
        raise HTTPException(status_code=404, detail="Pet not found")

    booking = _active_booking(
        session=session,
        owner_id=payload.owner_id,
        booking_id=payload.booking_id,
        pet_id=payload.pet_id,
    )
    current_zone = _pet_current_zone(session, payload.pet_id, booking)
    if auth.role == "owner" and _is_play_zone(current_zone) and not settings.owner_play_live_enabled:
        _audit_stream(
            session,
            action="deny",
            auth=auth,
            owner_id=payload.owner_id,
            booking_id=payload.booking_id,
            pet_id=payload.pet_id,
            zone_id=current_zone,
            result="denied",
            reason="owner_play_live_disabled",
        )
        raise HTTPException(status_code=403, detail="PLAY live is disabled for owners. Use highlights.")
    cam_ids = _allowed_cam_ids(session, current_zone)
    if not cam_ids:
        raise HTTPException(status_code=404, detail="No cameras available for current zone")

    ttl_seconds = payload.ttl_seconds or settings.stream_token_ttl_seconds
    if ttl_seconds < 60 or ttl_seconds > 180:
        raise HTTPException(status_code=400, detail="ttl_seconds must be between 60 and 180")

    now = utcnow()
    exp = now + timedelta(seconds=ttl_seconds)
    claims = {
        "sub": payload.owner_id,
        "booking_id": payload.booking_id,
        "pet_id": payload.pet_id,
        "zone_id": current_zone,
        "cam_ids": cam_ids,
        "exp": int(exp.timestamp()),
        "max_sessions": payload.max_sessions,
        "watermark": f"{payload.booking_id}|{now.isoformat()}",
    }
    token = sign_payload(claims)

    for cam_id in cam_ids:
        row = AccessToken(
            owner_id=payload.owner_id,
            booking_id=payload.booking_id,
            pet_id=payload.pet_id,
            cam_id=cam_id,
            exp=exp,
            sessions=payload.max_sessions,
        )
        session.add(row)
    session.commit()
    for cam_id in cam_ids:
        _audit_stream(
            session,
            action="issue",
            auth=auth,
            owner_id=payload.owner_id,
            booking_id=payload.booking_id,
            pet_id=payload.pet_id,
            zone_id=current_zone,
            cam_id=cam_id,
            result="ok",
        )

    stream_urls = [f"{settings.stream_base_url}/{cam_id}?token={token}" for cam_id in cam_ids]
    return {
        "token": token,
        "exp": exp,
        "zone_id": current_zone,
        "cam_ids": cam_ids,
        "watermark": claims["watermark"],
        "stream_urls": stream_urls,
    }


@router.post("/auth/stream-verify")
def verify_stream_token(
    payload: StreamVerifyRequest,
    auth: AuthContext = Depends(require_admin_or_system),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    try:
        claims = parse_and_verify(payload.token)
    except ValueError as exc:
        _audit_stream(
            session,
            action="deny",
            auth=auth,
            result="denied",
            reason=f"invalid_token:{exc}",
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    cam_ids = claims.get("cam_ids", [])
    if payload.cam_id and payload.cam_id not in cam_ids:
        _audit_stream(
            session,
            action="deny",
            auth=auth,
            owner_id=str(claims.get("sub", "")),
            booking_id=str(claims.get("booking_id", "")),
            pet_id=str(claims.get("pet_id", "")),
            zone_id=str(claims.get("zone_id", "")),
            cam_id=payload.cam_id,
            result="denied",
            reason="cam_not_allowed",
        )
        raise HTTPException(status_code=403, detail="cam_id is not allowed by token")

    cam_for_check = payload.cam_id or (cam_ids[0] if cam_ids else None)
    if not cam_for_check:
        raise HTTPException(status_code=403, detail="Token does not contain cam_ids")

    row = session.exec(
        select(AccessToken)
        .where(AccessToken.owner_id == str(claims.get("sub", "")))
        .where(AccessToken.booking_id == str(claims.get("booking_id", "")))
        .where(AccessToken.pet_id == str(claims.get("pet_id", "")))
        .where(AccessToken.cam_id == cam_for_check)
        .where(AccessToken.exp >= utcnow())
        .order_by(AccessToken.created_at.desc())
        .limit(1)
    ).first()
    if not row:
        _audit_stream(
            session,
            action="deny",
            auth=auth,
            owner_id=str(claims.get("sub", "")),
            booking_id=str(claims.get("booking_id", "")),
            pet_id=str(claims.get("pet_id", "")),
            zone_id=str(claims.get("zone_id", "")),
            cam_id=cam_for_check,
            result="denied",
            reason="missing_persisted_token",
        )
        raise HTTPException(status_code=403, detail="No valid persisted access token found")

    # Enforce concurrent playback session limits with soft heartbeat semantics.
    viewer_session_id = (payload.viewer_session_id or "").strip() or f"legacy:{cam_for_check}"
    max_sessions = max(1, int(claims.get("max_sessions", row.sessions or 1)))
    heartbeat_window = utcnow() - timedelta(seconds=90)
    token_fingerprint = hash_session_token(payload.token)

    active_rows = list(
        session.exec(
            select(StreamPlaybackSession)
            .where(StreamPlaybackSession.token_fingerprint == token_fingerprint)
            .where(StreamPlaybackSession.active.is_(True))
            .where(StreamPlaybackSession.last_seen_at >= heartbeat_window)
            .order_by(StreamPlaybackSession.last_seen_at.desc())
            .limit(20)
        )
    )
    active_session_ids = {it.viewer_session_id for it in active_rows}
    now = utcnow()
    if viewer_session_id not in active_session_ids and len(active_session_ids) >= max_sessions:
        _audit_stream(
            session,
            action="deny",
            auth=auth,
            owner_id=str(claims.get("sub", "")),
            booking_id=str(claims.get("booking_id", "")),
            pet_id=str(claims.get("pet_id", "")),
            zone_id=str(claims.get("zone_id", "")),
            cam_id=cam_for_check,
            result="denied",
            reason=f"session_limit_exceeded:{len(active_session_ids)}/{max_sessions}",
        )
        raise HTTPException(status_code=403, detail="Session limit exceeded")

    playback_row = session.exec(
        select(StreamPlaybackSession)
        .where(StreamPlaybackSession.token_fingerprint == token_fingerprint)
        .where(StreamPlaybackSession.viewer_session_id == viewer_session_id)
        .where(StreamPlaybackSession.cam_id == cam_for_check)
        .order_by(StreamPlaybackSession.last_seen_at.desc())
        .limit(1)
    ).first()
    if playback_row is None:
        playback_row = StreamPlaybackSession(
            token_fingerprint=token_fingerprint,
            owner_id=str(claims.get("sub", "")),
            booking_id=str(claims.get("booking_id", "")),
            pet_id=str(claims.get("pet_id", "")),
            cam_id=cam_for_check,
            viewer_session_id=viewer_session_id,
            active=True,
            created_at=now,
            last_seen_at=now,
        )
    else:
        playback_row.last_seen_at = now
        playback_row.active = True
    session.add(playback_row)
    session.commit()

    _audit_stream(
        session,
        action="verify",
        auth=auth,
        owner_id=str(claims.get("sub", "")),
        booking_id=str(claims.get("booking_id", "")),
        pet_id=str(claims.get("pet_id", "")),
        zone_id=str(claims.get("zone_id", "")),
        cam_id=cam_for_check,
        result="ok",
    )

    return {
        "ok": True,
        "cam_id": cam_for_check,
        "max_sessions": max_sessions,
        "active_sessions": len(active_session_ids | {viewer_session_id}),
        "viewer_session_id": viewer_session_id,
        "watermark": str(claims.get("watermark", "")),
        "exp": int(claims.get("exp", 0)),
    }


@router.get("/auth/stream-verify-hook")
def verify_stream_hook(
    token: str = Query(...),
    cam_id: str = Query(...),
    viewer_session_id: str | None = Query(default=None),
    auth: AuthContext = Depends(require_admin_or_system),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    return verify_stream_token(
        payload=StreamVerifyRequest(token=token, cam_id=cam_id, viewer_session_id=viewer_session_id),
        auth=auth,
        session=session,
    )


@router.post("/auth/stream-session/close")
def close_stream_session(
    payload: StreamSessionCloseRequest,
    auth: AuthContext = Depends(require_admin_or_system),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    try:
        claims = parse_and_verify(payload.token)
    except ValueError as exc:
        _audit_stream(
            session,
            action="deny",
            auth=auth,
            result="denied",
            reason=f"invalid_token:{exc}",
        )
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    cam_ids = claims.get("cam_ids", [])
    cam_for_check = payload.cam_id or (cam_ids[0] if cam_ids else None)
    if not cam_for_check:
        raise HTTPException(status_code=400, detail="cam_id is required")
    if cam_for_check not in cam_ids:
        raise HTTPException(status_code=403, detail="cam_id is not allowed by token")

    token_fingerprint = hash_session_token(payload.token)
    row = session.exec(
        select(StreamPlaybackSession)
        .where(StreamPlaybackSession.token_fingerprint == token_fingerprint)
        .where(StreamPlaybackSession.cam_id == cam_for_check)
        .where(StreamPlaybackSession.viewer_session_id == payload.viewer_session_id)
        .order_by(StreamPlaybackSession.last_seen_at.desc())
        .limit(1)
    ).first()
    if not row:
        return {"ok": True, "closed": False}
    row.active = False
    row.last_seen_at = utcnow()
    session.add(row)
    session.commit()
    _audit_stream(
        session,
        action="verify",
        auth=auth,
        owner_id=str(claims.get("sub", "")),
        booking_id=str(claims.get("booking_id", "")),
        pet_id=str(claims.get("pet_id", "")),
        zone_id=str(claims.get("zone_id", "")),
        cam_id=cam_for_check,
        result="ok",
        reason=f"session_closed:{payload.viewer_session_id}",
    )
    return {"ok": True, "closed": True, "viewer_session_id": payload.viewer_session_id}


@router.get("/admin/stream-audit-logs")
def list_stream_audit_logs(
    limit: int = Query(default=100, ge=1, le=1000),
    result: str | None = Query(default=None),
    action: str | None = Query(default=None),
    _auth: AuthContext = Depends(require_staff_or_admin),
    session: Session = Depends(get_session),
) -> list[StreamAuditLog]:
    query = select(StreamAuditLog)
    if result:
        query = query.where(StreamAuditLog.result == result)
    if action:
        query = query.where(StreamAuditLog.action == action)
    query = query.order_by(StreamAuditLog.at.desc()).limit(limit)
    return list(session.exec(query))


@router.post("/collars")
def create_collar(payload: CollarCreate, session: Session = Depends(get_session)) -> Collar:
    if not session.get(Animal, payload.animal_id):
        raise HTTPException(status_code=404, detail="Animal not found")

    data = payload.model_dump()
    data["start_ts"] = payload.start_ts or utcnow()
    collar = Collar(**data)
    session.add(collar)
    session.commit()
    session.refresh(collar)
    return collar


@router.get("/collars")
def list_collars(
    animal_id: str | None = Query(default=None), session: Session = Depends(get_session)
) -> list[Collar]:
    query = select(Collar)
    if animal_id:
        query = query.where(Collar.animal_id == animal_id)
    return list(session.exec(query.order_by(Collar.start_ts.desc())))


@router.post("/tracks")
def create_track(payload: TrackCreate, session: Session = Depends(get_session)) -> Track:
    if not session.get(Camera, payload.camera_id):
        raise HTTPException(status_code=404, detail="Camera not found")

    data = payload.model_dump()
    data["start_ts"] = payload.start_ts or utcnow()
    track = Track(**data)
    session.add(track)
    session.commit()
    session.refresh(track)
    return track


@router.get("/tracks")
def list_tracks(
    camera_id: str | None = Query(default=None), session: Session = Depends(get_session)
) -> list[Track]:
    query = select(Track)
    if camera_id:
        query = query.where(Track.camera_id == camera_id)
    return list(session.exec(query.order_by(Track.start_ts.desc())))


@router.post("/tracks/{track_id}/observations")
def create_track_observation(
    track_id: str,
    payload: TrackObservationCreate,
    session: Session = Depends(get_session),
) -> TrackObservation:
    if not session.get(Track, track_id):
        raise HTTPException(status_code=404, detail="Track not found")

    data = payload.model_dump()
    data["track_id"] = track_id
    data["ts"] = payload.ts or utcnow()
    observation = TrackObservation(**data)
    session.add(observation)
    session.commit()
    session.refresh(observation)
    return observation


@router.get("/tracks/{track_id}/observations")
def list_track_observations(
    track_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> list[TrackObservation]:
    if not session.get(Track, track_id):
        raise HTTPException(status_code=404, detail="Track not found")

    query = (
        select(TrackObservation)
        .where(TrackObservation.track_id == track_id)
        .order_by(TrackObservation.ts.desc())
        .limit(limit)
    )
    return list(session.exec(query))


@router.post("/associations")
def create_association(
    payload: AssociationCreate, session: Session = Depends(get_session)
) -> Association:
    if not session.get(Track, payload.track_id):
        raise HTTPException(status_code=404, detail="Track not found")
    if not session.get(Animal, payload.animal_id):
        raise HTTPException(status_code=404, detail="Animal not found")

    association = Association(**payload.model_dump())
    session.add(association)
    session.commit()
    session.refresh(association)
    return association


@router.get("/associations")
def list_associations(
    animal_id: str | None = Query(default=None),
    global_track_id: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> list[Association]:
    query = select(Association)
    if animal_id:
        query = query.where(Association.animal_id == animal_id)
    if global_track_id:
        query = query.where(Association.global_track_id == global_track_id)
    return list(session.exec(query.order_by(Association.created_at.desc())))


@router.get("/identities/{global_track_id}")
def get_identity(global_track_id: str, session: Session = Depends(get_session)) -> GlobalIdentity:
    row = session.get(GlobalIdentity, global_track_id)
    if not row:
        raise HTTPException(status_code=404, detail="Identity not found")
    return row


@router.put("/identities/{global_track_id}/animal")
def upsert_identity_animal(
    global_track_id: str,
    payload: IdentityUpsert,
    session: Session = Depends(get_session),
) -> GlobalIdentity:
    if payload.state not in {"unknown", "confirmed"}:
        raise HTTPException(status_code=400, detail="state must be one of: unknown, confirmed")
    if payload.animal_id and not session.get(Animal, payload.animal_id):
        raise HTTPException(status_code=404, detail="Animal not found")

    row = session.get(GlobalIdentity, global_track_id)
    if not row:
        row = GlobalIdentity(global_track_id=global_track_id)
    row.animal_id = payload.animal_id
    row.state = payload.state
    row.source = payload.source
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@router.post("/events")
def create_event(payload: EventCreate, session: Session = Depends(get_session)) -> Event:
    if not session.get(Animal, payload.animal_id):
        raise HTTPException(status_code=404, detail="Animal not found")

    data = payload.model_dump()
    data["start_ts"] = payload.start_ts or utcnow()
    event = Event(**data)
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


@router.get("/events")
def list_events(
    animal_id: str | None = Query(default=None), session: Session = Depends(get_session)
) -> list[Event]:
    query = select(Event)
    if animal_id:
        query = query.where(Event.animal_id == animal_id)
    query = query.order_by(Event.start_ts.desc())
    return list(session.exec(query))


@router.post("/positions")
def create_position(payload: PositionCreate, session: Session = Depends(get_session)) -> Position:
    if not session.get(Animal, payload.animal_id):
        raise HTTPException(status_code=404, detail="Animal not found")

    data = payload.model_dump()
    data["ts"] = payload.ts or utcnow()
    position = Position(**data)
    session.add(position)
    session.commit()
    session.refresh(position)
    return position


@router.post("/media-segments")
def create_media_segment(
    payload: MediaSegmentCreate, session: Session = Depends(get_session)
) -> MediaSegment:
    if payload.camera_id and not session.get(Camera, payload.camera_id):
        raise HTTPException(status_code=404, detail="Camera not found")

    data = payload.model_dump()
    data["start_ts"] = payload.start_ts or utcnow()
    segment = MediaSegment(**data)
    session.add(segment)
    session.commit()
    session.refresh(segment)
    return segment


@router.get("/media-segments")
def list_media_segments(
    camera_id: str | None = Query(default=None), session: Session = Depends(get_session)
) -> list[MediaSegment]:
    query = select(MediaSegment)
    if camera_id:
        query = query.where(MediaSegment.camera_id == camera_id)
    return list(session.exec(query.order_by(MediaSegment.start_ts.desc())))


@router.post("/clips")
def create_clip(payload: ClipCreate, session: Session = Depends(get_session)) -> Clip:
    if payload.event_id and not session.get(Event, payload.event_id):
        raise HTTPException(status_code=404, detail="Event not found")

    data = payload.model_dump()
    data["start_ts"] = payload.start_ts or utcnow()
    clip = Clip(**data)
    session.add(clip)
    session.commit()
    session.refresh(clip)
    return clip


@router.get("/clips")
def list_clips(
    event_id: str | None = Query(default=None), session: Session = Depends(get_session)
) -> list[Clip]:
    query = select(Clip)
    if event_id:
        query = query.where(Clip.event_id == event_id)
    return list(session.exec(query.order_by(Clip.start_ts.desc())))


@router.get("/clips/{clip_id}/playback-url")
def get_clip_playback_url(
    clip_id: str,
    auth: AuthContext = Depends(require_owner_or_admin),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    clip = session.get(Clip, clip_id)
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    if not clip.event_id:
        raise HTTPException(status_code=400, detail="Clip is not linked to an event")
    event = session.get(Event, clip.event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    animal = session.get(Animal, event.animal_id)
    if not animal:
        raise HTTPException(status_code=404, detail="Animal not found")
    if auth.role == "owner" and auth.user_id != animal.owner_id:
        raise HTTPException(status_code=403, detail="Owner cannot access this clip")

    path = clip.path or ""
    if path.startswith("http://") or path.startswith("https://"):
        return {"clip_id": clip_id, "playback_url": path, "source": "direct"}

    if path.startswith("auto://"):
        # auto://{camera_id}/{event_id}.mp4
        rest = path[len("auto://") :]
        camera_id = rest.split("/", 1)[0].strip()
        if not camera_id:
            raise HTTPException(status_code=400, detail="Invalid auto clip path")
        return {
            "clip_id": clip_id,
            "playback_url": f"{settings.stream_base_url}/{camera_id}",
            "source": "camera_live_fallback",
            "camera_id": camera_id,
        }

    return {
        "clip_id": clip_id,
        "playback_url": None,
        "source": "unresolved_local_path",
        "path": path,
    }


@router.get("/animals/{animal_id}/timeline")
def get_animal_timeline(
    animal_id: str,
    from_ts: datetime | None = Query(default=None),
    to_ts: datetime | None = Query(default=None),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    animal = session.get(Animal, animal_id)
    if not animal:
        raise HTTPException(status_code=404, detail="Animal not found")

    events_query = select(Event).where(Event.animal_id == animal_id)
    positions_query = select(Position).where(Position.animal_id == animal_id)
    analyses_query = select(VideoAnalysis).where(VideoAnalysis.animal_id == animal_id)

    if from_ts:
        events_query = events_query.where(Event.start_ts >= from_ts)
        positions_query = positions_query.where(Position.ts >= from_ts)
        analyses_query = analyses_query.where(VideoAnalysis.created_at >= from_ts)
    if to_ts:
        events_query = events_query.where(Event.start_ts <= to_ts)
        positions_query = positions_query.where(Position.ts <= to_ts)
        analyses_query = analyses_query.where(VideoAnalysis.created_at <= to_ts)

    events = list(session.exec(events_query.order_by(Event.start_ts.desc()).limit(200)))
    positions = list(session.exec(positions_query.order_by(Position.ts.desc()).limit(200)))
    analyses = list(session.exec(analyses_query.order_by(VideoAnalysis.created_at.desc()).limit(50)))

    timeline = [
        *[_timeline_item("event", row.start_ts, row) for row in events],
        *[_timeline_item("position", row.ts, row) for row in positions],
        *[_timeline_item("video_analysis", row.created_at, row) for row in analyses],
    ]
    timeline.sort(key=lambda item: item["ts"], reverse=True)

    return {
        "animal": animal,
        "events": events,
        "positions": positions,
        "video_analyses": analyses,
        "timeline": timeline,
    }


@router.post("/videos/process")
async def process_video(
    file: UploadFile = File(...),
    animal_id: str | None = Form(default=None),
    camera_id: str | None = Form(default=None),
    event_type: str | None = Form(default=None),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="File name is required")

    if not file.content_type or not file.content_type.startswith("video/"):
        raise HTTPException(status_code=400, detail="Only video files are allowed")

    if animal_id and not session.get(Animal, animal_id):
        raise HTTPException(status_code=404, detail="Animal not found")
    if camera_id and not session.get(Camera, camera_id):
        raise HTTPException(status_code=404, detail="Camera not found")

    video_id, video_path = await save_upload(file)
    analysis = analyze_video(video_path)
    encrypted_path = store_encrypted_analysis(video_id, analysis)

    now = utcnow()
    analysis_row = VideoAnalysis(
        video_id=video_id,
        animal_id=animal_id,
        camera_id=camera_id,
        filename=file.filename,
        uploaded_path=str(video_path),
        encrypted_analysis_path=str(encrypted_path),
        duration_seconds=analysis["duration_seconds"],
        fps=analysis["fps"],
        total_frames=analysis["total_frames"],
        sampled_frames=analysis["sampled_frames"],
        avg_motion_score=analysis["avg_motion_score"],
        avg_brightness=analysis["avg_brightness"],
        created_at=now,
    )
    session.add(analysis_row)

    segment = MediaSegment(
        camera_id=camera_id,
        start_ts=now,
        end_ts=now + timedelta(seconds=analysis["duration_seconds"]),
        path=str(video_path),
        codec=file.content_type,
    )
    session.add(segment)

    created_event: Event | None = None
    if event_type and animal_id:
        created_event = Event(
            animal_id=animal_id,
            type=event_type,
            severity="info",
            start_ts=now,
            end_ts=now + timedelta(seconds=analysis["duration_seconds"]),
        )
        session.add(created_event)

    session.commit()
    if created_event:
        session.refresh(created_event)

    return {
        "video_id": video_id,
        "filename": file.filename,
        "analysis_encrypted_path": str(encrypted_path),
        "event_id": created_event.event_id if created_event else None,
        "summary": {
            "duration_seconds": analysis["duration_seconds"],
            "fps": analysis["fps"],
            "total_frames": analysis["total_frames"],
            "sampled_frames": analysis["sampled_frames"],
            "avg_motion_score": analysis["avg_motion_score"],
            "avg_brightness": analysis["avg_brightness"],
        },
    }


@router.post("/videos/track")
async def track_video(
    file: UploadFile = File(...),
    camera_id: str = Form(...),
    animal_id: str | None = Form(default=None),
    conf_threshold: float = Form(default=0.25),
    iou_threshold: float = Form(default=0.45),
    frame_stride: int = Form(default=1),
    max_frames: int = Form(default=0),
    classes_csv: str = Form(default="15,16"),
    global_id_mode: str = Form(default="animal"),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if not file.filename:
        raise HTTPException(status_code=400, detail="File name is required")

    if not file.content_type or not file.content_type.startswith("video/"):
        raise HTTPException(status_code=400, detail="Only video files are allowed")

    camera = session.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found")

    if animal_id and not session.get(Animal, animal_id):
        raise HTTPException(status_code=404, detail="Animal not found")

    if frame_stride < 1:
        raise HTTPException(status_code=400, detail="frame_stride must be >= 1")
    if global_id_mode not in {"animal", "camera_track"}:
        raise HTTPException(status_code=400, detail="global_id_mode must be one of: animal, camera_track")

    classes: list[int] | None = None
    classes_csv = classes_csv.strip()
    if classes_csv:
        try:
            classes = [int(value.strip()) for value in classes_csv.split(",") if value.strip()]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="classes_csv must be comma-separated integers") from exc

    video_id, video_path = await save_upload(file)

    try:
        tracking = track_video_with_yolo_deepsort(
            video_path=video_path,
            conf_threshold=conf_threshold,
            iou_threshold=iou_threshold,
            frame_stride=frame_stride,
            max_frames=max_frames,
            classes=classes,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    now = utcnow()
    analysis_row = VideoAnalysis(
        video_id=video_id,
        animal_id=animal_id,
        camera_id=camera_id,
        filename=file.filename,
        uploaded_path=str(video_path),
        encrypted_analysis_path="",
        duration_seconds=float(tracking["duration_seconds"]),
        fps=float(tracking["fps"]),
        total_frames=int(tracking["total_frames"]),
        sampled_frames=int(tracking["processed_frames"]),
        avg_motion_score=0.0,
        avg_brightness=0.0,
        created_at=now,
    )
    session.add(analysis_row)

    segment = MediaSegment(
        camera_id=camera_id,
        start_ts=now,
        end_ts=now + timedelta(seconds=float(tracking["duration_seconds"])),
        path=str(video_path),
        codec=file.content_type,
    )
    session.add(segment)

    persisted_tracks = 0
    persisted_observations = 0
    persisted_associations = 0
    for source_track in tracking["tracks"]:
        observations = source_track["observations"]
        if not observations:
            continue

        start_ts = now + timedelta(seconds=float(observations[0]["ts_seconds"]))
        end_ts = now + timedelta(seconds=float(observations[-1]["ts_seconds"]))
        track_row = Track(
            camera_id=camera_id,
            start_ts=start_ts,
            end_ts=end_ts,
            quality_score=float(source_track["avg_confidence"]),
        )
        session.add(track_row)
        session.flush()
        persisted_tracks += 1

        for obs in observations:
            bbox_json = json.dumps([round(float(v), 3) for v in obs["bbox_xyxy"]], ensure_ascii=True)
            appearance_ref = f"class:{int(obs['class_id'])};conf:{float(obs['conf']):.6f}"
            row = TrackObservation(
                track_id=track_row.track_id,
                ts=now + timedelta(seconds=float(obs["ts_seconds"])),
                bbox=bbox_json,
                marker_id_read=None,
                appearance_vec_ref=appearance_ref,
            )
            session.add(row)
            persisted_observations += 1

        if animal_id:
            association = Association(
                global_track_id=_build_global_track_id(
                    global_id_mode,
                    camera_id,
                    int(source_track["source_track_id"]),
                    animal_id,
                ),
                track_id=track_row.track_id,
                animal_id=animal_id,
                confidence=float(source_track["avg_confidence"]),
            )
            session.add(association)
            persisted_associations += 1

    session.commit()

    return {
        "video_id": video_id,
        "camera_id": camera_id,
        "animal_id": animal_id,
        "tracking_summary": {
            "fps": tracking["fps"],
            "total_frames": tracking["total_frames"],
            "processed_frames": tracking["processed_frames"],
            "duration_seconds": tracking["duration_seconds"],
            "total_detections": tracking["total_detections"],
            "track_count": tracking["track_count"],
        },
        "db_persisted": {
            "tracks": persisted_tracks,
            "observations": persisted_observations,
            "associations": persisted_associations,
        },
    }


@router.get("/videos/{video_id}/analysis")
def get_analysis(video_id: str, session: Session = Depends(get_session)) -> dict[str, Any]:
    row = session.get(VideoAnalysis, video_id)
    if not row:
        raise HTTPException(status_code=404, detail="Analysis metadata not found")

    try:
        decrypted = read_encrypted_analysis(video_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Encrypted analysis file not found") from exc

    return {
        "metadata": row,
        "analysis": decrypted,
    }


@router.post("/exports/global-track/{global_track_id}")
def export_global_track(
    global_track_id: str,
    payload: ExportRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    try:
        excerpts, summary = build_export_plan(
            session=session,
            global_track_id=global_track_id,
            padding_seconds=payload.padding_seconds,
            merge_gap_seconds=payload.merge_gap_seconds,
            min_duration_seconds=payload.min_duration_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    export_id, manifest_path = save_manifest(
        global_track_id=global_track_id,
        summary=summary,
        excerpts=excerpts,
    )

    video_path: str | None = None
    render_error: str | None = None
    if payload.render_video:
        try:
            video_path = str(render_export_video(export_id=export_id, excerpts=excerpts))
        except Exception as exc:
            render_error = str(exc)

    return {
        "export_id": export_id,
        "global_track_id": global_track_id,
        "summary": summary,
        "manifest_path": str(manifest_path),
        "video_path": video_path,
        "render_error": render_error,
    }


@router.post("/exports/global-track/{global_track_id}/highlights")
def export_global_track_highlights(
    global_track_id: str,
    payload: HighlightRequest,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    try:
        excerpts, summary = build_export_plan(
            session=session,
            global_track_id=global_track_id,
            padding_seconds=payload.padding_seconds,
            merge_gap_seconds=payload.merge_gap_seconds,
            min_duration_seconds=payload.min_duration_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    highlights = build_highlight_plan(
        excerpts=excerpts,
        target_seconds=payload.target_seconds,
        per_clip_seconds=payload.per_clip_seconds,
    )
    if not highlights:
        raise HTTPException(status_code=404, detail="No highlight excerpts available")

    summary["mode"] = "highlights"
    summary["target_seconds"] = payload.target_seconds
    summary["per_clip_seconds"] = payload.per_clip_seconds
    summary["highlight_excerpt_count"] = len(highlights)

    export_id, manifest_path = save_manifest(
        global_track_id=global_track_id,
        summary=summary,
        excerpts=highlights,
    )

    video_path: str | None = None
    render_error: str | None = None
    try:
        video_path = str(render_export_video(export_id=export_id, excerpts=highlights))
    except Exception as exc:
        render_error = str(exc)

    return {
        "export_id": export_id,
        "global_track_id": global_track_id,
        "summary": summary,
        "manifest_path": str(manifest_path),
        "video_path": video_path,
        "render_error": render_error,
    }


@router.post("/exports/global-track/{global_track_id}/jobs")
def create_export_job(
    global_track_id: str,
    payload: ExportJobCreate,
    session: Session = Depends(get_session),
) -> ExportJob:
    mode = payload.mode.strip().lower()
    if mode not in {"full", "highlights"}:
        raise HTTPException(status_code=400, detail="mode must be one of: full, highlights")

    if payload.max_retries < 0:
        raise HTTPException(status_code=400, detail="max_retries must be >= 0")

    payload_data = payload.model_dump(exclude={"mode", "dedupe", "max_retries"})
    payload_json = json.dumps(payload_data, ensure_ascii=True)

    if payload.dedupe:
        existing_query = (
            select(ExportJob)
            .where(ExportJob.global_track_id == global_track_id)
            .where(ExportJob.mode == mode)
            .where(ExportJob.payload_json == payload_json)
            .where(ExportJob.status.in_(["pending", "running"]))
            .where(ExportJob.canceled_at.is_(None))
            .order_by(ExportJob.created_at.desc())
            .limit(1)
        )
        existing = session.exec(existing_query).first()
        if existing:
            return existing

    job = ExportJob(
        global_track_id=global_track_id,
        mode=mode,
        status="pending",
        payload_json=payload_json,
        max_retries=payload.max_retries,
        retry_count=0,
        next_run_at=utcnow(),
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


@router.get("/exports/jobs/{job_id}")
def get_export_job(job_id: str, session: Session = Depends(get_session)) -> ExportJob:
    job = session.get(ExportJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Export job not found")
    return job


@router.post("/exports/jobs/{job_id}/cancel")
def cancel_export_job(job_id: str, session: Session = Depends(get_session)) -> ExportJob:
    job = session.get(ExportJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Export job not found")
    if job.status in {"done", "failed", "canceled"}:
        return job

    job.status = "canceled"
    job.canceled_at = utcnow()
    job.next_run_at = None
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


@router.post("/exports/jobs/{job_id}/retry")
def retry_export_job(job_id: str, session: Session = Depends(get_session)) -> ExportJob:
    job = session.get(ExportJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Export job not found")
    if job.status not in {"failed", "canceled"}:
        raise HTTPException(status_code=400, detail="Only failed/canceled jobs can be retried")

    job.status = "pending"
    job.error_message = None
    job.started_at = None
    job.finished_at = None
    job.canceled_at = None
    job.next_run_at = utcnow()
    job.retry_count = 0
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


@router.get("/exports/{export_id}")
def get_export(
    export_id: str,
    download: str | None = Query(default=None),
) -> Any:
    manifest_path = manifest_path_for_export(export_id)
    video_path = video_path_for_export(export_id)

    if download:
        kind = download.strip().lower()
        if kind == "manifest":
            if not manifest_path.exists():
                raise HTTPException(status_code=404, detail="Manifest not found")
            return FileResponse(
                path=str(manifest_path),
                media_type="application/json",
                filename=f"{export_id}.json",
            )
        if kind == "video":
            if not video_path.exists():
                raise HTTPException(status_code=404, detail="Video not found")
            return FileResponse(
                path=str(video_path),
                media_type="video/mp4",
                filename=f"{export_id}.mp4",
            )
        raise HTTPException(status_code=400, detail="download must be one of: manifest, video")

    manifest_data = None
    if manifest_path.exists():
        try:
            manifest_data = load_manifest(export_id)
        except Exception:
            manifest_data = None

    return {
        "export_id": export_id,
        "manifest_path": str(manifest_path) if manifest_path.exists() else None,
        "video_path": str(video_path) if video_path.exists() else None,
        "manifest": manifest_data,
    }
