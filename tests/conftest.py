import sqlite3
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL = PROJECT_ROOT / "app" / "db" / "schema.sql"
SEED_RULES_SQL = PROJECT_ROOT / "app" / "db" / "seed_rules.sql"
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"


@pytest.fixture
def db_conn():
    """Create an in-memory SQLite database with schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    yield conn
    conn.close()


@pytest.fixture
def db_conn_with_rules(db_conn):
    """In-memory database with schema + seed rules loaded."""
    db_conn.executescript(SEED_RULES_SQL.read_text(encoding="utf-8"))
    return db_conn
