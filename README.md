# PawLuxe Hotel Backend

FastAPI 기반 반려동물 호텔 운영 백엔드입니다.

## 핵심 기능
- 예약/체크인 기반 펫 상태 조회
- 스트림 토큰 발급/검증 (`/auth/stream-token`, `/auth/stream-verify`)
- 역할/세션 기반 접근제어 (`owner`, `staff`, `admin`, `system`)
- 스태프 존 이동/케어로그
- 카메라 헬스 모니터링/감사로그
- RTSP 트래킹 워커 및 Export 파이프라인
- 라이브 트랙 조회/스트리밍 (객체 bbox 오버레이용)
- 시스템 ingest/알림 파이프라인
- 스트림 동시 세션 제한 검증

## 로컬 실행
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8001
```

## 필수 환경변수
- `API_KEY`
- `DATABASE_URL`
- `STREAM_SIGNING_KEY`
- `CORS_ALLOW_ORIGINS`

예시:
```env
API_KEY=change-me
DATABASE_URL=sqlite:///./pawluxe.db
STREAM_SIGNING_KEY=replace-with-strong-key
STREAM_TOKEN_TTL_SECONDS=120
STREAM_BASE_URL=https://stream.example.com/live
OWNER_PLAY_LIVE_ENABLED=false
CORS_ALLOW_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
```

프론트가 `:4000`이면 아래처럼 맞추는 것을 권장합니다.
```env
CORS_ALLOW_ORIGINS=http://localhost:4000,http://127.0.0.1:4000
```

## 테스트
```bash
.venv/bin/pytest -q
```

## 멀티 카메라 스모크 테스트
`scripts/multi_camera_smoke.sh`는 카메라/예약/권한/헬스/라이브 트랙 API를 한 번에 점검합니다.
```bash
API_BASE=http://localhost:8001/api/v1 \
API_KEY=change-me \
./scripts/multi_camera_smoke.sh
```

## 라이브 트랙/재생 API
- `GET /api/v1/live/tracks/latest`: 최신 트랙(bbox/zone/animal) 조회
- `GET /api/v1/live/zones/summary`: 존 단위 실시간 관측 집계
- `GET /api/v1/live/zones/heatmap`: 존 단위 시간 버킷 히트맵 집계
- `WS /api/v1/ws/live-tracks`: 실시간 트랙 스트림
- `GET /api/v1/live/cameras/{camera_id}/playback-url`: 프론트 오버레이용 재생 URL 조회
- `POST /api/v1/system/live-tracks/ingest`: 외부 추적기 입력을 실시간 적재

## 운영 알림 API
- `POST /api/v1/system/alerts/evaluate`: 카메라/격리이동/트래킹공백 규칙 평가
- `GET /api/v1/staff/alerts`: 스태프 알림 목록 조회
- `POST /api/v1/staff/alerts/{alert_id}/ack`: 알림 ack/resolved 처리
- `WS /api/v1/ws/staff-alerts`: 스태프 알림 실시간 스트림
- `GET /api/v1/staff/today-board`의 `alerts_summary`로 운영 KPI(open/critical/avg_ack) 확인

## 자동 클립 API
- `POST /api/v1/system/clips/auto-generate`: 최근 트랙 관측 기반 auto_highlight 이벤트/클립 생성
- `GET /api/v1/reports/bookings/{booking_id}`의 `clips[].event_type`으로 auto/manual 구분 가능
- `GET /api/v1/clips/{clip_id}/playback-url`: 클립 재생 URL 해석 (auto clip은 camera live fallback)

## 스트림 보안
- `POST /api/v1/auth/stream-verify`는 `viewer_session_id` 기반 동시 세션 제한을 강제합니다.
- `POST /api/v1/auth/stream-session/close`로 특정 viewer 세션을 명시 종료할 수 있습니다.
- 토큰 응답에 `watermark` 문자열이 포함됩니다.

## Docker 스택
```bash
docker compose -f docker-compose.stack.yml up -d --build
```

## 주요 디렉토리
- `app/`: API/도메인 로직
- `tests/`: 테스트
- `deploy/`: systemd/docker 배포 파일
- `scripts/`: 운영 스크립트

## 라이선스
Internal / Proprietary
