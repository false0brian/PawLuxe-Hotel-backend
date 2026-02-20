from collections.abc import Generator

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from app.core.config import settings
from app.db import models  # noqa: F401


def _create_engine():
    url = settings.database_url
    kwargs = {"echo": False, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


engine = _create_engine()


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _apply_lightweight_migrations()


def _apply_lightweight_migrations() -> None:
    # Lightweight forward-only migrations for local SQLite where Alembic is not configured.
    if not settings.database_url.startswith("sqlite"):
        return

    inspector = sa_inspect(engine)
    tables = set(inspector.get_table_names())
    if "export_jobs" not in tables:
        return

    cols = {col["name"] for col in inspector.get_columns("export_jobs")}
    stmts: list[str] = []
    if "retry_count" not in cols:
        stmts.append("ALTER TABLE export_jobs ADD COLUMN retry_count INTEGER DEFAULT 0")
    if "max_retries" not in cols:
        stmts.append("ALTER TABLE export_jobs ADD COLUMN max_retries INTEGER DEFAULT 3")
    if "next_run_at" not in cols:
        stmts.append("ALTER TABLE export_jobs ADD COLUMN next_run_at DATETIME")
    if "canceled_at" not in cols:
        stmts.append("ALTER TABLE export_jobs ADD COLUMN canceled_at DATETIME")

    if stmts:
        with engine.begin() as conn:
            for stmt in stmts:
                conn.execute(text(stmt))

    # care_logs.value_json was introduced after initial MVP schema.
    if "care_logs" in tables:
        care_cols = {col["name"] for col in inspector.get_columns("care_logs")}
        if "value_json" not in care_cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE care_logs ADD COLUMN value_json TEXT"))


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
