import pytest

from app.db import session as db_session


def test_create_engine_sqlite_url() -> None:
    original = db_session.settings.database_url
    try:
        db_session.settings.database_url = "sqlite:///./tmp-test.db"
        engine = db_session._create_engine()
        assert engine.url.drivername.startswith("sqlite")
    finally:
        db_session.settings.database_url = original


def test_create_engine_postgres_url() -> None:
    pytest.importorskip("psycopg")
    original = db_session.settings.database_url
    try:
        db_session.settings.database_url = "postgresql+psycopg://u:p@localhost:5432/db"
        engine = db_session._create_engine()
        assert engine.url.drivername.startswith("postgresql")
    finally:
        db_session.settings.database_url = original
