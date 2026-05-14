---
title: 家庭现金流 App MVP 实施计划 v0.1
created: 2026-05-15
updated: 2026-05-15
type: implementation-plan
reviewed_by: gstack-plan-eng-review
status: ready-for-claude-code-after-task-0
---

# 家庭现金流 App MVP 实施计划 v0.1

> 后续仓库代码改动默认交给 Claude Code 执行。本计划已吸收 gstack 工程评审结果，先锁定数据语义，再写代码。

## 1. 目标

先用 CLI 跑通数据模型，再做 Web 仪表盘。

最短路径：

```text
数据语义锁定 → schema.sql → seed_rules.sql → synthetic fixtures → import_csv.py → normalize.py → classify.py → generate_monthly_cashflow.py → 最简 Web 仪表盘
```

第一阶段成功标准不是页面好看，而是财务结论不被这几类沉默错误污染：

- 金额方向算反。
- 重复导入或重复 normalize 导致现金流翻倍。
- 信用卡还款被重复算作支出。
- 借入资金被算作收入。
- 投资和资产购入进入日常消费。
- 用户人工修正被分类器覆盖。

## 2. 技术栈

第一版使用 boring stack：

- Python 3.11+
- SQLite
- Python 标准库 `csv` / `sqlite3` / `argparse` / `decimal` / `hashlib` / `json`
- pytest
- Web 阶段再选 FastAPI + Jinja2 或 Streamlit

第一阶段优先不用 pandas。原因：CSV 导入、hash、日期解析、SQLite 写入都能用标准库完成，依赖越少，Claude Code 实施和本地复现越稳。

## 3. 项目目录

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
│   └── scripts/
│       ├── import_csv.py
│       ├── normalize.py
│       ├── classify.py
│       └── generate_monthly_cashflow.py
├── data/
│   ├── raw/
│   └── processed/
└── tests/
    └── fixtures/
        ├── sample_pixiu_minimal.csv
        ├── sample_pixiu_edge_cases.csv
        └── sample_pixiu_2021_2022.csv
```

## 4. 数据语义硬规则

这些规则先于代码实现。任何实现与这些规则冲突，都算错。

### 4.1 金额单位

所有标准化和聚合后的金额字段使用整数分：

```text
amount_cents INTEGER
```

禁止在标准化表、月度聚合表、预测表和模拟表中使用 SQLite `REAL` 保存金额。

### 4.2 金额符号

`amount_cents` 永远是非负绝对值。

方向只由 `cashflow_direction` 表达：

```text
inflow   真实现金流入
outflow  真实现金流出
neutral  内部流转，不计入真实收支
```

示例：

```text
工资收入 10000 元 → amount_cents=1000000, cashflow_direction=inflow
房贷支出 3000 元 → amount_cents=300000, cashflow_direction=outflow
信用卡还款 5000 元 → amount_cents=500000, cashflow_direction=neutral
```

### 4.3 原始金额保留

`raw_transactions` 必须保留原始金额信息：

```text
amount_original TEXT
income_amount_original TEXT
expense_amount_original TEXT
```

真实计算只使用 normalize 后的 `amount_cents`。

### 4.4 幂等性

所有脚本必须可重复运行，不得重复计数。

必须满足：

```sql
normalized_transactions.raw_transaction_id UNIQUE
monthly_cashflow(year, month) UNIQUE
```

### 4.5 人工覆盖优先

用户人工修正结果优先于所有自动规则。

分类器必须遵守：

```text
如果 manual_financial_type 不为空，不覆盖该交易分类。
```

### 4.6 规则可解释

第一阶段分类必须规则优先，AI 后置。

分类规则使用 `condition_json` 表达复合条件，MVP 只支持：

```text
year_in
any_text_contains
direction_in
account_contains
amount_cents_min
amount_cents_max
```

## 5. CLI 脚本统一契约

所有脚本必须支持：

```bash
--db data/processed/cashflow.db
--dry-run
--verbose
```

所有脚本必须输出稳定摘要，便于测试和 gstack 后续 QA：

```text
imported=1200 skipped_duplicate=14 failed=0
normalized=1200 skipped_existing=1200 failed=0
classified=980 unknown=220 manual_skipped=5
monthly_generated=38
```

错误必须明确输出到 stderr，并以非零 exit code 失败。

## 6. 实施任务

### Task 0：新增项目施工规则 AGENTS.md

目标：让后续 Claude Code 施工不破坏隐私和数据语义。

必须写入：

- 真实账本 CSV 和 SQLite 数据库不得提交。
- 金额用 cents，不用 REAL。
- `amount_cents` 永远非负，方向用 `cashflow_direction`。
- 导入、标准化、分类、月度聚合必须幂等。
- 自动分类规则必须可解释，AI 分类后置。
- 仓库代码改动必须配 pytest。
- Web 仪表盘必须等 CLI 数据闭环通过后再做。

验收：

```bash
read AGENTS.md
```

应能明确看到隐私、金额、幂等、测试和 Claude Code 施工规则。

### Task 1：创建 schema.sql

目标：把数据库设计落成可执行 SQL，并吸收 gstack 评审结论。

必须包含：

- 8 张表。
- 所有金额字段使用 `*_cents INTEGER`。
- `raw_transactions` 保留原始金额文本和完整 `raw_payload`。
- `raw_transactions.source_row_hash UNIQUE`。
- `normalized_transactions.raw_transaction_id UNIQUE`。
- `classification_rules.condition_json TEXT`。
- `normalized_transactions` 包含人工覆盖字段。
- `monthly_cashflow(year, month) UNIQUE`。
- 必要索引：年份月份、分类类型、方向类型月份、规则 enabled+priority。

验收：

```bash
sqlite3 data/processed/cashflow.db < app/db/schema.sql
sqlite3 data/processed/cashflow.db '.tables'
sqlite3 data/processed/cashflow.db '.schema normalized_transactions'
```

预期：

- 能看到 8 张表。
- `normalized_transactions` 有 `amount_cents`、`cashflow_direction`、manual override 字段和 `UNIQUE(raw_transaction_id)`。

### Task 2：创建 seed_rules.sql

目标：写入第一批可解释分类规则。

规则必须使用 `condition_json`。第一批至少覆盖：

- 2021/2022 historical_debt_asset_event
- 内部转账
- 信用卡还款
- 房贷
- 车贷
- 借入资金
- 投资流出
- 投资流入
- 特斯拉/车辆资产购入
- 资产出售
- 稳定收入
- 一次性收入
- 退款
- 固定刚性支出
- 日常生活支出

验收：

```bash
sqlite3 data/processed/cashflow.db < app/db/seed_rules.sql
sqlite3 data/processed/cashflow.db 'select count(*) from classification_rules;'
sqlite3 data/processed/cashflow.db 'select rule_name, condition_json from classification_rules order by priority limit 5;'
```

预期：

- 规则数大于等于 15。
- 前 5 条规则有可读的 `condition_json`。

### Task 3：创建 synthetic CSV fixtures

目标：先用假账本验证模型，不直接拿真实账本调试。

创建：

```text
tests/fixtures/sample_pixiu_minimal.csv
tests/fixtures/sample_pixiu_edge_cases.csv
tests/fixtures/sample_pixiu_2021_2022.csv
```

`sample_pixiu_edge_cases.csv` 至少包含：

- 工资收入
- 年终奖
- 信用卡还款
- 内部转账
- 借入资金
- 房贷
- 车贷
- 基金买入
- 基金赎回
- 特斯拉购车
- 二手出售
- 退款
- 两笔同日同商户同金额但真实存在的重复交易

验收：

```bash
python -m pytest tests/test_fixtures.py -v
```

预期：fixtures 文件存在、字段齐全、行数符合预期。

### Task 4：实现 CSV 导入器 import_csv.py

功能：

- 读取单个 CSV 文件或目录。
- 自动识别字段别名。
- 支持 UTF-8 / UTF-8-SIG / GBK。
- 保留完整 raw_payload。
- 生成 `source_row_hash`，重复导入同一文件不增加行数。
- 如果源数据存在交易 ID，优先纳入 hash。

验收：

```bash
python app/scripts/import_csv.py --db data/processed/cashflow.db --input tests/fixtures/sample_pixiu_edge_cases.csv
python app/scripts/import_csv.py --db data/processed/cashflow.db --input tests/fixtures/sample_pixiu_edge_cases.csv
sqlite3 data/processed/cashflow.db 'select count(*) from raw_transactions;'
python -m pytest tests/test_import_csv.py -v
```

预期：第二次导入不会增加行数；两笔真实重复交易不会被误删。

### Task 5：实现标准化转换 normalize.py

功能：

- 从 raw_transactions 生成 normalized_transactions。
- 解析日期并抽取 year / month。
- 把金额转为 `amount_cents` 非负整数。
- 初判 `cashflow_direction`。
- 标记大额交易。
- 使用 upsert，重复运行不产生重复 normalized。

验收：

```bash
python app/scripts/normalize.py --db data/processed/cashflow.db
python app/scripts/normalize.py --db data/processed/cashflow.db
sqlite3 data/processed/cashflow.db 'select count(*) from normalized_transactions;'
python -m pytest tests/test_normalize.py -v
```

预期：

- normalized 数量等于 raw 数量。
- 支出和收入的 `amount_cents` 都是正数。
- 转账可进入 neutral。
- 重复运行不增加行数。

### Task 6：实现规则分类器 classify.py

功能：

- 按 enabled + priority 读取 classification_rules。
- 支持 `condition_json` 的 MVP 操作符。
- manual override 优先，不覆盖人工分类。
- 输出 confidence 和 review_status。
- unknown、大额、低置信度交易进入 pending。

验收：

```bash
python app/scripts/classify.py --db data/processed/cashflow.db
sqlite3 data/processed/cashflow.db "select financial_type, count(*) from normalized_transactions group by financial_type order by count(*) desc;"
python -m pytest tests/test_classify.py -v
```

预期：

- 信用卡还款为 `credit_card_payment` + `neutral`。
- 内部转账为 `internal_transfer` + `neutral`。
- 借入资金为 `debt_inflow`，不算收入。
- 投资流出不算生活消费。
- 特斯拉购车为 `asset_purchase`。
- 2021/2022 标记为 `historical_debt_asset_event`。
- manual override 不被覆盖。

### Task 7：实现月度现金流生成器

功能：

- 聚合 normalized_transactions。
- 生成 monthly_cashflow。
- 使用 cents 聚合。
- 过滤 `historical_debt_asset_event`。
- neutral 不进入真实收支。
- 计算基础经营现金流和总现金流。

核心公式：

```text
net_operating_cashflow_cents
= stable_income_cents
- fixed_expense_cents
- living_expense_cents
- debt_payment_cents
```

```text
net_total_cashflow_cents
= total_real_income_cents
+ investment_inflow_cents
+ asset_sale_cents
+ debt_inflow_cents
+ refund_cents
- fixed_expense_cents
- living_expense_cents
- debt_payment_cents
- investment_outflow_cents
- asset_purchase_cents
```

验收：

```bash
python app/scripts/generate_monthly_cashflow.py --db data/processed/cashflow.db
sqlite3 data/processed/cashflow.db 'select year, month, stable_income_cents, debt_payment_cents, net_operating_cashflow_cents from monthly_cashflow order by year, month;'
python -m pytest tests/test_monthly_cashflow.py -v
```

预期：

- one_time_income 不进入基础经营现金流。
- neutral 不进入收入和支出。
- refund 不算真实收入。
- 2021/2022 不污染日常消费模型。

### Task 8：实现 CLI 摘要命令

目标：在 Web 前先能从命令行看核心指标。

建议新增：

```text
app/scripts/print_summary.py
```

展示：

- 月度稳定收入
- 固定支出
- 债务还款
- 基础经营现金流
- unknown 待审核数量

验收：

```bash
python app/scripts/print_summary.py --db data/processed/cashflow.db
```

预期：输出可读摘要，且金额格式为元。

### Task 9：最简 Web 仪表盘

必须等 Task 1 到 Task 8 及 pytest 全部通过后再做。

第一版只展示：

- 本月稳定收入
- 本月刚性支出
- 本月债务还款
- 本月基础结余
- 近 12 月基础结余趋势
- unknown 待审核数量

验收：

```bash
python app/main.py
```

浏览器打开本地地址能看到仪表盘。之后再用 gstack browser 做 QA。

## 7. 测试要求

每个代码任务必须先写或同步写 pytest。

最低测试文件：

```text
tests/test_schema.py
tests/test_fixtures.py
tests/test_import_csv.py
tests/test_normalize.py
tests/test_classify.py
tests/test_monthly_cashflow.py
```

核心测试必须覆盖：

- 重复导入不会产生重复 raw。
- 真实重复交易不会被 hash 误杀。
- 重复 normalize 不会产生重复 normalized。
- 支出/收入都转为正数 cents。
- 信用卡还款 neutral。
- 内部转账 neutral。
- 借入资金不算收入。
- 投资不算日常生活支出。
- 资产购入不算生活消费。
- 人工覆盖不被 classify 覆盖。
- 2021/2022 不进入日常现金流模型。
- 月度现金流公式正确。

## 8. NOT in scope

第一阶段不做：

- 手机原生 App。
- 云同步。
- 多用户。
- 登录权限系统。
- 银行/微信/支付宝自动连接。
- AI 自动分类主导。
- 复杂图表。
- 完整决策模拟器 UI。

## 9. 并行策略

不建议一开始并行拆给多个工作树。

原因：金额规范、schema 和 fixtures 是共同地基。地基没锁前，并行实现 import / normalize / classify 会产生不一致。

推荐顺序：

```text
Lane A：AGENTS.md + schema.sql + seed_rules.sql
  ↓
Lane B：fixtures + import_csv.py tests
  ↓
Lane C：normalize.py + classify.py tests
  ↓
Lane D：monthly_cashflow.py + print_summary.py tests
  ↓
Lane E：Web dashboard + gstack browser QA
```

## 10. Claude Code 交接口径

下一步交给 Claude Code 时，指令应是：

```text
请按 docs/plans/mvp-implementation-plan-v0.1.md 执行 Task 0 到 Task 3。
不要接触真实 data/raw 数据。
先完成 AGENTS.md、app/db/schema.sql、app/db/seed_rules.sql、tests/fixtures 和对应 pytest。
所有金额用 amount_cents INTEGER，禁止 REAL 存钱。
完成后运行 pytest，并提交 git commit。
```

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| Eng Review | `/plan-eng-review` | Architecture & tests | 1 | issues_open → plan revised | 6 architecture issues, 2 code quality issues, 25 test gaps absorbed into this plan |

- **UNRESOLVED:** 0 after this revision.
- **VERDICT:** ENG REVIEW ABSORBED — ready to hand Task 0-3 to Claude Code.
