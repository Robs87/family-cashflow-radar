"""Tests for BeeCount Cloud transaction import."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.scripts import import_beecount as import_beecount_module
from app.scripts.beecount_tokens import StoredToken
from app.scripts.classify import classify
from app.scripts.beecount_category_mappings import infer_category_mapping, upsert_mapping
from app.scripts.import_beecount import import_beecount, import_beecount_payload
from app.scripts.normalize import normalize
from tests.conftest import FIXTURES_DIR, PROJECT_ROOT, SCHEMA_SQL


IMPORT_SCRIPT = PROJECT_ROOT / "app" / "scripts" / "import_beecount.py"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.executescript((PROJECT_ROOT / "app" / "db" / "seed_rules.sql").read_text(encoding="utf-8"))
    conn.close()
    return path


def _load_payload() -> dict:
    return json.loads((FIXTURES_DIR / "sample_beecount_transactions.json").read_text(encoding="utf-8"))


def _fetch_raw(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM raw_transactions ORDER BY id").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def test_import_beecount_transactions_maps_direction_and_amounts(db_path):
    summary = import_beecount_payload(db_path, _load_payload())

    assert str(summary) == "imported=3 updated=0 skipped_duplicate=0 failed=0"
    rows = _fetch_raw(db_path)
    assert [row["direction_raw"] for row in rows] == ["收入", "支出", "转账"]
    assert [row["amount_cents"] for row in rows] == [2000000, 6850, 500000]
    assert rows[0]["income_amount_original"] == "20000.00"
    assert rows[1]["expense_amount_original"] == "68.50"
    assert rows[2]["income_amount_original"] == ""
    assert rows[2]["expense_amount_original"] == ""
    assert rows[2]["account"] == "招商银行->交通银行"
    assert rows[0]["raw_hash"] == "beecount_cloud:ledger_family_demo:bc_tx_income_001"


def test_import_beecount_is_idempotent(db_path):
    payload = _load_payload()
    first = import_beecount_payload(db_path, payload)
    second = import_beecount_payload(db_path, payload)

    assert first.imported == 3
    assert str(second) == "imported=0 updated=0 skipped_duplicate=3 failed=0"
    assert len(_fetch_raw(db_path)) == 3


def test_import_beecount_updates_existing_transaction_by_sync_id(db_path):
    payload = _load_payload()
    import_beecount_payload(db_path, payload)
    payload["transactions"][1]["amount"] = "70.00"
    payload["transactions"][1]["note"] = "午饭修正"

    summary = import_beecount_payload(db_path, payload)

    assert str(summary) == "imported=0 updated=1 skipped_duplicate=2 failed=0"
    rows = _fetch_raw(db_path)
    assert rows[1]["amount_cents"] == 7000
    assert rows[1]["note"] == "午饭修正"
    assert len(rows) == 3


def test_import_beecount_rejects_bad_transaction_without_stopping_batch(db_path):
    payload = _load_payload()
    payload["transactions"].append({"sync_id": "bad", "tx_type": "expense", "happened_at": "2026-05-04"})

    summary = import_beecount_payload(db_path, payload)

    assert summary.imported == 3
    assert summary.failed == 1
    assert len(_fetch_raw(db_path)) == 3


def test_import_beecount_cli_input_json(db_path):
    result = subprocess.run(
        [
            sys.executable,
            str(IMPORT_SCRIPT),
            "--db",
            str(db_path),
            "--input-json",
            str(FIXTURES_DIR / "sample_beecount_transactions.json"),
        ],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0
    assert "imported=3 updated=0 skipped_duplicate=0 failed=0" in result.stdout
    assert len(_fetch_raw(db_path)) == 3


def test_import_beecount_uses_refresh_token_when_access_token_missing(db_path, monkeypatch):
    payload = _load_payload()

    monkeypatch.delenv("BEECOUNT_ACCESS_TOKEN_TEST", raising=False)
    monkeypatch.setenv("BEECOUNT_REFRESH_TOKEN_TEST", "refresh-old")
    monkeypatch.setattr(
        import_beecount_module,
        "_refresh_access_token",
        lambda base_url, refresh_token: {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
        },
    )
    monkeypatch.setattr(
        import_beecount_module,
        "_fetch_api_payload",
        lambda base_url, ledger_id, access_token, limit: payload,
    )

    summary = import_beecount(
        db_path,
        base_url="https://bee.example",
        ledger_id="ledger_family_demo",
        access_token_env="BEECOUNT_ACCESS_TOKEN_TEST",
        refresh_token_env="BEECOUNT_REFRESH_TOKEN_TEST",
    )

    assert summary.imported == 3
    assert len(_fetch_raw(db_path)) == 3
    assert os.environ["BEECOUNT_ACCESS_TOKEN_TEST"] == "access-new"
    assert os.environ["BEECOUNT_REFRESH_TOKEN_TEST"] == "refresh-new"


def test_import_beecount_refreshes_after_unauthorized(db_path, monkeypatch):
    import urllib.error

    payload = _load_payload()
    calls = []

    def fake_fetch(base_url, ledger_id, access_token, limit):
        calls.append(access_token)
        if access_token == "access-old":
            raise urllib.error.HTTPError("url", 401, "Unauthorized", None, None)
        return payload

    monkeypatch.setenv("BEECOUNT_ACCESS_TOKEN_TEST", "access-old")
    monkeypatch.setenv("BEECOUNT_REFRESH_TOKEN_TEST", "refresh-old")
    monkeypatch.setattr(
        import_beecount_module,
        "_refresh_access_token",
        lambda base_url, refresh_token: {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
        },
    )
    monkeypatch.setattr(import_beecount_module, "_fetch_api_payload", fake_fetch)

    summary = import_beecount(
        db_path,
        base_url="https://bee.example",
        ledger_id="ledger_family_demo",
        access_token_env="BEECOUNT_ACCESS_TOKEN_TEST",
        refresh_token_env="BEECOUNT_REFRESH_TOKEN_TEST",
    )

    assert summary.imported == 3
    assert calls == ["access-old", "access-new"]


def test_import_beecount_reports_rotated_refresh_token(db_path, monkeypatch):
    import urllib.error

    def fake_fetch(base_url, ledger_id, access_token, limit):
        raise urllib.error.HTTPError("url", 401, "Unauthorized", None, None)

    def fake_refresh(base_url, refresh_token):
        raise urllib.error.HTTPError("url", 401, "Unauthorized", None, None)

    monkeypatch.setenv("BEECOUNT_ACCESS_TOKEN_TEST", "access-old")
    monkeypatch.setenv("BEECOUNT_REFRESH_TOKEN_TEST", "refresh-old")
    monkeypatch.setattr(import_beecount_module, "_fetch_api_payload", fake_fetch)
    monkeypatch.setattr(import_beecount_module, "_refresh_access_token", fake_refresh)

    with pytest.raises(RuntimeError, match="refresh token 无效或已轮换"):
        import_beecount(
            db_path,
            base_url="https://bee.example",
            ledger_id="ledger_family_demo",
            access_token_env="BEECOUNT_ACCESS_TOKEN_TEST",
            refresh_token_env="BEECOUNT_REFRESH_TOKEN_TEST",
        )


def test_import_beecount_reads_tokens_from_keychain_and_persists_rotation(db_path, monkeypatch):
    import urllib.error

    payload = _load_payload()
    calls = []
    saved_tokens = []

    def fake_get_token(env_name):
        if env_name == "BEECOUNT_ACCESS_TOKEN_TEST":
            return StoredToken("access-old", "keychain")
        if env_name == "BEECOUNT_REFRESH_TOKEN_TEST":
            return StoredToken("refresh-old", "keychain")
        return StoredToken("", "")

    def fake_fetch(base_url, ledger_id, access_token, limit):
        calls.append(access_token)
        if access_token == "access-old":
            raise urllib.error.HTTPError("url", 401, "Unauthorized", None, None)
        return payload

    monkeypatch.delenv("BEECOUNT_ACCESS_TOKEN_TEST", raising=False)
    monkeypatch.delenv("BEECOUNT_REFRESH_TOKEN_TEST", raising=False)
    monkeypatch.setattr(import_beecount_module, "get_token", fake_get_token)
    monkeypatch.setattr(import_beecount_module, "_fetch_api_payload", fake_fetch)
    monkeypatch.setattr(
        import_beecount_module,
        "_refresh_access_token",
        lambda base_url, refresh_token: {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
        },
    )
    monkeypatch.setattr(
        import_beecount_module,
        "write_keychain_token",
        lambda account, token: saved_tokens.append((account, token)),
    )

    summary = import_beecount(
        db_path,
        base_url="https://bee.example",
        ledger_id="ledger_family_demo",
        access_token_env="BEECOUNT_ACCESS_TOKEN_TEST",
        refresh_token_env="BEECOUNT_REFRESH_TOKEN_TEST",
    )

    assert summary.imported == 3
    assert calls == ["access-old", "access-new"]
    assert saved_tokens == [
        ("BEECOUNT_ACCESS_TOKEN_TEST", "access-new"),
        ("BEECOUNT_REFRESH_TOKEN_TEST", "refresh-new"),
    ]


def test_beecount_known_transactions_are_auto_approved_after_classify(db_path):
    payload = {
        "ledger_id": "ledger_family_demo",
        "transactions": [
            {
                "sync_id": "bc_large_meal",
                "tx_type": "expense",
                "happened_at": "2026-05-10T08:00:00+08:00",
                "amount": "12000.00",
                "account_name": "招商银行",
                "category_name": "早餐",
                "note": "早餐",
            }
        ],
    }

    import_beecount_payload(db_path, payload)
    normalize(db_path)
    classify(db_path)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT financial_type, review_status
           FROM normalized_transactions"""
    ).fetchone()
    conn.close()

    assert dict(row) == {"financial_type": "living_expense", "review_status": "approved"}


def test_beecount_category_mapping_overrides_keyword_guessing(db_path):
    payload = {
        "ledger_id": "ledger_family_demo",
        "transactions": [
            {
                "sync_id": "bc_fund_redeem",
                "tx_type": "expense",
                "happened_at": "2026-05-10T08:00:00+08:00",
                "amount": "584.58",
                "account_name": "招商银行",
                "category_name": "基金赎回",
                "note": "",
            },
            {
                "sync_id": "bc_cash_dividend",
                "tx_type": "expense",
                "happened_at": "2026-05-11T08:00:00+08:00",
                "amount": "459.20",
                "account_name": "招商银行",
                "category_name": "现金分红",
                "note": "",
            },
        ],
    }

    import_beecount_payload(db_path, payload)
    normalize(db_path)
    classify(db_path)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT category_l1, category_l2, cashflow_direction, financial_type, review_status
           FROM normalized_transactions
           ORDER BY id"""
    ).fetchall()
    mapping_count = conn.execute("SELECT COUNT(*) FROM beecount_category_mappings").fetchone()[0]
    conn.close()

    assert [dict(row) for row in rows] == [
        {
            "category_l1": "投资",
            "category_l2": "基金赎回",
            "cashflow_direction": "inflow",
            "financial_type": "investment_inflow",
            "review_status": "approved",
        },
        {
            "category_l1": "投资",
            "category_l2": "现金分红",
            "cashflow_direction": "inflow",
            "financial_type": "investment_inflow",
            "review_status": "approved",
        },
    ]
    assert mapping_count == 2


def test_beecount_mapping_can_upgrade_prior_inferred_unknown(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """INSERT INTO beecount_category_mappings
           (beecount_kind, category_name, parent_name, radar_cashflow_direction,
            radar_financial_type, radar_category_l1, radar_category_l2, mapping_source)
           VALUES ('expense', '早餐', '', 'outflow', 'unknown', '未映射', '早餐', 'inferred')"""
    )

    upsert_mapping(conn, infer_category_mapping("expense", "早餐", "", 1))
    row = conn.execute(
        """SELECT radar_cashflow_direction, radar_financial_type, radar_category_l1, radar_category_l2
           FROM beecount_category_mappings
           WHERE beecount_kind = 'expense' AND category_name = '早餐'"""
    ).fetchone()
    conn.close()

    assert row == ("outflow", "living_expense", "日常生活", "早餐")
