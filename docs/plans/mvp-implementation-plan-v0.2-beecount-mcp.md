---
title: 家庭现金流雷达 MVP 实施计划 v0.2 - BeeCount MCP
type: implementation-plan
created: 2026-05-18
updated: 2026-05-22
status: superseded
---

# 家庭现金流雷达 MVP 实施计划 v0.2 - BeeCount MCP

> 状态：历史参考，已被 `docs/prd/prd-v0.2.md`、`docs/plans/v0.2-action-advice-plan.md` 和 `docs/plans/v0.3-beecount-cloud-source-plan.md` 取代。
>
> 取代原因：本文件以 BeeCount MCP cache、全新 planned events schema 和“CLI 完成后再做 Web”为主线；当前项目已经转为 BeeCount read API / `raw_transactions` 镜像 / 现有 Web 分析层演进。后续施工不要按本文件的严格任务顺序执行。

> 后续仓库代码改动默认交给 Claude Code 执行。本计划把 v0.1 的 CSV 主线调整为 BeeCount Cloud MCP 事实源 + 家庭现金流计划层。

## 1. 目标

最短路径：

```text
BeeCount MCP 读取已发生流水
→ 本地只读缓存
→ 计划事件表
→ 房贷 / 周期义务展开
→ 已发生流水 + 未来计划合并预测
→ CLI 摘要
→ 简单 Web 仪表盘
→ AI 分析报告
```

第一阶段不重做 BeeCount Cloud 已有的记账能力，只补 BeeCount Cloud 不具备的家庭现金流计划和预测能力。

## 2. 非目标

第一阶段不做：

- 日常记账录入；
- BeeCount 账户 / 分类 / 标签 / 预算管理复制；
- 银行、微信、支付宝自动连接；
- 未确认写回 BeeCount；
- 复杂权限系统；
- 移动端 App；
- 完整复杂图表；
- AI 自动修改事实流水。

## 3. 技术栈

第一版继续使用 boring stack：

- Python 3.11+；
- SQLite；
- pytest；
- BeeCount Cloud MCP；
- CLI 脚本；
- Web 阶段再选 FastAPI + Jinja2 或 Streamlit。

金额规则：

```text
所有金额字段统一使用整数分 *_cents INTEGER。
利率用 bps，例如 3.45% = 345。
```

## 4. 项目目录调整

建议目录：

```text
family-cashflow-radar/
├── AGENTS.md
├── README.md
├── docs/
│   ├── prd/
│   ├── design/
│   ├── plans/
│   └── logs/
├── app/
│   ├── db/
│   │   ├── schema.sql
│   │   └── seed_rules.sql
│   ├── beecount/
│   │   ├── mcp_client.py
│   │   └── sync_transactions.py
│   ├── planning/
│   │   ├── planned_events.py
│   │   ├── recurring_obligations.py
│   │   └── loan_plans.py
│   ├── forecast/
│   │   └── cashflow_forecast.py
│   └── scripts/
│       ├── sync_beecount.py
│       ├── generate_loan_schedule.py
│       ├── generate_planned_events.py
│       ├── generate_cashflow_forecast.py
│       └── print_summary.py
├── data/
│   ├── raw/
│   └── processed/
└── tests/
    └── fixtures/
```

## 5. 施工任务

### Task 0：更新项目边界文档

目标：让后续 Claude Code 不再沿 v0.1 的“貔貅 CSV 主路径”施工。

修改：

- `AGENTS.md`
- `README.md`

必须写清：

- BeeCount Cloud 是已发生流水事实源；
- 本项目通过 BeeCount MCP 读取流水；
- 本项目不重复实现记账系统；
- 本项目维护 BeeCount 缺失的计划事件、房贷计划、周期义务、现金流预测和决策模拟；
- 真实账本和本地 SQLite 数据库不得提交；
- AI 不得静默修改 BeeCount 事实流水。

验收：

```bash
python - <<'PY'
from pathlib import Path
for p in ['AGENTS.md', 'README.md']:
    text = Path(p).read_text()
    assert 'BeeCount Cloud' in text
    assert 'MCP' in text
    assert '不重复' in text or '不替代' in text
print('ok')
PY
```

### Task 1：创建 v0.2 schema.sql

目标：落地 BeeCount 缓存 + 计划现金流 schema。

必须包含：

- `beecount_transactions_cache`
- `planned_cashflow_events`
- `recurring_obligations`
- `loan_plans`
- `loan_payment_schedule`
- `cashflow_forecast_snapshots`
- `decision_scenarios`

验收：

```bash
rm -f data/processed/cashflow.db
sqlite3 data/processed/cashflow.db < app/db/schema.sql
sqlite3 data/processed/cashflow.db '.tables'
sqlite3 data/processed/cashflow.db '.schema loan_plans'
sqlite3 data/processed/cashflow.db '.schema planned_cashflow_events'
```

预期：

- 能看到以上核心表；
- 金额字段都是 `*_cents INTEGER`；
- 利率字段使用 `annual_interest_rate_bps INTEGER`；
- BeeCount 交易 ID 有唯一约束；
- 贷款计划同一贷款同一日期不重复生成。

### Task 2：建立 BeeCount MCP 客户端封装

目标：封装读取 BeeCount Cloud MCP 的最小接口。

建议文件：

- `app/beecount/mcp_client.py`
- `tests/test_beecount_mcp_client.py`

接口：

```python
class BeeCountMCPClient:
    def list_ledgers(self) -> list[dict]:
        ...

    def list_transactions(self, ledger_id: str, start_date: str, end_date: str) -> list[dict]:
        ...
```

测试要求：

- 使用 mock MCP 返回，不连接真实 BeeCount；
- 能解析交易 ID、日期、金额、方向、账户、分类、标签；
- MCP 返回缺字段时给出明确错误。

验收：

```bash
pytest tests/test_beecount_mcp_client.py -v
```

### Task 3：实现 BeeCount 交易同步缓存

目标：把 MCP 返回的已发生流水幂等写入 `beecount_transactions_cache`。

建议文件：

- `app/beecount/sync_transactions.py`
- `app/scripts/sync_beecount.py`
- `tests/test_sync_beecount.py`

CLI 契约：

```bash
python -m app.scripts.sync_beecount \
  --db data/processed/cashflow.db \
  --ledger-id <ledger_id> \
  --start-date 2026-01-01 \
  --end-date 2026-12-31 \
  --dry-run \
  --verbose
```

稳定输出：

```text
synced=120 skipped_existing=10 failed=0
```

测试要求：

- 重复同步不重复插入；
- 金额转为整数分；
- 原始 payload 保留；
- 同步失败不清空已有数据。

### Task 4：实现计划事件 CRUD 与导入

目标：能维护未来计划事件。

建议文件：

- `app/planning/planned_events.py`
- `tests/test_planned_events.py`

最低能力：

- 新增一次性未来收入 / 支出；
- 停用计划事件；
- 标记计划事件为 matched；
- 查询某个日期范围内的 active 计划事件。

测试要求：

- `amount_cents` 必须非负；
- `cashflow_direction` 只能是 `inflow`、`outflow`、`neutral`；
- matched 事件不再进入未来预测重复计算。

### Task 5：实现周期义务展开

目标：把固定账单、保险、物业、学费等周期义务展开成计划事件。

建议文件：

- `app/planning/recurring_obligations.py`
- `app/scripts/generate_planned_events.py`
- `tests/test_recurring_obligations.py`

最低能力：

- monthly；
- quarterly；
- yearly；
- 指定起止日期；
- 重复运行不重复生成。

稳定输出：

```text
planned_events_generated=12 skipped_existing=12 failed=0
```

### Task 6：实现房贷 / 贷款计划展开

目标：把贷款计划自动展开为未来还款计划和计划现金流事件。

建议文件：

- `app/planning/loan_plans.py`
- `app/scripts/generate_loan_schedule.py`
- `tests/test_loan_plans.py`

最低能力：

- 等额本息或固定月供第一版可先支持一种；
- 使用 `annual_interest_rate_bps`；
- 生成未来 N 个月还款计划；
- 生成对应 `planned_cashflow_events`；
- 重复运行不重复生成。

测试要求：

- 金额单位为分；
- 利息 / 本金拆分可解释；
- 同一贷款同一还款日不重复生成；
- 生成的计划事件可以被 BeeCount 实际流水匹配。

### Task 7：实现计划事件与 BeeCount 实际流水匹配

目标：避免计划支出和已发生支出重复计入。

建议文件：

- `app/planning/match_actuals.py`
- `tests/test_match_actuals.py`

匹配条件第一版可以保守：

- 日期相同或相差 N 天；
- 金额相同或差额在阈值内；
- 方向一致；
- 分类 / 备注关键词相似。

输出：

```text
matched=5 candidates=2 unmatched=10
```

规则：

- 自动匹配只能用于高置信度；
- 低置信度进入待确认；
- matched 后预测时不重复计算计划事件。

### Task 8：实现现金流预测

目标：合并 BeeCount 已发生流水和未来计划事件。

建议文件：

- `app/forecast/cashflow_forecast.py`
- `app/scripts/generate_cashflow_forecast.py`
- `tests/test_cashflow_forecast.py`

输入：

- 当前现金余额；
- 预测起止日期；
- BeeCount 已发生流水；
- active 且 unmatched 的未来计划事件；
- 风险阈值配置。

输出：

- 按月收入；
- 按月支出；
- 月末现金余额；
- 最低现金余额；
- 现金安全月数；
- 风险等级；
- 压力月份。

测试要求：

- matched 计划事件不会重复计算；
- 未来计划支出会降低预测现金；
- 未来计划收入会提高预测现金；
- 能识别 `safe` / `watch` / `tight` / `danger`。

### Task 9：CLI 摘要

目标：先不用复杂页面，也能回答核心问题。

建议文件：

- `app/scripts/print_summary.py`
- `tests/test_print_summary.py`

输出示例：

```text
current_cash=5000000
forecast_horizon_months=6
minimum_cash=1800000
safety_months=3.2
risk_level=watch
pressure_month=2026-08
unmatched_planned_events=4
pending_match_candidates=2
```

### Task 10：最简 Web 仪表盘

目标：在 CLI 数据闭环跑通后，再做 Web。

页面：

- 现金流首页；
- 计划事件列表；
- 房贷计划列表；
- 预测结果页；
- 待匹配交易页。

验收：

- 能看到 BeeCount 已发生流水汇总；
- 能看到未来计划事件；
- 能看到未来 3 / 6 / 12 个月风险；
- 能看到计划事件和实际流水匹配状态。

## 6. 测试总要求

最低测试：

```text
tests/test_schema.py
tests/test_beecount_mcp_client.py
tests/test_sync_beecount.py
tests/test_planned_events.py
tests/test_recurring_obligations.py
tests/test_loan_plans.py
tests/test_match_actuals.py
tests/test_cashflow_forecast.py
tests/test_print_summary.py
```

禁止用真实家庭账本做测试断言。测试必须使用 synthetic fixtures 或 mock MCP 返回。

## 7. 执行顺序

严格顺序：

```text
Task 0 文档边界
Task 1 schema
Task 2 MCP client mock
Task 3 sync cache
Task 4 planned events
Task 5 recurring obligations
Task 6 loan plans
Task 7 matching
Task 8 forecast
Task 9 CLI summary
Task 10 Web dashboard
```

Web 必须等 CLI 数据闭环和 pytest 通过后再做。
