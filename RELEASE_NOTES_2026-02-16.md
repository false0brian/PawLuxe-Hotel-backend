# Release Notes (2026-02-16)

## 요약
이번 고도화는 PostgreSQL 전환 기반, Identity/Export 안정화, RTSP 워커 운영성 강화에 초점을 맞췄습니다.

## 사용자 영향
- Identity 관리 API 추가
  - `GET /api/v1/identities/{global_track_id}`
  - `PUT /api/v1/identities/{global_track_id}/animal`
  - `global_track_id -> animal_id`를 확정/미확정 상태로 조회 및 수정 가능
- Export Job 제어 기능 강화
  - `dedupe` 지원으로 동일 조건의 `pending/running` 잡 중복 생성 방지
  - `timeout_seconds`, `max_retries` 지원
  - `POST /api/v1/exports/jobs/{job_id}/cancel` 추가
  - `POST /api/v1/exports/jobs/{job_id}/retry` 추가
- Export 렌더링 안정성 개선
  - ffmpeg 타임아웃 전달
  - 렌더링 임시 디렉토리 자동 정리

## 운영 영향
- DB 엔진 확장
  - `psycopg[binary]` 의존성 추가
  - `DATABASE_URL` PostgreSQL 설정 지원
  - `docker-compose.postgres.yml` 제공
- 워커 동시성/복구성 개선
  - PostgreSQL에서 `FOR UPDATE SKIP LOCKED`로 job claim
  - 실패 시 지수 백오프 재시도 (`retry_count`, `next_run_at`)
  - 취소된 잡/미도래 잡 스킵
- RTSP 추적 워커 튜닝 옵션 추가
  - `--observation-stride`
  - `--min-track-observations`
  - `--track-stale-frames`
- 하위호환 보완
  - 워커 `Namespace` 직접 호출 시 신규 인자 누락되어도 기본값으로 동작

## 데이터/스키마 변경
- 신규 테이블
  - `global_identities`
- `export_jobs` 확장 컬럼
  - `retry_count`, `max_retries`, `next_run_at`, `canceled_at`
  - 상태값 `canceled` 추가
- SQLite 환경
  - `app/db/session.py` 경량 forward-only migration 포함 (`export_jobs` 보강)

## 마이그레이션 포인트
- PostgreSQL 전환 시 `.env`의 `DATABASE_URL` 변경 필요
- 기존 SQLite 데이터는 자동 이전되지 않음
- 수동 이관 스크립트 제공
  - `scripts/migrate_sqlite_to_postgres.py`

## 검증 상태
- 전체 테스트: `20 passed, 1 skipped`
- 보강 테스트 범위
  - DB 엔진 생성 분기 (SQLite/Postgres)
  - Identity API 흐름
  - Export dedupe/cancel/retry/timeout/backoff
  - 렌더 임시 디렉토리 정리
  - RTSP e2e 회귀 보호
