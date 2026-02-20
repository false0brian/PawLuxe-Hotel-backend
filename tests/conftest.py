import sys
from pathlib import Path

import pytest
from sqlmodel import SQLModel

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import engine, init_db


def _truncate_all_tables() -> None:
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        for table in reversed(SQLModel.metadata.sorted_tables):
            conn.exec_driver_sql(f'DELETE FROM "{table.name}"')
        if engine.dialect.name == "sqlite":
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")


@pytest.fixture(autouse=True)
def isolate_database_state() -> None:
    init_db()
    _truncate_all_tables()
    yield
    _truncate_all_tables()
