-- Family Cashflow Radar - Seed Classification Rules v0.1
-- Rules are ordered by priority (lower number = higher priority).
-- condition_json uses MVP operators: year_in, any_text_contains, direction_in, account_contains, amount_cents_min, amount_cents_max

-- ============================================================
-- Priority 10: 2021/2022 历史债务资产事件
-- ============================================================
INSERT INTO classification_rules
    (rule_name, priority, condition_json, target_cashflow_direction, target_financial_type,
     is_debt_related, is_asset_related, confidence, description)
VALUES
    ('historical_debt_asset_event_2021_2022', 10,
     '{"year_in": [2021, 2022]}',
     'neutral', 'historical_debt_asset_event',
     1, 1, 0.7,
     '2021和2022年的交易标记为历史债务资产事件，不计入日常现金流');

-- ============================================================
-- Priority 20: 内部转账
-- ============================================================
INSERT INTO classification_rules
    (rule_name, priority, condition_json, target_cashflow_direction, target_financial_type,
     is_internal_transfer, confidence, description)
VALUES
    ('internal_transfer', 20,
     '{"any_text_contains": ["转账", "账户转账", "账户互转", "余额宝转入", "余额宝转出", "微信零钱", "支付宝余额", "银行卡转入", "银行卡转出", "提现", "充值"]}',
     'neutral', 'internal_transfer',
     1, 0.9,
     '内部账户间转账，不计入真实收支');

-- ============================================================
-- Priority 30: 信用卡还款
-- ============================================================
INSERT INTO classification_rules
    (rule_name, priority, condition_json, target_cashflow_direction, target_financial_type,
     is_debt_related, confidence, description)
VALUES
    ('credit_card_payment', 30,
     '{"any_text_contains": ["信用卡还款", "还信用卡", "信用卡自动还款", "账单还款", "购汇还款"]}',
     'neutral', 'credit_card_payment',
     1, 0.95,
     '信用卡还款为内部流转，不计入真实支出');

-- ============================================================
-- Priority 40: 房贷还款
-- ============================================================
INSERT INTO classification_rules
    (rule_name, priority, condition_json, target_cashflow_direction, target_financial_type,
     target_category_l1, is_debt_related, confidence, description)
VALUES
    ('debt_payment_mortgage', 40,
     '{"any_text_contains": ["房贷", "按揭", "月供", "还贷"]}',
     'outflow', 'debt_payment',
     '债务', 1, 0.95,
     '房贷/按揭还款');

-- ============================================================
-- Priority 41: 车贷还款
-- ============================================================
INSERT INTO classification_rules
    (rule_name, priority, condition_json, target_cashflow_direction, target_financial_type,
     target_category_l1, is_debt_related, confidence, description)
VALUES
    ('debt_payment_car_loan', 41,
     '{"any_text_contains": ["车贷", "汽车金融", "特斯拉金融"]}',
     'outflow', 'debt_payment',
     '债务', 1, 0.95,
     '车贷/汽车金融还款');

-- ============================================================
-- Priority 42: 贷款还款（通用）
-- ============================================================
INSERT INTO classification_rules
    (rule_name, priority, condition_json, target_cashflow_direction, target_financial_type,
     target_category_l1, is_debt_related, confidence, description)
VALUES
    ('debt_payment_general', 42,
     '{"any_text_contains": ["贷款还款"]}',
     'outflow', 'debt_payment',
     '债务', 1, 0.9,
     '通用贷款还款');

-- ============================================================
-- Priority 50: 借入资金
-- ============================================================
INSERT INTO classification_rules
    (rule_name, priority, condition_json, target_cashflow_direction, target_financial_type,
     target_category_l1, target_category_l2, confidence, description)
VALUES
    ('debt_inflow', 50,
     '{"any_text_contains": ["借款", "借入", "借钱", "周转", "亲友借款", "贷款到账"], "direction_in": ["收入", "in"]}',
     'inflow', 'debt_inflow',
     '债务', '借入资金', 0.85,
     '借入资金流入，不算真实收入');

-- ============================================================
-- Priority 55: 工作垫付
-- ============================================================
INSERT INTO classification_rules
    (rule_name, priority, condition_json, target_cashflow_direction, target_financial_type,
     target_category_l1, target_category_l2, confidence, description)
VALUES
    ('reimbursable_expense', 55,
     '{"any_text_contains": ["工作垫付", "公司垫付", "帮公司垫付", "代垫", "出差垫付", "垫付报销"], "direction_in": ["支出", "out"]}',
     'outflow', 'reimbursable_expense',
     '垫付报销', '工作垫付', 0.9,
     '工作垫付临时占用现金，不算家庭生活支出');

-- ============================================================
-- Priority 56: 报销回款
-- ============================================================
INSERT INTO classification_rules
    (rule_name, priority, condition_json, target_cashflow_direction, target_financial_type,
     target_category_l1, target_category_l2, confidence, description)
VALUES
    ('reimbursement_income', 56,
     '{"any_text_contains": ["报销", "其他报销", "报销到账", "公司报销", "报销款", "报销入账", "垫付报销"], "direction_in": ["收入", "in"]}',
     'inflow', 'reimbursement_income',
     '垫付报销', '报销回款', 0.9,
     '工作垫付回款，不算稳定收入');

-- ============================================================
-- Priority 60: 投资流出
-- ============================================================
INSERT INTO classification_rules
    (rule_name, priority, condition_json, target_cashflow_direction, target_financial_type,
     target_category_l1, is_investment_related, confidence, description)
VALUES
    ('investment_outflow', 60,
     '{"any_text_contains": ["基金", "股票", "证券", "理财", "定投", "买入", "申购", "USDT", "币安", "欧易", "OKX"]}',
     'outflow', 'investment_outflow',
     '投资', 1, 0.9,
     '投资买入/申购流出');

-- ============================================================
-- Priority 61: 投资流入
-- ============================================================
INSERT INTO classification_rules
    (rule_name, priority, condition_json, target_cashflow_direction, target_financial_type,
     target_category_l1, is_investment_related, confidence, description)
VALUES
    ('investment_inflow', 61,
     '{"any_text_contains": ["赎回", "分红", "理财到账", "基金赎回", "股票卖出", "证券转出"]}',
     'inflow', 'investment_inflow',
     '投资', 1, 0.9,
     '投资赎回/分红流入');

-- ============================================================
-- Priority 70: 资产购入
-- ============================================================
INSERT INTO classification_rules
    (rule_name, priority, condition_json, target_cashflow_direction, target_financial_type,
     target_category_l1, is_asset_related, confidence, description)
VALUES
    ('asset_purchase', 70,
     '{"any_text_contains": ["特斯拉", "Tesla", "车辆购置", "购车", "首付", "汽车", "设备"]}',
     'outflow', 'asset_purchase',
     '资产购入', 1, 0.9,
     '大额资产购入（车辆、设备等）');

-- ============================================================
-- Priority 71: 资产出售
-- ============================================================
INSERT INTO classification_rules
    (rule_name, priority, condition_json, target_cashflow_direction, target_financial_type,
     target_category_l1, is_asset_related, confidence, description)
VALUES
    ('asset_sale', 71,
     '{"any_text_contains": ["卖出", "二手", "闲鱼", "转卖", "出售", "回收", "卖车", "卖设备"]}',
     'inflow', 'asset_sale',
     '资产出售', 1, 0.85,
     '资产出售流入');

-- ============================================================
-- Priority 80: 稳定收入
-- ============================================================
INSERT INTO classification_rules
    (rule_name, priority, condition_json, target_cashflow_direction, target_financial_type,
     target_category_l1, confidence, description)
VALUES
    ('stable_income', 80,
     '{"any_text_contains": ["工资", "薪资", "绩效", "劳务费", "公司转账", "项目款"]}',
     'inflow', 'stable_income',
     '收入', 0.9,
     '稳定收入（工资、劳务费等）');

-- ============================================================
-- Priority 90: 一次性收入
-- ============================================================
INSERT INTO classification_rules
    (rule_name, priority, condition_json, target_cashflow_direction, target_financial_type,
     target_category_l1, confidence, description)
VALUES
    ('one_time_income', 90,
     '{"any_text_contains": ["奖金", "年终奖", "补贴", "红包", "礼金", "临时收入"]}',
     'inflow', 'one_time_income',
     '收入', 0.85,
     '一次性收入（奖金、补贴、红包等）');

-- ============================================================
-- Priority 95: 退款
-- ============================================================
INSERT INTO classification_rules
    (rule_name, priority, condition_json, target_cashflow_direction, target_financial_type,
     confidence, description)
VALUES
    ('refund', 95,
     '{"any_text_contains": ["退款", "退货", "退费", "返现", "冲正", "撤销"]}',
     'inflow', 'refund',
     0.9,
     '退款/退货/返现');

-- ============================================================
-- Priority 100: 固定刚性支出
-- ============================================================
INSERT INTO classification_rules
    (rule_name, priority, condition_json, target_cashflow_direction, target_financial_type,
     target_category_l1, confidence, description)
VALUES
    ('fixed_expense', 100,
     '{"any_text_contains": ["房租", "物业费", "水电费", "燃气费", "暖气费", "保险费", "社保", "公积金", "话费", "宽带", "学费", "培训费", "幼儿园"]}',
     'outflow', 'fixed_expense',
     '固定支出', 0.85,
     '固定刚性支出（房租、物业、保险等）');

-- ============================================================
-- Priority 110: 日常生活支出
-- ============================================================
INSERT INTO classification_rules
    (rule_name, priority, condition_json, target_cashflow_direction, target_financial_type,
     target_category_l1, confidence, description)
VALUES
    ('living_expense', 110,
     '{"any_text_contains": ["餐饮", "早餐", "午餐", "晚餐", "夜宵", "外卖", "超市", "购物", "交通", "打车", "地铁", "加油", "停车", "娱乐", "电影", "旅游", "医疗", "药品", "理发", "快递"]}',
     'outflow', 'living_expense',
     '生活支出', 0.75,
     '日常生活支出（餐饮、交通、购物等）');

-- ============================================================
-- Priority 999: unknown 兜底
-- ============================================================
INSERT INTO classification_rules
    (rule_name, priority, condition_json, target_cashflow_direction, target_financial_type,
     confidence, description)
VALUES
    ('unknown_fallback', 999,
     '{}',
     'outflow', 'unknown',
     0.1,
     '兜底规则：无法匹配任何已知模式的交易');
