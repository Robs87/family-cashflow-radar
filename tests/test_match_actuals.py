"""Tests for matching planned events to BeeCount actual transactions."""

import sqlite3

import pytest

from app.scripts.classify import classify
from app.scripts.import_beecount import import_beecount_payload
from app.scripts.normalize import normalize
from app.scripts.planned_events import create_planned_event, forecast_events_by_month, match_planned_events
from tests.conftest import SCHEMA_SQL, SEED_RULES_SQL


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.executescript(SEED_RULES_SQL.read_text(encoding="utf-8"))
    conn.close()
    return path


def test_match_planned_event_to_beecount_actual_and_remove_from_forecast(db_path):
    create_planned_event(
        db_path,
        "奖金到账",
        "2026-06-20",
        50_000_00,
        "inflow",
        "one_time_income",
        category_l2="奖金",
    )
    payload = {
        "ledger_id": "ledger_family_demo",
        "transactions": [
            {
                "sync_id": "bc_bonus_001",
                "tx_type": "income",
                "happened_at": "2026-06-21T08:00:00+08:00",
                "amount": "50000.00",
                "account_name": "招商银行",
                "category_name": "奖金",
                "note": "年中奖金",
            }
        ],
    }
    import_beecount_payload(db_path, payload)
    normalize(db_path)
    classify(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """UPDATE normalized_transactions
           SET manual_financial_type = 'one_time_income',
               manual_cashflow_direction = 'inflow',
               manual_category_l2 = '奖金'
           WHERE amount_cents = 5000000"""
    )
    conn.commit()
    conn.close()

    summary = match_planned_events(db_path)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT match_status, matched_normalized_transaction_id, match_confidence
           FROM planned_cashflow_events"""
    ).fetchone()
    forecast = forecast_events_by_month(conn, "2026-06", 1)
    conn.close()

    assert str(summary) == "matched=1 scanned=1"
    assert row["match_status"] == "matched"
    assert row["matched_normalized_transaction_id"] is not None
    assert row["match_confidence"] >= 0.9
    assert forecast == {}


def test_match_requires_beecount_source_not_legacy_manual_rows(db_path):
    event_id = create_planned_event(db_path, "计划收入", "2026-06-20", 10_000_00, "inflow", "one_time_income")
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """INSERT INTO raw_transactions
           (source_file, source_row_no, transaction_date, amount_cents, direction_raw,
            category_original, raw_hash)
           VALUES ('manual', 1, '2026-06-20', 1000000, '收入', '奖金', 'legacy_hash')"""
    )
    conn.execute(
        """INSERT INTO normalized_transactions
           (raw_transaction_id, transaction_date, year, month, amount_cents,
            cashflow_direction, financial_type, category_l2)
           VALUES (1, '2026-06-20', 2026, 6, 1000000, 'inflow', 'one_time_income', '奖金')"""
    )
    conn.commit()
    conn.close()

    summary = match_planned_events(db_path)

    conn = sqlite3.connect(str(db_path))
    status = conn.execute("SELECT match_status FROM planned_cashflow_events WHERE id = ?", (event_id,)).fetchone()[0]
    conn.close()

    assert str(summary) == "matched=0 scanned=1"
    assert status == "unmatched"
