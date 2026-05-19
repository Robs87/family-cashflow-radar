---
title: BeeCount MCP 与计划现金流设计 v0.1
type: design
created: 2026-05-18
updated: 2026-05-18
status: draft
---

# BeeCount MCP 与计划现金流设计 v0.1

## 1. 设计原则

家庭现金流雷达的数据分两类：

```text
已发生事实：来自 BeeCount Cloud MCP。
未来计划：由家庭现金流雷达维护。
```

两类数据必须分开存储、分开标记、合并分析。

核心原则：

1. BeeCount Cloud 是已发生流水的事实源。
2. 家庭现金流雷达不重复实现记账系统。
3. 本地数据库只是分析缓存和计划数据存储。
4. 未来计划事件不能伪装成 BeeCount 已发生交易。
5. 已发生交易和计划事件匹配后，预测时必须去重。
6. 所有标准化金额字段使用整数分 `*_cents INTEGER`。
7. AI 只能解释、建议和生成待确认草案，不能静默修改事实源。

## 2. 数据层分工

### 2.1 BeeCount Cloud

BeeCount Cloud 负责：

- 交易流水；
- 账户；
- 分类；
- 标签；
- 预算等基础账本能力；
- MCP 查询接口。

家庭现金流雷达通过 MCP 读取 BeeCount 数据，不把自己变成新的流水录入系统。

### 2.2 家庭现金流雷达本地库

本地库负责：

- 缓存 BeeCount 交易，便于分析；
- 存储未来计划事件；
- 存储周期性义务；
- 存储房贷 / 贷款计划；
- 存储预测结果；
- 存储决策模拟结果；
- 存储 AI 分析快照和待确认建议。

## 3. 建议表结构

### 3.1 `beecount_transactions_cache`

缓存 BeeCount 已发生流水。

```sql
CREATE TABLE IF NOT EXISTS beecount_transactions_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    beecount_transaction_id TEXT NOT NULL UNIQUE,
    ledger_id TEXT,
    transaction_time TEXT,
    transaction_date TEXT NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    cashflow_direction TEXT NOT NULL,
    account_id TEXT,
    account_name TEXT,
    category_id TEXT,
    category_name TEXT,
    tags_json TEXT,
    merchant TEXT,
    note TEXT,
    raw_payload TEXT NOT NULL,
    synced_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_beecount_tx_date ON beecount_transactions_cache(transaction_date);
CREATE INDEX IF NOT EXISTS idx_beecount_tx_year_month ON beecount_transactions_cache(year, month);
CREATE INDEX IF NOT EXISTS idx_beecount_tx_direction ON beecount_transactions_cache(cashflow_direction);
```

说明：

- `beecount_transaction_id` 来自 BeeCount MCP，保证重复同步不重复落库。
- `raw_payload` 保留原始 MCP 返回，便于追溯。
- 本表是缓存，不是新的事实源。

### 3.2 `planned_cashflow_events`

存储未来计划事件。

```sql
CREATE TABLE IF NOT EXISTS planned_cashflow_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_name TEXT NOT NULL,
    event_date TEXT NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL,
    cashflow_direction TEXT NOT NULL,
    event_type TEXT NOT NULL,
    certainty_level TEXT NOT NULL DEFAULT 'confirmed',
    source_type TEXT NOT NULL DEFAULT 'manual',
    source_id INTEGER,
    recurrence_id INTEGER,
    matched_beecount_transaction_id TEXT,
    match_status TEXT NOT NULL DEFAULT 'unmatched',
    status TEXT NOT NULL DEFAULT 'active',
    note TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_planned_event_date ON planned_cashflow_events(event_date);
CREATE INDEX IF NOT EXISTS idx_planned_event_year_month ON planned_cashflow_events(year, month);
CREATE INDEX IF NOT EXISTS idx_planned_event_match_status ON planned_cashflow_events(match_status);
```

字段说明：

- `event_type`：例如 `salary_expected`、`mortgage_payment`、`insurance`、`tuition`、`large_expense`、`one_time_income`。
- `certainty_level`：`confirmed`、`likely`、`assumption`。
- `source_type`：`manual`、`loan_plan`、`recurring_obligation`、`ai_suggestion`。
- `match_status`：`unmatched`、`matched`、`ignored`、`cancelled`。

### 3.3 `recurring_obligations`

存储周期性义务，例如保险、物业、学费、固定订阅。

```sql
CREATE TABLE IF NOT EXISTS recurring_obligations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    obligation_name TEXT NOT NULL,
    obligation_type TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    cashflow_direction TEXT NOT NULL DEFAULT 'outflow',
    frequency TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT,
    day_of_month INTEGER,
    next_due_date TEXT,
    certainty_level TEXT NOT NULL DEFAULT 'confirmed',
    status TEXT NOT NULL DEFAULT 'active',
    note TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_recurring_status ON recurring_obligations(status);
CREATE INDEX IF NOT EXISTS idx_recurring_next_due ON recurring_obligations(next_due_date);
```

### 3.4 `loan_plans`

存储房贷 / 贷款计划。

```sql
CREATE TABLE IF NOT EXISTS loan_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_name TEXT NOT NULL,
    loan_type TEXT NOT NULL,
    principal_initial_cents INTEGER NOT NULL,
    principal_current_cents INTEGER NOT NULL,
    annual_interest_rate_bps INTEGER NOT NULL,
    repayment_method TEXT NOT NULL,
    monthly_payment_cents INTEGER,
    payment_day INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT,
    lender TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    note TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_loan_status ON loan_plans(status);
```

说明：

- 利率用基点 `bps` 表达，避免浮点误差。例如 3.45% = 345 bps。
- 每月还款金额用分。

### 3.5 `loan_payment_schedule`

存储贷款还款计划展开结果。

```sql
CREATE TABLE IF NOT EXISTS loan_payment_schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_plan_id INTEGER NOT NULL,
    due_date TEXT NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    scheduled_payment_cents INTEGER NOT NULL,
    principal_cents INTEGER,
    interest_cents INTEGER,
    remaining_principal_cents INTEGER,
    planned_event_id INTEGER,
    matched_beecount_transaction_id TEXT,
    match_status TEXT NOT NULL DEFAULT 'unmatched',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(loan_plan_id) REFERENCES loan_plans(id),
    FOREIGN KEY(planned_event_id) REFERENCES planned_cashflow_events(id),
    UNIQUE(loan_plan_id, due_date)
);

CREATE INDEX IF NOT EXISTS idx_loan_schedule_due_date ON loan_payment_schedule(due_date);
CREATE INDEX IF NOT EXISTS idx_loan_schedule_match ON loan_payment_schedule(match_status);
```

### 3.6 `cashflow_forecast_snapshots`

存储预测快照。

```sql
CREATE TABLE IF NOT EXISTS cashflow_forecast_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_name TEXT NOT NULL,
    generated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    horizon_months INTEGER NOT NULL,
    opening_cash_cents INTEGER,
    minimum_cash_cents INTEGER,
    safety_months REAL,
    risk_level TEXT NOT NULL,
    pressure_month TEXT,
    assumptions_json TEXT,
    result_json TEXT NOT NULL
);
```

说明：

- `safety_months` 是比例指标，可以用 `REAL`；金额仍然必须用 `*_cents INTEGER`。
- `result_json` 保存按月 / 按日预测结果。

### 3.7 `decision_scenarios`

存储大额决策模拟。

```sql
CREATE TABLE IF NOT EXISTS decision_scenarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_name TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    payment_type TEXT NOT NULL,
    installment_months INTEGER,
    monthly_payment_cents INTEGER,
    expected_income_impact_cents INTEGER DEFAULT 0,
    expected_expense_impact_cents INTEGER DEFAULT 0,
    result_risk_level TEXT,
    result_min_cash_cents INTEGER,
    result_min_safety_months REAL,
    recommendation TEXT,
    explanation TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

## 4. 预测合并规则

现金流预测输入分三类：

1. BeeCount 已发生流水；
2. 未匹配的未来计划事件；
3. 由房贷 / 周期义务自动展开出来的计划事件。

合并规则：

```text
如果 planned_cashflow_events.match_status = matched，预测时不再把该计划事件作为未来支出重复计算。
如果 BeeCount 交易已经发生，历史月份以 BeeCount 实际流水为准。
如果计划事件尚未匹配，并且 event_date >= today，则进入未来预测。
```

## 5. MCP 同步契约

BeeCount MCP 同步应满足：

- 支持按账本选择；
- 支持按日期范围读取；
- 支持增量同步；
- 保存 BeeCount 原始交易 ID；
- 重复同步不重复插入；
- 同步失败不破坏已有缓存；
- 不提交真实账本数据到 git。

## 6. AI 安全边界

AI 生成内容分三类：

1. 分析报告：可直接输出。
2. 计划建议：必须进入待确认状态。
3. 写回 BeeCount 的建议：必须由用户确认后执行。

AI 建议不得直接成为事实。所有建议都要能追溯到：

- BeeCount 流水；
- 计划事件；
- 贷款计划；
- 预测参数。
