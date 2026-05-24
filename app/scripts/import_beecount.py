#!/usr/bin/env python3
"""Import BeeCount Cloud transactions into raw_transactions.

BeeCount Cloud remains the recording layer. This importer mirrors BeeCount
transactions into the local analysis database so the existing normalize,
classify, monthly cashflow, and advice pipeline can consume them.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from app.scripts.beecount_tokens import get_token, write_keychain_token
from app.scripts.schema_migrations import ensure_v02_schema


TX_TYPES = {"expense", "income", "transfer"}


@dataclass(frozen=True)
class ImportSummary:
    imported: int = 0
    updated: int = 0
    deleted: int = 0
    skipped_duplicate: int = 0
    failed: int = 0

    def __str__(self) -> str:
        return (
            f"imported={self.imported} updated={self.updated} deleted={self.deleted} "
            f"skipped_duplicate={self.skipped_duplicate} failed={self.failed}"
        )


def _parse_amount_cents(value: Any) -> int:
    cleaned = str(value).strip().replace(",", "").replace("，", "")
    if not cleaned:
        raise ValueError("金额为空")
    amount = abs(Decimal(cleaned))
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _transaction_time(raw: dict[str, Any]) -> str:
    value = raw.get("happened_at") or raw.get("happenedAt") or raw.get("transaction_time")
    if not value:
        raise ValueError("缺少 happened_at / happenedAt")
    return str(value)


def _transaction_date(raw: dict[str, Any]) -> str:
    value = _transaction_time(raw)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return value[:10]


def _tx_type(raw: dict[str, Any]) -> str:
    value = str(raw.get("tx_type") or raw.get("type") or "").strip().lower()
    if value not in TX_TYPES:
        raise ValueError(f"不支持的 BeeCount 交易类型: {value!r}")
    return value


def _source_transaction_id(raw: dict[str, Any]) -> str:
    value = raw.get("sync_id") or raw.get("syncId") or raw.get("id") or raw.get("transaction_id")
    if not value:
        raise ValueError("缺少 BeeCount 交易标识 sync_id / syncId / id")
    return str(value)


def _source_updated_at(raw: dict[str, Any]) -> str:
    return str(raw.get("updated_at") or raw.get("updatedAt") or raw.get("modified_at") or raw.get("modifiedAt") or "")


def _source_deleted_at(raw: dict[str, Any]) -> str:
    if bool(raw.get("is_deleted") or raw.get("isDeleted") or raw.get("deleted")):
        return str(raw.get("deleted_at") or raw.get("deletedAt") or raw.get("updated_at") or raw.get("updatedAt") or "")
    return str(raw.get("deleted_at") or raw.get("deletedAt") or "")


def _tags(raw: dict[str, Any]) -> str:
    value = raw.get("tags_list")
    if isinstance(value, list):
        return ",".join(str(item) for item in value if str(item).strip())
    value = raw.get("tags")
    if isinstance(value, list):
        return ",".join(str(item) for item in value if str(item).strip())
    return str(value or "")


def _account(raw: dict[str, Any]) -> str:
    tx_type = _tx_type(raw)
    if tx_type == "transfer":
        from_name = str(raw.get("from_account_name") or raw.get("fromAccountName") or "").strip()
        to_name = str(raw.get("to_account_name") or raw.get("toAccountName") or "").strip()
        if from_name or to_name:
            return f"{from_name}->{to_name}"
    return str(raw.get("account_name") or raw.get("accountName") or "").strip()


def _normalize_payload(input_payload: Any, ledger_id: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    if isinstance(input_payload, list):
        if not ledger_id:
            ledger_id = "default"
        return ledger_id, [item for item in input_payload if isinstance(item, dict)]

    if not isinstance(input_payload, dict):
        raise ValueError("BeeCount payload 必须是交易数组或包含 transactions/items 的对象")

    effective_ledger_id = (
        ledger_id
        or input_payload.get("ledger_id")
        or input_payload.get("ledgerId")
        or input_payload.get("ledger_external_id")
        or input_payload.get("ledgerSyncId")
        or "default"
    )
    items = input_payload.get("transactions")
    if items is None:
        items = input_payload.get("items")
    if items is None:
        raise ValueError("BeeCount payload 缺少 transactions / items")
    if not isinstance(items, list):
        raise ValueError("BeeCount transactions / items 必须是数组")
    return str(effective_ledger_id), [item for item in items if isinstance(item, dict)]


def _payload_json(ledger_id: str, raw: dict[str, Any]) -> str:
    payload = {
        "source_system": "beecount_cloud",
        "source_ledger_id": ledger_id,
        "source_transaction_id": _source_transaction_id(raw),
        "source_updated_at": _source_updated_at(raw),
        "source_deleted_at": _source_deleted_at(raw),
        "transaction": raw,
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _payload_fingerprint(raw: dict[str, Any]) -> str:
    canonical = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _raw_hash(ledger_id: str, raw: dict[str, Any]) -> str:
    version = _source_updated_at(raw) or _source_deleted_at(raw) or _payload_fingerprint(raw)
    return f"beecount_cloud:{ledger_id}:{_source_transaction_id(raw)}:{version}"


def _row_values(ledger_id: str, source_row_no: int, raw: dict[str, Any]) -> tuple[Any, ...]:
    tx_type = _tx_type(raw)
    amount_cents = _parse_amount_cents(raw.get("amount"))
    amount_original = f"{Decimal(amount_cents) / Decimal(100):.2f}"
    transaction_time = _transaction_time(raw)
    direction_raw = {"income": "收入", "expense": "支出", "transfer": "转账"}[tx_type]
    income_original = amount_original if tx_type == "income" else ""
    expense_original = amount_original if tx_type == "expense" else ""
    category = str(raw.get("category_name") or raw.get("categoryName") or "").strip()
    note = str(raw.get("note") or "").strip()
    source_transaction_id = _source_transaction_id(raw)
    source_updated_at = _source_updated_at(raw)
    source_deleted_at = _source_deleted_at(raw)
    return (
        f"beecount_cloud:{ledger_id}",
        source_row_no,
        transaction_time,
        _transaction_date(raw),
        amount_original,
        income_original,
        expense_original,
        amount_cents,
        direction_raw,
        _account(raw),
        category,
        "",
        "",
        note,
        "",
        _tags(raw),
        _payload_json(ledger_id, raw),
        _raw_hash(ledger_id, raw),
        "beecount_cloud",
        ledger_id,
        source_transaction_id,
        source_updated_at,
        source_deleted_at,
        1,
    )


def import_beecount_payload(
    db_path: Path,
    payload: Any,
    ledger_id: str | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> ImportSummary:
    effective_ledger_id, transactions = _normalize_payload(payload, ledger_id=ledger_id)
    imported = updated = deleted = skipped = failed = 0

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_v02_schema(conn)
    cursor = conn.cursor()
    try:
        for source_row_no, raw in enumerate(transactions, start=1):
            try:
                row = _row_values(effective_ledger_id, source_row_no, raw)
            except (ValueError, ArithmeticError) as exc:
                print(f"错误: BeeCount 交易 {source_row_no}: {exc}", file=sys.stderr)
                failed += 1
                continue

            if dry_run:
                imported += 1
                if verbose:
                    print(f"[DRY RUN] {_source_transaction_id(raw)} {_tx_type(raw)} {raw.get('amount')}")
                continue

            existing = cursor.execute(
                "SELECT raw_payload FROM raw_transactions WHERE raw_hash = ?",
                (row[17],),
            ).fetchone()
            raw_hash = row[17]
            source_system = row[18]
            source_ledger_id = row[19]
            source_transaction_id = row[20]
            source_deleted_at = row[22]
            prior_latest = cursor.execute(
                """SELECT raw_hash
                   FROM raw_transactions
                   WHERE source_system = ?
                     AND source_ledger_id = ?
                     AND source_transaction_id = ?
                     AND source_is_latest = 1
                   ORDER BY id DESC
                   LIMIT 1""",
                (source_system, source_ledger_id, source_transaction_id),
            ).fetchone()
            if existing and existing[0] == row[16]:
                if prior_latest and prior_latest[0] != raw_hash:
                    cursor.execute(
                        """UPDATE raw_transactions
                           SET source_is_latest = CASE WHEN raw_hash = ? THEN 1 ELSE 0 END
                           WHERE source_system = ?
                             AND source_ledger_id = ?
                             AND source_transaction_id = ?""",
                        (raw_hash, source_system, source_ledger_id, source_transaction_id),
                    )
                skipped += 1
                continue

            if existing:
                cursor.execute(
                    """DELETE FROM normalized_transactions
                       WHERE raw_transaction_id IN (
                           SELECT id
                           FROM raw_transactions
                           WHERE source_system = ?
                             AND source_ledger_id = ?
                             AND source_transaction_id = ?
                       )""",
                    (source_system, source_ledger_id, source_transaction_id),
                )
                cursor.execute(
                    """UPDATE raw_transactions
                       SET source_file = ?,
                           source_row_no = ?,
                           transaction_time = ?,
                           transaction_date = ?,
                           amount_original = ?,
                           income_amount_original = ?,
                           expense_amount_original = ?,
                           amount_cents = ?,
                           direction_raw = ?,
                           account = ?,
                           category_original = ?,
                           subcategory_original = ?,
                           merchant = ?,
                           note = ?,
                           project = ?,
                           tags = ?,
                           raw_payload = ?,
                           source_system = ?,
                           source_ledger_id = ?,
                           source_transaction_id = ?,
                           source_updated_at = ?,
                           source_deleted_at = ?,
                           source_is_latest = ?,
                           imported_at = CURRENT_TIMESTAMP
                       WHERE raw_hash = ?""",
                    row[:17] + row[18:] + (raw_hash,),
                )
                updated += 1
            else:
                if prior_latest:
                    cursor.execute(
                        """DELETE FROM normalized_transactions
                           WHERE raw_transaction_id IN (
                               SELECT id
                               FROM raw_transactions
                               WHERE source_system = ?
                                 AND source_ledger_id = ?
                                 AND source_transaction_id = ?
                           )""",
                        (source_system, source_ledger_id, source_transaction_id),
                    )
                cursor.execute(
                    """UPDATE raw_transactions
                       SET source_is_latest = 0
                       WHERE source_system = ?
                         AND source_ledger_id = ?
                         AND source_transaction_id = ?""",
                    (source_system, source_ledger_id, source_transaction_id),
                )
                cursor.execute(
                    """INSERT INTO raw_transactions
                    (source_file, source_row_no, transaction_time, transaction_date,
                     amount_original, income_amount_original, expense_amount_original,
                     amount_cents, direction_raw, account, category_original,
                     subcategory_original, merchant, note, project, tags,
                     raw_payload, raw_hash, source_system, source_ledger_id,
                     source_transaction_id, source_updated_at, source_deleted_at,
                     source_is_latest)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    row,
                )
                if source_deleted_at:
                    deleted += 1
                elif prior_latest:
                    updated += 1
                else:
                    imported += 1

        conn.commit()
    finally:
        conn.close()
    return ImportSummary(imported=imported, updated=updated, deleted=deleted, skipped_duplicate=skipped, failed=failed)


def _refresh_access_token(base_url: str, refresh_token: str) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/v1/auth/refresh"
    body = json.dumps({"refresh_token": refresh_token}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise RuntimeError("BeeCount refresh 响应缺少 access_token")
    if not payload.get("refresh_token"):
        raise RuntimeError("BeeCount refresh 响应缺少 refresh_token")
    return payload


def _fetch_api_payload(base_url: str, ledger_id: str, access_token: str, limit: int) -> Any:
    endpoint = f"{base_url.rstrip('/')}/api/v1/read/ledgers/{urllib.parse.quote(ledger_id)}/transactions"
    url = f"{endpoint}?{urllib.parse.urlencode({'limit': limit})}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_api_payload_with_refresh(
    base_url: str,
    ledger_id: str,
    read_token_env: str,
    access_token_env: str,
    refresh_token_env: str,
    limit: int,
) -> Any:
    read_token = get_token(read_token_env)
    if read_token.value:
        try:
            return _fetch_api_payload(base_url, ledger_id, read_token.value, limit)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RuntimeError(
                    f"BeeCount read API token 无效或权限不足；请在 BeeCount 创建 read:api PAT 并更新 {read_token_env}"
                ) from exc
            raise

    access_token = get_token(access_token_env)
    refresh_token = get_token(refresh_token_env)

    refresh_attempted = False
    if access_token.value:
        try:
            return _fetch_api_payload(base_url, ledger_id, access_token.value, limit)
        except urllib.error.HTTPError as exc:
            if exc.code != 401 or not refresh_token.value:
                raise
            refresh_attempted = True
    elif not refresh_token.value:
        raise RuntimeError(
            f"环境变量 {read_token_env} 未设置，且 {access_token_env} / {refresh_token_env} 未设置"
        )

    try:
        refreshed = _refresh_access_token(base_url, refresh_token.value)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            prefix = "access token 已失效，且 " if refresh_attempted else ""
            raise RuntimeError(
                f"{prefix}refresh token 无效或已轮换；请重新登录 BeeCount 并更新 {refresh_token_env}"
            ) from exc
        raise
    os.environ[access_token_env] = str(refreshed["access_token"])
    os.environ[refresh_token_env] = str(refreshed["refresh_token"])
    if access_token.source == "keychain" or refresh_token.source == "keychain":
        write_keychain_token(access_token_env, str(refreshed["access_token"]))
        write_keychain_token(refresh_token_env, str(refreshed["refresh_token"]))
    return _fetch_api_payload(base_url, ledger_id, str(refreshed["access_token"]), limit)


def import_beecount(
    db_path: Path,
    input_json: Path | None = None,
    base_url: str | None = None,
    ledger_id: str | None = None,
    read_token_env: str = "BEECOUNT_READ_API_TOKEN",
    access_token_env: str = "BEECOUNT_ACCESS_TOKEN",
    refresh_token_env: str = "BEECOUNT_REFRESH_TOKEN",
    limit: int = 500,
    dry_run: bool = False,
    verbose: bool = False,
) -> ImportSummary:
    """Import BeeCount transactions from a local JSON payload or read API."""
    if input_json:
        payload = json.loads(input_json.read_text(encoding="utf-8"))
    elif base_url and ledger_id:
        payload = _fetch_api_payload_with_refresh(
            base_url,
            ledger_id,
            read_token_env,
            access_token_env,
            refresh_token_env,
            limit,
        )
    else:
        raise ValueError("必须提供 input_json，或同时提供 base_url 和 ledger_id")

    summary = import_beecount_payload(
        db_path,
        payload,
        ledger_id=ledger_id,
        dry_run=dry_run,
        verbose=verbose,
    )
    print(summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同步 BeeCount Cloud 流水到 raw_transactions")
    parser.add_argument("--db", default="data/processed/cashflow.db")
    parser.add_argument("--input-json", help="BeeCount transactions/items JSON 文件")
    parser.add_argument("--base-url", help="BeeCount Cloud base URL，例如 https://bee.332626.xyz:9090")
    parser.add_argument("--ledger-id", help="BeeCount ledger id / external id")
    parser.add_argument(
        "--read-token-env",
        default="BEECOUNT_READ_API_TOKEN",
        help="读取 BeeCount 长期只读 read:api PAT 的环境变量名；优先于 access/refresh token",
    )
    parser.add_argument(
        "--access-token-env",
        default="BEECOUNT_ACCESS_TOKEN",
        help="读取 BeeCount 普通 access token 的环境变量名；仅作为旧登录 token 兜底",
    )
    parser.add_argument(
        "--refresh-token-env",
        default="BEECOUNT_REFRESH_TOKEN",
        help="读取 BeeCount refresh token 的环境变量名；access token 缺失或过期时自动刷新",
    )
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    try:
        summary = import_beecount(
            Path(args.db),
            input_json=Path(args.input_json) if args.input_json else None,
            base_url=args.base_url,
            ledger_id=args.ledger_id,
            read_token_env=args.read_token_env,
            access_token_env=args.access_token_env,
            refresh_token_env=args.refresh_token_env,
            limit=args.limit,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
    except Exception as exc:
        print(f"错误: BeeCount 导入失败: {exc}", file=sys.stderr)
        return 1

    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
