from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omarchy_yolo.db import Database


def test_schema_contract_detects_missing_index_when_validated_directly(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "state.sqlite3")
    try:
        db._execute("DROP INDEX idx_jobs_created")
        with pytest.raises(sqlite3.DatabaseError, match="missing index idx_jobs_created"):
            db._validate_schema_contract()
    finally:
        db.close()


def test_missing_migration_path_rolls_back_without_changing_schema_version(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "state.sqlite3")
    try:
        before = db.schema_version()
        with pytest.raises(sqlite3.DatabaseError, match="no migration path"):
            db._migrate(0)
        assert db.schema_version() == before
    finally:
        db.close()
