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
- `WS /api/v1/ws/live-tracks`: 실시간 트랙 스트림
- `GET /api/v1/live/cameras/{camera_id}/playback-url`: 프론트 오버레이용 재생 URL 조회

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
