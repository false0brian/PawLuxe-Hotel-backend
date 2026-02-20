#!/usr/bin/env python3
"""Migrate PawLuxe data from SQLite to PostgreSQL.

Examples:
  python scripts/migrate_sqlite_to_postgres.py \
    --source sqlite:///./pawluxe.db \
    --target postgresql+psycopg://pawluxe:pawluxe@127.0.0.1:5432/pawluxe

  python scripts/migrate_sqlite_to_postgres.py \
    --source sqlite:///./pawluxe.db \
    --target postgresql+psycopg://pawluxe:pawluxe@127.0.0.1:5432/pawluxe \
    --on-conflict replace
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable

from sqlalchemy import inspect as sa_inspect
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import (
    Animal,
    Association,
    Camera,
    Clip,
    Collar,
    Event,
    ExportJob,
    GlobalTrackProfile,
    MediaSegment,
    Position,
    Track,
    TrackObservation,
    VideoAnalysis,
)


MODEL_ORDER = [
    Animal,
    Camera,
    Collar,
    Track,
    TrackObservation,
    Position,
    GlobalTrackProfile,
    Association,
    Event,
    MediaSegment,
    Clip,
    VideoAnalysis,
    ExportJob,
]


def _pk_column_name(model: type[SQLModel]) -> str:
    mapper = sa_inspect(model)
    if len(mapper.primary_key) != 1:
        raise RuntimeError(f"Expected single-column PK for {model.__name__}")
    return mapper.primary_key[0].name


def _iter_rows(source: Session, model: type[SQLModel]) -> Iterable[SQLModel]:
    return source.exec(select(model))


def _migrate_table(
    source: Session,
    target: Session,
    model: type[SQLModel],
    on_conflict: str,
    dry_run: bool,
) -> tuple[int, int, int]:
    inserted = 0
    replaced = 0
    skipped = 0

    pk_name = _pk_column_name(model)

    for row in _iter_rows(source, model):
        data = row.model_dump()
        pk_value = data[pk_name]
        existing = target.get(model, pk_value)

        if existing is not None:
            if on_conflict == "skip":
                skipped += 1
                continue
            if on_conflict == "replace":
                replaced += 1
                if not dry_run:
                    target.delete(existing)
            else:
                raise RuntimeError(f"Unsupported on_conflict mode: {on_conflict}")

        inserted += 1
        if not dry_run:
            target.add(model(**data))

    if not dry_run:
        target.commit()

    return inserted, replaced, skipped


def migrate(source_url: str, target_url: str, on_conflict: str, dry_run: bool) -> None:
    source_engine = create_engine(source_url, echo=False)
    target_engine = create_engine(target_url, echo=False, pool_pre_ping=True)

    # Ensure target schema exists.
    if not dry_run:
        SQLModel.metadata.create_all(target_engine)

    print(f"source={source_url}")
    print(f"target={target_url}")
    print(f"on_conflict={on_conflict}, dry_run={dry_run}")

    with Session(source_engine) as source, Session(target_engine) as target:
        for model in MODEL_ORDER:
            inserted, replaced, skipped = _migrate_table(
                source=source,
                target=target,
                model=model,
                on_conflict=on_conflict,
                dry_run=dry_run,
            )
            print(
                f"{model.__tablename__}: inserted={inserted} replaced={replaced} skipped={skipped}"  # type: ignore[attr-defined]
            )



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate PawLuxe DB from SQLite to PostgreSQL")
    parser.add_argument("--source", required=True, help="SQLAlchemy URL for source DB (typically sqlite)")
    parser.add_argument("--target", required=True, help="SQLAlchemy URL for target DB (typically postgres)")
    parser.add_argument(
        "--on-conflict",
        choices=["skip", "replace"],
        default="skip",
        help="How to handle same PK already existing in target",
    )
    parser.add_argument("--dry-run", action="store_true", help="Analyze and print counts without writing")
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    migrate(
        source_url=args.source,
        target_url=args.target,
        on_conflict=args.on_conflict,
        dry_run=args.dry_run,
    )
