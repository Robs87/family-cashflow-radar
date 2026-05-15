#!/usr/bin/env python3
"""Minimal local web dashboard for Family Cashflow Radar."""

import argparse
import contextlib
import html
import io
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.scripts.classify import classify
from app.scripts.generate_monthly_cashflow import generate_monthly_cashflow
from app.scripts.import_csv import import_csv
from app.scripts.normalize import normalize


DEFAULT_DB = Path("data/processed/cashflow.db")
DEFAULT_RAW_INPUT = Path("data/raw")
SCHEMA_SQL = Path(__file__).resolve().parent / "db" / "schema.sql"
SEED_RULES_SQL = Path(__file__).resolve().parent / "db" / "seed_rules.sql"


def _format_yuan(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents_abs = abs(int(cents or 0))
    return f"{sign}{cents_abs // 100:,}.{cents_abs % 100:02d}"


def _month_label(row: sqlite3.Row) -> str:
    return f"{row['year']}-{row['month']:02d}"


def _fetch_dashboard_data(db_path: Path) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        latest_month = conn.execute(
            """SELECT *
               FROM monthly_cashflow
               ORDER BY year DESC, month DESC
               LIMIT 1"""
        ).fetchone()
        trend = conn.execute(
            """SELECT year, month, net_operating_cashflow_cents
               FROM monthly_cashflow
               ORDER BY year DESC, month DESC
               LIMIT 12"""
        ).fetchall()
        review = conn.execute(
            """SELECT
                  SUM(CASE WHEN COALESCE(manual_financial_type, financial_type) = 'unknown' THEN 1 ELSE 0 END) AS unknown_count,
                  SUM(CASE WHEN review_status = 'pending' THEN 1 ELSE 0 END) AS pending_count
               FROM normalized_transactions"""
        ).fetchone()
    finally:
        conn.close()

    return {
        "latest_month": dict(latest_month) if latest_month else None,
        "trend": [dict(row) for row in reversed(trend)],
        "unknown_count": int((review["unknown_count"] if review else 0) or 0),
        "pending_count": int((review["pending_count"] if review else 0) or 0),
    }


def _ensure_database_initialized(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        has_raw_table = conn.execute(
            """SELECT 1
               FROM sqlite_master
               WHERE type = 'table' AND name = 'raw_transactions'"""
        ).fetchone()
        if not has_raw_table:
            conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))

        rules_count = conn.execute("SELECT COUNT(*) FROM classification_rules").fetchone()[0]
        if rules_count == 0:
            conn.executescript(SEED_RULES_SQL.read_text(encoding="utf-8"))

        conn.commit()
    finally:
        conn.close()


def _run_step(label: str, func, *args) -> dict:
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = 0
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            func(*args)
        except SystemExit as exc:
            exit_code = int(exc.code or 0)
        except Exception as exc:
            exit_code = 1
            print(f"{type(exc).__name__}: {exc}", file=stderr)

    return {
        "label": label,
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "stdout": stdout.getvalue().strip(),
        "stderr": stderr.getvalue().strip(),
    }


def run_refresh_pipeline(db_path: Path, input_path: Path = DEFAULT_RAW_INPUT) -> dict:
    started_steps = []
    try:
        _ensure_database_initialized(db_path)
    except Exception as exc:
        return {
            "ok": False,
            "steps": [
                {
                    "label": "初始化数据库",
                    "ok": False,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": f"{type(exc).__name__}: {exc}",
                }
            ],
        }

    steps = [
        ("导入 CSV", import_csv, db_path, input_path),
        ("标准化交易", normalize, db_path),
        ("规则分类", classify, db_path),
        ("生成月度现金流", generate_monthly_cashflow, db_path),
    ]
    for label, func, *args in steps:
        result = _run_step(label, func, *args)
        started_steps.append(result)
        if not result["ok"]:
            break

    return {"ok": all(step["ok"] for step in started_steps), "steps": started_steps}


def _metric(label: str, value: str, tone: str = "neutral") -> str:
    return (
        f'<section class="metric metric-{tone}">'
        f"<span>{html.escape(label)}</span>"
        f"<strong>{html.escape(value)}</strong>"
        "</section>"
    )


def _render_trend_bars(trend: list[dict]) -> str:
    if not trend:
        return '<p class="empty">暂无月度趋势数据</p>'

    max_abs = max(abs(row["net_operating_cashflow_cents"] or 0) for row in trend) or 1
    bars = []
    for row in trend:
        value = int(row["net_operating_cashflow_cents"] or 0)
        width = max(4, round(abs(value) / max_abs * 100))
        tone = "positive" if value >= 0 else "negative"
        bars.append(
            '<div class="trend-row">'
            f'<span class="trend-month">{html.escape(_month_label(row))}</span>'
            '<div class="trend-track">'
            f'<div class="trend-bar {tone}" style="width:{width}%"></div>'
            "</div>"
            f'<span class="trend-value">{html.escape(_format_yuan(value))}</span>'
            "</div>"
        )
    return "\n".join(bars)


def _render_pipeline_result(result: dict | None) -> str:
    if not result:
        return ""

    tone = "success" if result["ok"] else "failure"
    title = "刷新完成" if result["ok"] else "刷新失败"
    step_html = []
    for step in result["steps"]:
        status = "完成" if step["ok"] else "失败"
        output = "\n".join(part for part in (step["stdout"], step["stderr"]) if part)
        step_html.append(
            '<li class="run-step">'
            f'<span><strong>{html.escape(step["label"])}</strong><em>{html.escape(status)}</em></span>'
            f'<code>{html.escape(output or "无输出")}</code>'
            "</li>"
        )

    return (
        f'<section class="run-result run-{tone}">'
        f"<h2>{html.escape(title)}</h2>"
        f'<ol>{"".join(step_html)}</ol>'
        "</section>"
    )


def render_dashboard_html(db_path: Path, pipeline_result: dict | None = None) -> str:
    data = _fetch_dashboard_data(db_path)
    latest = data["latest_month"]

    if latest:
        month_label = _month_label(latest)
        stable_income = _format_yuan(latest["stable_income_cents"])
        fixed_expense = _format_yuan(latest["fixed_expense_cents"])
        debt_payment = _format_yuan(latest["debt_payment_cents"])
        net_operating = _format_yuan(latest["net_operating_cashflow_cents"])
        net_tone = "good" if latest["net_operating_cashflow_cents"] >= 0 else "bad"
        metrics = "\n".join(
            [
                _metric("本月稳定收入", f"{stable_income} 元", "good"),
                _metric("本月刚性支出", f"{fixed_expense} 元"),
                _metric("本月债务还款", f"{debt_payment} 元"),
                _metric("本月基础结余", f"{net_operating} 元", net_tone),
            ]
        )
    else:
        month_label = "暂无月份"
        metrics = '<p class="empty">暂无月度现金流数据。请先运行导入、标准化、分类和月度聚合脚本。</p>'

    unknown_count = data["unknown_count"]
    pending_count = data["pending_count"]
    trend_html = _render_trend_bars(data["trend"])
    pipeline_html = _render_pipeline_result(pipeline_result)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>家庭现金流雷达</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #1d252d;
      --muted: #65717d;
      --line: #d9e0e7;
      --panel: #ffffff;
      --bg: #f6f8fa;
      --green: #207a50;
      --red: #b33b3b;
      --blue: #2f6690;
      --amber: #8a6200;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }}
    header {{
      border-bottom: 1px solid var(--line);
      background: #ffffff;
    }}
    .wrap {{
      width: min(1120px, calc(100% - 32px));
      margin: 0 auto;
    }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      padding: 20px 0;
    }}
    h1 {{
      margin: 0;
      font-size: 24px;
      font-weight: 700;
      letter-spacing: 0;
    }}
    .period {{
      color: var(--muted);
      font-size: 14px;
      white-space: nowrap;
    }}
    .actions {{
      display: flex;
      gap: 10px;
      align-items: center;
    }}
    button {{
      min-height: 38px;
      border: 1px solid var(--green);
      border-radius: 8px;
      background: var(--green);
      color: #ffffff;
      font: inherit;
      font-weight: 700;
      padding: 0 14px;
      cursor: pointer;
    }}
    button:active {{ transform: translateY(1px); }}
    main {{
      padding: 22px 0 36px;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }}
    .metric, .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .metric {{
      min-height: 104px;
      padding: 16px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      border-top: 4px solid var(--blue);
    }}
    .metric-good {{ border-top-color: var(--green); }}
    .metric-bad {{ border-top-color: var(--red); }}
    .metric span {{
      color: var(--muted);
      font-size: 13px;
    }}
    .metric strong {{
      font-size: 24px;
      line-height: 1.15;
      overflow-wrap: anywhere;
    }}
    .grid {{
      display: grid;
      grid-template-columns: minmax(0, 2fr) minmax(280px, 1fr);
      gap: 14px;
      margin-top: 14px;
    }}
    .panel {{
      padding: 18px;
    }}
    h2 {{
      margin: 0 0 16px;
      font-size: 17px;
      letter-spacing: 0;
    }}
    .trend-row {{
      display: grid;
      grid-template-columns: 72px minmax(120px, 1fr) 116px;
      gap: 12px;
      align-items: center;
      min-height: 34px;
      font-size: 13px;
    }}
    .trend-month {{ color: var(--muted); }}
    .trend-track {{
      height: 12px;
      border-radius: 6px;
      background: #e7ebef;
      overflow: hidden;
    }}
    .trend-bar {{
      height: 100%;
      border-radius: 6px;
    }}
    .trend-bar.positive {{ background: var(--green); }}
    .trend-bar.negative {{ background: var(--red); }}
    .trend-value {{
      text-align: right;
      font-variant-numeric: tabular-nums;
    }}
    .review-list {{
      display: grid;
      gap: 10px;
    }}
    .review-item {{
      display: flex;
      justify-content: space-between;
      gap: 14px;
      padding: 12px 0;
      border-bottom: 1px solid var(--line);
    }}
    .review-item:last-child {{ border-bottom: 0; }}
    .review-item span {{ color: var(--muted); }}
    .review-item strong {{
      font-size: 22px;
      font-variant-numeric: tabular-nums;
    }}
    .review-item.warn strong {{ color: var(--amber); }}
    .empty {{
      color: var(--muted);
      margin: 0;
    }}
    .run-result {{
      margin-bottom: 14px;
      border: 1px solid var(--line);
      border-left: 4px solid var(--green);
      border-radius: 8px;
      background: #ffffff;
      padding: 16px 18px;
    }}
    .run-failure {{ border-left-color: var(--red); }}
    .run-result h2 {{
      margin-bottom: 12px;
    }}
    .run-result ol {{
      list-style: none;
      padding: 0;
      margin: 0;
      display: grid;
      gap: 8px;
    }}
    .run-step {{
      display: grid;
      grid-template-columns: 150px minmax(0, 1fr);
      gap: 12px;
      align-items: start;
    }}
    .run-step span {{
      display: flex;
      justify-content: space-between;
      gap: 8px;
      color: var(--ink);
    }}
    .run-step em {{
      color: var(--muted);
      font-style: normal;
      font-size: 12px;
      white-space: nowrap;
    }}
    .run-step code {{
      color: var(--muted);
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }}
    @media (max-width: 800px) {{
      .topbar {{ align-items: flex-start; flex-direction: column; }}
      .period {{ white-space: normal; }}
      .metrics, .grid {{ grid-template-columns: 1fr; }}
      .trend-row {{ grid-template-columns: 66px minmax(88px, 1fr) 96px; }}
      .run-step {{ grid-template-columns: 1fr; }}
      .metric strong {{ font-size: 21px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="wrap topbar">
      <h1>家庭现金流雷达</h1>
      <div class="actions">
        <form method="post" action="/actions/refresh">
          <button type="submit">刷新数据</button>
        </form>
        <div class="period">当前月份：{html.escape(month_label)}</div>
      </div>
    </div>
  </header>
  <main class="wrap">
    {pipeline_html}
    <section class="metrics">{metrics}</section>
    <section class="grid">
      <div class="panel">
        <h2>近 12 月基础结余趋势</h2>
        {trend_html}
      </div>
      <div class="panel">
        <h2>分类审核</h2>
        <div class="review-list">
          <div class="review-item warn"><span>unknown 待审核</span><strong>{unknown_count}</strong></div>
          <div class="review-item"><span>pending 待审核</span><strong>{pending_count}</strong></div>
        </div>
      </div>
    </section>
  </main>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB
    raw_input_path = DEFAULT_RAW_INPUT

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in ("/", "/index.html"):
            self.send_error(404)
            return

        try:
            body = render_dashboard_html(self.db_path).encode("utf-8")
            status = 200
        except sqlite3.Error as exc:
            body = f"数据库读取失败: {html.escape(str(exc))}".encode("utf-8")
            status = 500

        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/actions/refresh":
            self.send_error(404)
            return

        result = run_refresh_pipeline(self.db_path, self.raw_input_path)
        try:
            body = render_dashboard_html(self.db_path, pipeline_result=result).encode("utf-8")
            status = 200 if result["ok"] else 500
        except sqlite3.Error as exc:
            body = f"数据库读取失败: {html.escape(str(exc))}".encode("utf-8")
            status = 500

        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


def run_server(db_path: Path, host: str = "127.0.0.1", port: int = 8000, input_path: Path = DEFAULT_RAW_INPUT) -> None:
    DashboardHandler.db_path = db_path
    DashboardHandler.raw_input_path = input_path
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    print(f"Using database: {db_path}")
    print(f"Using CSV input: {input_path}")
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="启动家庭现金流雷达 Web 仪表盘")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径")
    parser.add_argument("--input", default=str(DEFAULT_RAW_INPUT), help="CSV 文件或目录路径")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    args = parser.parse_args()
    run_server(Path(args.db), host=args.host, port=args.port, input_path=Path(args.input))


if __name__ == "__main__":
    main()
