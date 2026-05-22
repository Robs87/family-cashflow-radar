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
    source_system TEXT,
    source_ledger_id TEXT,
    source_transaction_id TEXT,
    source_updated_at TEXT,
    source_deleted_at TEXT,
    source_is_latest INTEGER NOT NULL DEFAULT 1 CHECK(source_is_latest IN (0, 1)),
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
        'credit_card_payment', 'refund', 'reimbursable_expense',
        'reimbursement_income', 'historical_debt_asset_event', 'unknown'
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
        'credit_card_payment', 'refund', 'reimbursable_expense',
        'reimbursement_income', 'historical_debt_asset_event', 'unknown'
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
        'credit_card_payment', 'refund', 'reimbursable_expense',
        'reimbursement_income', 'historical_debt_asset_event', 'unknown'
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
-- 3.1. beecount_category_mappings: BeeCount 分类到现金流语义
-- ============================================================
CREATE TABLE IF NOT EXISTS beecount_category_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    beecount_kind TEXT NOT NULL CHECK(beecount_kind IN ('expense', 'income', 'transfer')),
    category_name TEXT NOT NULL,
    parent_name TEXT DEFAULT '',
    level INTEGER DEFAULT 1,
    radar_cashflow_direction TEXT NOT NULL CHECK(radar_cashflow_direction IN ('inflow', 'outflow', 'neutral')),
    radar_financial_type TEXT NOT NULL CHECK(radar_financial_type IN (
        'stable_income', 'one_time_income', 'living_expense', 'fixed_expense',
        'debt_payment', 'debt_inflow', 'asset_purchase', 'asset_sale',
        'investment_outflow', 'investment_inflow', 'internal_transfer',
        'credit_card_payment', 'refund', 'reimbursable_expense',
        'reimbursement_income', 'historical_debt_asset_event', 'unknown'
    )),
    radar_category_l1 TEXT DEFAULT '',
    radar_category_l2 TEXT DEFAULT '',
    confidence REAL DEFAULT 1.0,
    enabled INTEGER DEFAULT 1,
    mapping_source TEXT DEFAULT 'inferred',
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(beecount_kind, category_name, parent_name)
);

CREATE INDEX IF NOT EXISTS idx_beecount_category_mappings_lookup
    ON beecount_category_mappings(beecount_kind, category_name, enabled);

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
    reimbursable_expense_cents INTEGER DEFAULT 0 CHECK(reimbursable_expense_cents >= 0),
    reimbursement_income_cents INTEGER DEFAULT 0 CHECK(reimbursement_income_cents >= 0),
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

-- ============================================================
-- 9. recurring_bill_templates: 周期性账单模板
-- ============================================================
CREATE TABLE IF NOT EXISTS recurring_bill_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    bill_type TEXT NOT NULL CHECK(bill_type IN ('mortgage', 'fixed_bill')),
    amount_cents INTEGER CHECK(amount_cents IS NULL OR amount_cents >= 0),
    cashflow_direction TEXT NOT NULL CHECK(cashflow_direction IN ('inflow', 'outflow', 'neutral')),
    financial_type TEXT NOT NULL CHECK(financial_type IN (
        'stable_income', 'one_time_income', 'living_expense', 'fixed_expense',
        'debt_payment', 'debt_inflow', 'asset_purchase', 'asset_sale',
        'investment_outflow', 'investment_inflow', 'internal_transfer',
        'credit_card_payment', 'refund', 'reimbursable_expense',
        'reimbursement_income', 'historical_debt_asset_event', 'unknown'
    )),
    category_l1 TEXT,
    category_l2 TEXT,
    account TEXT,
    start_date TEXT NOT NULL,
    end_date TEXT,
    schedule_type TEXT NOT NULL DEFAULT 'monthly' CHECK(schedule_type = 'monthly'),
    day_of_month INTEGER NOT NULL CHECK(day_of_month BETWEEN 1 AND 31),
    debt_id INTEGER,
    enabled INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(debt_id) REFERENCES debts(id)
);

CREATE INDEX IF NOT EXISTS idx_recurring_templates_enabled
    ON recurring_bill_templates(enabled, bill_type);

-- ============================================================
-- 10. mortgage_repayment_schedule: 房贷还款计划
-- ============================================================
CREATE TABLE IF NOT EXISTS mortgage_repayment_schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recurring_template_id INTEGER NOT NULL,
    debt_id INTEGER NOT NULL,
    period_no INTEGER NOT NULL CHECK(period_no >= 1),
    due_date TEXT NOT NULL,
    payment_cents INTEGER NOT NULL CHECK(payment_cents >= 0),
    principal_cents INTEGER NOT NULL CHECK(principal_cents >= 0),
    interest_cents INTEGER NOT NULL CHECK(interest_cents >= 0),
    fee_cents INTEGER NOT NULL DEFAULT 0 CHECK(fee_cents >= 0),
    remaining_principal_cents INTEGER NOT NULL CHECK(remaining_principal_cents >= 0),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(recurring_template_id, period_no),
    UNIQUE(recurring_template_id, due_date),
    FOREIGN KEY(recurring_template_id) REFERENCES recurring_bill_templates(id),
    FOREIGN KEY(debt_id) REFERENCES debts(id),
    CHECK(payment_cents = principal_cents + interest_cents + fee_cents)
);

CREATE INDEX IF NOT EXISTS idx_mortgage_schedule_due_date
    ON mortgage_repayment_schedule(due_date);

-- ============================================================
-- 11. recurring_bill_instances: 周期账单生成结果
-- ============================================================
CREATE TABLE IF NOT EXISTS recurring_bill_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recurring_template_id INTEGER NOT NULL,
    due_date TEXT NOT NULL,
    normalized_transaction_id INTEGER NOT NULL UNIQUE,
    schedule_id INTEGER,
    generated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(recurring_template_id, due_date),
    FOREIGN KEY(recurring_template_id) REFERENCES recurring_bill_templates(id),
    FOREIGN KEY(normalized_transaction_id) REFERENCES normalized_transactions(id),
    FOREIGN KEY(schedule_id) REFERENCES mortgage_repayment_schedule(id)
);

-- ============================================================
-- 12. debt_payment_splits: 债务还款本金/利息拆分
-- ============================================================
CREATE TABLE IF NOT EXISTS debt_payment_splits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_transaction_id INTEGER NOT NULL UNIQUE,
    debt_id INTEGER,
    principal_cents INTEGER NOT NULL CHECK(principal_cents >= 0),
    interest_cents INTEGER NOT NULL CHECK(interest_cents >= 0),
    fee_cents INTEGER NOT NULL DEFAULT 0 CHECK(fee_cents >= 0),
    remaining_principal_cents INTEGER CHECK(remaining_principal_cents IS NULL OR remaining_principal_cents >= 0),
    note TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(normalized_transaction_id) REFERENCES normalized_transactions(id),
    FOREIGN KEY(debt_id) REFERENCES debts(id)
);

CREATE INDEX IF NOT EXISTS idx_debt_payment_splits_debt
    ON debt_payment_splits(debt_id);

-- ============================================================
-- 13. mortgage_prepayment_events: 房贷提前还款事件
-- ============================================================
CREATE TABLE IF NOT EXISTS mortgage_prepayment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recurring_template_id INTEGER NOT NULL,
    debt_id INTEGER NOT NULL,
    prepayment_date TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    effect_type TEXT NOT NULL CHECK(effect_type IN ('reduce_term', 'reduce_payment')),
    remaining_principal_before_cents INTEGER NOT NULL CHECK(remaining_principal_before_cents >= 0),
    remaining_principal_after_cents INTEGER NOT NULL CHECK(remaining_principal_after_cents >= 0),
    generated_normalized_transaction_id INTEGER UNIQUE,
    replaced_schedule_json TEXT,
    note TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(recurring_template_id) REFERENCES recurring_bill_templates(id),
    FOREIGN KEY(debt_id) REFERENCES debts(id),
    FOREIGN KEY(generated_normalized_transaction_id) REFERENCES normalized_transactions(id)
);

CREATE INDEX IF NOT EXISTS idx_mortgage_prepayment_events_template_date
    ON mortgage_prepayment_events(recurring_template_id, prepayment_date);
