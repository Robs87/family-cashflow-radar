#!/usr/bin/env python3
"""CSV importer for Family Cashflow Radar.

Reads Pixiu-exported CSV files into the raw_transactions table.
Supports single file or directory input, multiple encodings, and idempotent re-import.
"""

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

# Column aliases: normalized name -> list of possible CSV header names
COLUMN_ALIASES = {
    "transaction_time": ["时间", "交易时间", "日期时间", "交易日期时间", "date", "time", "datetime"],
    "amount": ["金额", "交易金额", "amount", "value"],
    "type": ["类型", "交易类型", "收支类型", "type", "direction"],
    "account": ["账户", "交易账户", "account", "card"],
    "category": ["分类", "类别", "category"],
    "subcategory": ["子分类", "子类别", "subcategory", "sub_category"],
    "merchant": ["商户", "商家", "交易对手", "merchant", "payee", "counterparty"],
    "note": ["备注", "说明", "描述", "note", "memo", "description", "comment"],
    "project": ["项目", "project"],
    "tags": ["标签", "tag", "tags"],
    "transaction_id": ["交易ID", "交易id", "ID", "id", "transaction_id", "txn_id", "流水号"],
}

ENCODINGS = ["utf-8", "utf-8-sig", "gbk", "gb18030"]


def _detect_encoding(file_path: Path) -> str:
    for enc in ENCODINGS:
        try:
            with open(file_path, encoding=enc) as f:
                f.read(4096)
            return enc
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError(f"无法检测文件编码: {file_path}，尝试过的编码: {ENCODINGS}")


def _parse_amount_cents(amount_str: str) -> int:
    cleaned = amount_str.strip().replace(",", "").replace("，", "")
    if not cleaned:
        raise ValueError("金额为空")
    try:
        amount = abs(Decimal(cleaned))
    except Exception:
        raise ValueError(f"无效金额: {amount_str!r}")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _extract_date(time_str: str) -> str:
    return time_str.strip()[:10]


def _compute_hash(payload: dict) -> str:
    stable = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _build_column_mapping(headers: list[str]) -> dict[str, int]:
    mapping = {}
    normalized_headers = [h.strip() for h in headers]
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            for idx, header in enumerate(normalized_headers):
                if header == alias:
                    mapping[field] = idx
                    break
            if field in mapping:
                break
    return mapping


def _read_csv_rows(file_path: Path, encoding: str):
    with open(file_path, encoding=encoding, newline="") as f:
        reader = csv.reader(f)
        headers = next(reader)
        # Strip BOM from first header if present
        if headers and headers[0].startswith("\ufeff"):
            headers[0] = headers[0].lstrip("\ufeff")
        col_map = _build_column_mapping(headers)
        required = ["transaction_time", "amount", "type"]
        missing = [f for f in required if f not in col_map]
        if missing:
            raise ValueError(f"CSV 缺少必要列: {missing}，找到的列: {headers}")
        rows = []
        for row_no_0, raw_row in enumerate(reader, start=1):
            if not raw_row or all(cell.strip() == "" for cell in raw_row):
                continue
            row = {field: raw_row[idx].strip() for field, idx in col_map.items() if idx < len(raw_row)}
            row["source_row_no"] = row_no_0
            rows.append(row)
        return rows


def import_csv(db_path: Path, input_path: Path, dry_run: bool = False, verbose: bool = False) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    cursor = conn.cursor()

    source_files = []
    if input_path.is_dir():
        source_files.extend(sorted(input_path.rglob("*.csv")))
    elif input_path.is_file():
        source_files.append(input_path)
    else:
        print(f"错误: 输入路径不存在: {input_path}", file=sys.stderr)
        sys.exit(1)

    total_imported = 0
    total_skipped = 0
    total_failed = 0

    for source_file in source_files:
        if verbose:
            print(f"导入: {source_file}")

        try:
            encoding = _detect_encoding(source_file)
            rows = _read_csv_rows(source_file, encoding)
        except Exception as e:
            print(f"错误: 读取文件失败 {source_file}: {e}", file=sys.stderr)
            total_failed += 1
            continue

        if verbose:
            print(f"  编码: {encoding}, 行数: {len(rows)}")

        for row in rows:
            source_row_no = row["source_row_no"]
            transaction_time = row.get("transaction_time", "")

            try:
                amount_cents = _parse_amount_cents(row["amount"])
            except ValueError as e:
                print(f"错误: 文件 {source_file} 行 {source_row_no}: 金额解析失败: {e}", file=sys.stderr)
                total_failed += 1
                continue

            amount_original = row["amount"]
            type_raw = row.get("type", "")
            income_amount_original = amount_original if type_raw == "收入" else ""
            expense_amount_original = amount_original if type_raw == "支出" else ""

            payload = {
                "source_file": str(source_file),
                "source_row_no": source_row_no,
                **{k: v for k, v in row.items() if k != "source_row_no"},
            }
            txn_id = row.get("transaction_id", "")
            if txn_id:
                payload["transaction_id"] = txn_id

            raw_hash = _compute_hash(payload)
            transaction_date = _extract_date(transaction_time) if transaction_time else ""

            if dry_run:
                total_imported += 1
                if verbose:
                    print(f"  [DRY RUN] 行 {source_row_no}: {transaction_time} {amount_original} {type_raw}")
                continue

            try:
                cursor.execute(
                    """INSERT OR IGNORE INTO raw_transactions
                    (source_file, source_row_no, transaction_time, transaction_date,
                     amount_original, income_amount_original, expense_amount_original,
                     amount_cents, direction_raw, account, category_original,
                     subcategory_original, merchant, note, project, tags,
                     raw_payload, raw_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(source_file),
                        source_row_no,
                        transaction_time,
                        transaction_date,
                        amount_original,
                        income_amount_original,
                        expense_amount_original,
                        amount_cents,
                        type_raw,
                        row.get("account", ""),
                        row.get("category", ""),
                        row.get("subcategory", ""),
                        row.get("merchant", ""),
                        row.get("note", ""),
                        row.get("project", ""),
                        row.get("tags", ""),
                        json.dumps(payload, ensure_ascii=False, default=str),
                        raw_hash,
                    ),
                )
                if cursor.rowcount > 0:
                    total_imported += 1
                    if verbose:
                        print(f"  行 {source_row_no}: {transaction_time} {amount_original} {type_raw}")
                else:
                    total_skipped += 1
                    if verbose:
                        print(f"  行 {source_row_no}: 重复，跳过")
            except sqlite3.Error as e:
                print(f"错误: 文件 {source_file} 行 {source_row_no}: {e}", file=sys.stderr)
                total_failed += 1

    if not dry_run:
        conn.commit()
    conn.close()

    print(f"imported={total_imported} skipped_duplicate={total_skipped} failed={total_failed}")

    if total_failed > 0:
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="导入 CSV 到 raw_transactions")
    parser.add_argument("--db", required=True, help="SQLite 数据库路径")
    parser.add_argument("--input", required=True, help="CSV 文件或目录路径")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不写入数据库")
    parser.add_argument("--verbose", action="store_true", help="输出详细日志")
    args = parser.parse_args()
    import_csv(Path(args.db), Path(args.input), dry_run=args.dry_run, verbose=args.verbose)


if __name__ == "__main__":
    main()
