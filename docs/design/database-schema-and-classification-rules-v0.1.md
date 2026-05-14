---
title: 家庭现金流 App 数据库 Schema 与自动分类规则 v0.1
created: 2026-05-15
updated: 2026-05-15
type: design
---

# 数据库 Schema 与自动分类规则 v0.1

目标：每一笔账都能被稳定翻译成收入、支出、债务、资产、投资、内部转账、退款或未知。

## 1. 数据库总结构

SQLite v0.1 建议 8 张表：

- `raw_transactions`：原始交易表
- `normalized_transactions`：标准化交易表
- `classification_rules`：分类规则表
- `asset_events`：资产事件表
- `debts`：债务表
- `monthly_cashflow`：月度现金流表
- `cashflow_forecast`：现金流预测表
- `decision_scenarios`：决策模拟表

第一阶段必须先跑通前 4 张：

- `raw_transactions`
- `normalized_transactions`
- `classification_rules`
- `monthly_cashflow`

## 2. SQLite Schema

### 2.1 raw_transactions

```sql
CREATE TABLE IF NOT EXISTS raw_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    source_row_no INTEGER NOT NULL,
    transaction_time TEXT,
    transaction_date TEXT,
    amount REAL NOT NULL,
    direction_raw TEXT,
    account TEXT,
    category_original TEXT,
    subcategory_original TEXT,
    merchant TEXT,
    note TEXT,
    project TEXT,
    tags TEXT,
    raw_payload TEXT,
    raw_hash TEXT NOT NULL UNIQUE,
    imported_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_raw_transaction_date ON raw_transactions(transaction_date);
CREATE INDEX IF NOT EXISTS idx_raw_hash ON raw_transactions(raw_hash);
```

### 2.2 normalized_transactions

```sql
CREATE TABLE IF NOT EXISTS normalized_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_transaction_id INTEGER NOT NULL,
    transaction_date TEXT NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    amount REAL NOT NULL,
    cashflow_direction TEXT NOT NULL,
    financial_type TEXT NOT NULL,
    category_l1 TEXT,
    category_l2 TEXT,
    account TEXT,
    counterparty TEXT,
    description TEXT,
    is_recurring INTEGER DEFAULT 0,
    is_large_amount INTEGER DEFAULT 0,
    is_internal_transfer INTEGER DEFAULT 0,
    is_debt_related INTEGER DEFAULT 0,
    is_asset_related INTEGER DEFAULT 0,
    is_investment_related INTEGER DEFAULT 0,
    classification_rule_id INTEGER,
    confidence REAL DEFAULT 0,
    review_status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(raw_transaction_id) REFERENCES raw_transactions(id),
    FOREIGN KEY(classification_rule_id) REFERENCES classification_rules(id)
);

CREATE INDEX IF NOT EXISTS idx_norm_year_month ON normalized_transactions(year, month);
CREATE INDEX IF NOT EXISTS idx_norm_financial_type ON normalized_transactions(financial_type);
CREATE INDEX IF NOT EXISTS idx_norm_review_status ON normalized_transactions(review_status);
```

### 2.3 classification_rules

```sql
CREATE TABLE IF NOT EXISTS classification_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_name TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    match_field TEXT NOT NULL,
    match_pattern TEXT NOT NULL,
    amount_min REAL,
    amount_max REAL,
    direction_raw TEXT,
    account_pattern TEXT,
    target_cashflow_direction TEXT NOT NULL,
    target_financial_type TEXT NOT NULL,
    target_category_l1 TEXT,
    target_category_l2 TEXT,
    is_internal_transfer INTEGER DEFAULT 0,
    is_debt_related INTEGER DEFAULT 0,
    is_asset_related INTEGER DEFAULT 0,
    is_investment_related INTEGER DEFAULT 0,
    confidence REAL DEFAULT 0.9,
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_rules_priority ON classification_rules(priority);
CREATE INDEX IF NOT EXISTS idx_rules_enabled ON classification_rules(enabled);
```

### 2.4 monthly_cashflow

```sql
CREATE TABLE IF NOT EXISTS monthly_cashflow (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    stable_income REAL DEFAULT 0,
    one_time_income REAL DEFAULT 0,
    total_real_income REAL DEFAULT 0,
    fixed_expense REAL DEFAULT 0,
    living_expense REAL DEFAULT 0,
    debt_payment REAL DEFAULT 0,
    investment_outflow REAL DEFAULT 0,
    investment_inflow REAL DEFAULT 0,
    asset_purchase REAL DEFAULT 0,
    asset_sale REAL DEFAULT 0,
    refund REAL DEFAULT 0,
    internal_transfer_amount REAL DEFAULT 0,
    credit_card_payment_amount REAL DEFAULT 0,
    net_operating_cashflow REAL DEFAULT 0,
    net_total_cashflow REAL DEFAULT 0,
    cashflow_health_score REAL,
    generated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(year, month)
);
```

### 2.5 asset_events

```sql
CREATE TABLE IF NOT EXISTS asset_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date TEXT NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    asset_type TEXT NOT NULL,
    asset_name TEXT,
    event_type TEXT NOT NULL,
    amount REAL NOT NULL,
    linked_transaction_ids TEXT,
    description TEXT,
    is_one_time INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

### 2.6 debts

```sql
CREATE TABLE IF NOT EXISTS debts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    debt_name TEXT NOT NULL,
    debt_type TEXT NOT NULL,
    principal_initial REAL,
    principal_current REAL,
    monthly_payment REAL,
    interest_rate REAL,
    start_date TEXT,
    end_date TEXT,
    lender TEXT,
    status TEXT DEFAULT 'active',
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

### 2.7 cashflow_forecast

```sql
CREATE TABLE IF NOT EXISTS cashflow_forecast (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_month TEXT NOT NULL,
    opening_cash REAL DEFAULT 0,
    expected_stable_income REAL DEFAULT 0,
    expected_one_time_income REAL DEFAULT 0,
    expected_fixed_expense REAL DEFAULT 0,
    expected_living_expense REAL DEFAULT 0,
    expected_debt_payment REAL DEFAULT 0,
    expected_large_expense REAL DEFAULT 0,
    closing_cash REAL DEFAULT 0,
    safety_months REAL DEFAULT 0,
    risk_level TEXT DEFAULT 'unknown',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

### 2.8 decision_scenarios

```sql
CREATE TABLE IF NOT EXISTS decision_scenarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_name TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    amount REAL NOT NULL,
    start_month TEXT NOT NULL,
    payment_type TEXT NOT NULL,
    installment_months INTEGER,
    monthly_payment REAL,
    expected_income_impact REAL DEFAULT 0,
    expected_expense_impact REAL DEFAULT 0,
    result_risk_level TEXT,
    result_min_cash REAL,
    result_min_safety_months REAL,
    recommendation TEXT,
    explanation TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

## 3. 核心枚举

### 3.1 cashflow_direction

- `inflow`：真实现金流入
- `outflow`：真实现金流出
- `neutral`：内部流转，不计入真实收支

### 3.2 financial_type

- `stable_income`
- `one_time_income`
- `living_expense`
- `fixed_expense`
- `debt_payment`
- `debt_inflow`
- `asset_purchase`
- `asset_sale`
- `investment_outflow`
- `investment_inflow`
- `internal_transfer`
- `credit_card_payment`
- `refund`
- `historical_debt_asset_event`
- `unknown`

## 4. CSV 字段映射规则

字段别名识别：

- 时间 / 日期 / 交易时间 / 记账时间 → `transaction_time`
- 金额 / 金额(元) / 支出金额 / 收入金额 → `amount`
- 类型 / 收支 / 收支类型 / 方向 → `direction_raw`
- 账户 / 账户名 / 钱包 / 支付账户 → `account`
- 分类 / 一级分类 → `category_original`
- 子分类 / 二级分类 → `subcategory_original`
- 商户 / 对方 / 收款方 / 付款方 → `merchant`
- 备注 / 说明 / 描述 → `note`

## 5. 去重 Hash 规则

拼接字段：

```text
transaction_time | amount | direction_raw | account | category_original | subcategory_original | merchant | note
```

然后做 SHA256。

重复导入同一 CSV，不应重复产生交易。

## 6. 自动分类规则 v0.1

规则优先级：

1. 2021、2022 历史债务资产事件
2. 内部转账
3. 信用卡还款
4. 债务还款
5. 借入资金
6. 投资流入/流出
7. 资产购入/出售
8. 稳定收入
9. 固定刚性支出
10. 日常生活支出
11. unknown

### 6.1 2021、2022 历史规则

条件：`year IN (2021, 2022)`

结果：

- `financial_type = historical_debt_asset_event`
- `confidence = 0.7`

### 6.2 内部转账

关键词：转账、账户转账、余额宝转入、余额宝转出、微信零钱、支付宝余额、银行卡转入、银行卡转出、提现、充值。

结果：

- `financial_type = internal_transfer`
- `cashflow_direction = neutral`
- `is_internal_transfer = 1`
- `confidence = 0.9`

### 6.3 信用卡还款

关键词：信用卡还款、还信用卡、信用卡自动还款、账单还款。

结果：

- `financial_type = credit_card_payment`
- `cashflow_direction = neutral`
- `is_debt_related = 1`
- `confidence = 0.95`

### 6.4 房贷 / 车贷 / 债务还款

关键词：房贷、按揭、贷款还款、月供、还贷、车贷、汽车金融、特斯拉金融。

结果：

- `financial_type = debt_payment`
- `cashflow_direction = outflow`
- `category_l1 = 债务`
- `is_debt_related = 1`
- `confidence = 0.95`

### 6.5 借入资金

关键词：借款、借入、借钱、周转、垫付、亲友借款、贷款到账。

方向为收入时：

- `financial_type = debt_inflow`
- `cashflow_direction = inflow`
- `category_l1 = 债务`
- `category_l2 = 借入资金`
- `confidence = 0.85`

### 6.6 投资流出 / 流入

流出关键词：基金、股票、证券、理财、定投、买入、申购、USDT、币安、欧易、OKX。

流入关键词：赎回、卖出、分红、理财到账、基金赎回、股票卖出、证券转出。

结果：

- `investment_outflow` 或 `investment_inflow`
- `category_l1 = 投资`
- `confidence = 0.9`

### 6.7 资产购入 / 出售

购入关键词：特斯拉、Tesla、车辆购置、购车、首付、汽车、设备。

出售关键词：卖出、二手、闲鱼、转卖、出售、回收、卖车、卖设备。

结果：

- `asset_purchase` 或 `asset_sale`
- `category_l1 = 资产购入` 或 `资产出售`
- `confidence = 0.85 到 0.95`

### 6.8 稳定收入 / 一次性收入 / 退款

稳定收入关键词：工资、薪资、绩效、劳务费、公司转账、项目款。

一次性收入关键词：奖金、年终奖、报销、补贴、红包、礼金、临时收入。

退款关键词：退款、退货、退费、返现、冲正、撤销。

## 7. 月度现金流计算

### 7.1 基础经营现金流

```text
net_operating_cashflow = stable_income - fixed_expense - living_expense - debt_payment
```

这里不用一次性收入。因为要看不靠奖金、不靠借钱、不靠卖资产，家庭基本盘能否自洽。

### 7.2 总现金流

```text
net_total_cashflow = total_real_income + investment_inflow + asset_sale + debt_inflow + refund - fixed_expense - living_expense - debt_payment - investment_outflow - asset_purchase
```

内部转账和信用卡还款不计入。

## 8. 现金流健康评分 v0.1

### 8.1 基础结余率

```text
基础结余率 = net_operating_cashflow / stable_income
```

- >= 30%：健康
- 10% 到 30%：可接受
- 0% 到 10%：偏紧
- < 0%：危险

### 8.2 债务压力率

```text
债务压力率 = debt_payment / stable_income
```

- <= 30%：健康
- 30% 到 50%：偏高
- 50% 到 70%：高压
- > 70%：危险

### 8.3 刚性支出压力率

```text
刚性支出压力率 = (fixed_expense + debt_payment) / stable_income
```

- <= 50%：健康
- 50% 到 70%：偏紧
- 70% 到 90%：高压
- > 90%：危险
