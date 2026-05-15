-- Family Cashflow Radar - SQLite Schema v0.1
-- All monetary amounts in normalized/aggregated tables use *_cents INTEGER (non-negative).
-- raw_transactions preserves original text amounts for audit.

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ============================================================
-- 1. raw_transactions: 原始交易
-- ============================================================
CREATE TABLE IF NOT EXISTS raw_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    source_row_no INTEGER NOT NULL,
    transaction_time TEXT,
    transaction_date TEXT,
    amount_original TEXT,
    income_amount_original TEXT,
    expense_amount_original TEXT,
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
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

-- ============================================================
-- 2. normalized_transactions: 标准化交易
-- ============================================================
CREATE TABLE IF NOT EXISTS normalized_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_transaction_id INTEGER NOT NULL UNIQUE,
    transaction_date TEXT NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    cashflow_direction TEXT NOT NULL CHECK(cashflow_direction IN ('inflow', 'outflow', 'neutral')),
    financial_type TEXT NOT NULL CHECK(financial_type IN (
        'stable_income', 'one_time_income', 'living_expense', 'fixed_expense',
        'debt_payment', 'debt_inflow', 'asset_purchase', 'asset_sale',
        'investment_outflow', 'investment_inflow', 'internal_transfer',
        'credit_card_payment', 'refund', 'historical_debt_asset_event', 'unknown'
    )),
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
    review_status TEXT DEFAULT 'pending' CHECK(review_status IN (
        'pending', 'approved', 'rejected', 'needs_review'
    )),
    -- manual override fields
    manual_financial_type TEXT CHECK(manual_financial_type IS NULL OR manual_financial_type IN (
        'stable_income', 'one_time_income', 'living_expense', 'fixed_expense',
        'debt_payment', 'debt_inflow', 'asset_purchase', 'asset_sale',
        'investment_outflow', 'investment_inflow', 'internal_transfer',
        'credit_card_payment', 'refund', 'historical_debt_asset_event', 'unknown'
    )),
    manual_category_l1 TEXT,
    manual_category_l2 TEXT,
    manual_cashflow_direction TEXT CHECK(manual_cashflow_direction IS NULL OR manual_cashflow_direction IN (
        'inflow', 'outflow', 'neutral'
    )),
    manual_note TEXT,
    manual_updated_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(raw_transaction_id) REFERENCES raw_transactions(id),
    FOREIGN KEY(classification_rule_id) REFERENCES classification_rules(id)
);

CREATE INDEX IF NOT EXISTS idx_norm_year_month ON normalized_transactions(year, month);
CREATE INDEX IF NOT EXISTS idx_norm_financial_type ON normalized_transactions(financial_type);
CREATE INDEX IF NOT EXISTS idx_norm_review_status ON normalized_transactions(review_status);
CREATE INDEX IF NOT EXISTS idx_norm_direction_type_month
    ON normalized_transactions(cashflow_direction, financial_type, year, month);

-- ============================================================
-- 3. classification_rules: 分类规则
-- ============================================================
CREATE TABLE IF NOT EXISTS classification_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_name TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    condition_json TEXT NOT NULL,
    target_cashflow_direction TEXT NOT NULL CHECK(target_cashflow_direction IN ('inflow', 'outflow', 'neutral')),
    target_financial_type TEXT NOT NULL CHECK(target_financial_type IN (
        'stable_income', 'one_time_income', 'living_expense', 'fixed_expense',
        'debt_payment', 'debt_inflow', 'asset_purchase', 'asset_sale',
        'investment_outflow', 'investment_inflow', 'internal_transfer',
        'credit_card_payment', 'refund', 'historical_debt_asset_event', 'unknown'
    )),
    target_category_l1 TEXT,
    target_category_l2 TEXT,
    is_internal_transfer INTEGER DEFAULT 0,
    is_debt_related INTEGER DEFAULT 0,
    is_asset_related INTEGER DEFAULT 0,
    is_investment_related INTEGER DEFAULT 0,
    confidence REAL DEFAULT 0.9,
    enabled INTEGER DEFAULT 1,
    description TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_rules_priority ON classification_rules(priority);
CREATE INDEX IF NOT EXISTS idx_rules_enabled_priority ON classification_rules(enabled, priority);

-- ============================================================
-- 4. monthly_cashflow: 月度现金流
-- ============================================================
CREATE TABLE IF NOT EXISTS monthly_cashflow (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
    stable_income_cents INTEGER DEFAULT 0 CHECK(stable_income_cents >= 0),
    one_time_income_cents INTEGER DEFAULT 0 CHECK(one_time_income_cents >= 0),
    total_real_income_cents INTEGER DEFAULT 0 CHECK(total_real_income_cents >= 0),
    fixed_expense_cents INTEGER DEFAULT 0 CHECK(fixed_expense_cents >= 0),
    living_expense_cents INTEGER DEFAULT 0 CHECK(living_expense_cents >= 0),
    debt_payment_cents INTEGER DEFAULT 0 CHECK(debt_payment_cents >= 0),
    investment_outflow_cents INTEGER DEFAULT 0 CHECK(investment_outflow_cents >= 0),
    investment_inflow_cents INTEGER DEFAULT 0 CHECK(investment_inflow_cents >= 0),
    asset_purchase_cents INTEGER DEFAULT 0 CHECK(asset_purchase_cents >= 0),
    asset_sale_cents INTEGER DEFAULT 0 CHECK(asset_sale_cents >= 0),
    refund_cents INTEGER DEFAULT 0 CHECK(refund_cents >= 0),
    internal_transfer_cents INTEGER DEFAULT 0 CHECK(internal_transfer_cents >= 0),
    credit_card_payment_cents INTEGER DEFAULT 0 CHECK(credit_card_payment_cents >= 0),
    debt_inflow_cents INTEGER DEFAULT 0 CHECK(debt_inflow_cents >= 0),
    net_operating_cashflow_cents INTEGER DEFAULT 0,
    net_total_cashflow_cents INTEGER DEFAULT 0,
    cashflow_health_score REAL,
    generated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(year, month)
);

-- ============================================================
-- 5. asset_events: 资产事件
-- ============================================================
CREATE TABLE IF NOT EXISTS asset_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date TEXT NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    asset_type TEXT NOT NULL,
    asset_name TEXT,
    event_type TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    linked_transaction_ids TEXT,
    description TEXT,
    is_one_time INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- 6. debts: 债务
-- ============================================================
CREATE TABLE IF NOT EXISTS debts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    debt_name TEXT NOT NULL,
    debt_type TEXT NOT NULL,
    principal_initial_cents INTEGER CHECK(principal_initial_cents >= 0),
    principal_current_cents INTEGER CHECK(principal_current_cents >= 0),
    monthly_payment_cents INTEGER CHECK(monthly_payment_cents >= 0),
    interest_rate REAL,
    start_date TEXT,
    end_date TEXT,
    lender TEXT,
    status TEXT DEFAULT 'active',
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- 7. cashflow_forecast: 现金流预测
-- ============================================================
CREATE TABLE IF NOT EXISTS cashflow_forecast (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_month TEXT NOT NULL,
    opening_cash_cents INTEGER DEFAULT 0 CHECK(opening_cash_cents >= 0),
    expected_stable_income_cents INTEGER DEFAULT 0 CHECK(expected_stable_income_cents >= 0),
    expected_one_time_income_cents INTEGER DEFAULT 0 CHECK(expected_one_time_income_cents >= 0),
    expected_fixed_expense_cents INTEGER DEFAULT 0 CHECK(expected_fixed_expense_cents >= 0),
    expected_living_expense_cents INTEGER DEFAULT 0 CHECK(expected_living_expense_cents >= 0),
    expected_debt_payment_cents INTEGER DEFAULT 0 CHECK(expected_debt_payment_cents >= 0),
    expected_large_expense_cents INTEGER DEFAULT 0 CHECK(expected_large_expense_cents >= 0),
    closing_cash_cents INTEGER DEFAULT 0 CHECK(closing_cash_cents >= 0),
    safety_months REAL DEFAULT 0,
    risk_level TEXT DEFAULT 'unknown',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- 8. decision_scenarios: 决策模拟
-- ============================================================
CREATE TABLE IF NOT EXISTS decision_scenarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_name TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    start_month TEXT NOT NULL,
    payment_type TEXT NOT NULL,
    installment_months INTEGER,
    monthly_payment_cents INTEGER CHECK(monthly_payment_cents >= 0),
    expected_income_impact_cents INTEGER DEFAULT 0 CHECK(expected_income_impact_cents >= 0),
    expected_expense_impact_cents INTEGER DEFAULT 0 CHECK(expected_expense_impact_cents >= 0),
    result_risk_level TEXT,
    result_min_cash_cents INTEGER CHECK(result_min_cash_cents >= 0),
    result_min_safety_months REAL,
    recommendation TEXT,
    explanation TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
