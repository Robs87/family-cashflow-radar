---
title: 家庭现金流 App MVP 实施计划 v0.1
created: 2026-05-15
updated: 2026-05-15
type: implementation-plan
---

# 家庭现金流 App MVP 实施计划 v0.1

> 后续仓库代码改动默认交给 Claude Code 执行。这里先把任务拆到可实现粒度。

## 1. 目标

先用 CLI 跑通数据模型，再做 Web 仪表盘。

最短路径：

```text
schema.sql → import_csv.py → normalize.py → classify.py → generate_monthly_cashflow.py → 最简 Web 仪表盘
```

## 2. 技术栈

建议第一版：

- Python 3.11+
- SQLite
- pandas 或 Python csv 标准库
- FastAPI + Jinja2 或 Streamlit（二选一）
- pytest

第一阶段优先选择 Python 标准库 + SQLite，减少依赖。

## 3. 项目目录

```text
family-cashflow-radar/
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
```

## 4. 实施任务

### Task 1：创建 schema.sql

目标：把设计文档中的 8 张表落成可执行 SQL。

验收：

```bash
sqlite3 data/processed/cashflow.db < app/db/schema.sql
sqlite3 data/processed/cashflow.db '.tables'
```

预期能看到 8 张表。

### Task 2：创建 seed_rules.sql

目标：写入第一批分类规则。

验收：

```bash
sqlite3 data/processed/cashflow.db < app/db/seed_rules.sql
sqlite3 data/processed/cashflow.db 'select count(*) from classification_rules;'
```

预期规则数大于 10。

### Task 3：实现 CSV 导入器 import_csv.py

功能：

- 读取 `data/raw/` 下 CSV。
- 自动识别字段别名。
- 生成 raw_hash。
- 写入 raw_transactions。
- 重复导入不重复。

验收：

```bash
python app/scripts/import_csv.py --db data/processed/cashflow.db --input data/raw
python app/scripts/import_csv.py --db data/processed/cashflow.db --input data/raw
```

第二次导入后 raw_transactions 数量不变。

### Task 4：实现标准化转换 normalize.py

功能：

- 从 raw_transactions 读取。
- 生成 normalized_transactions。
- 抽取 year / month。
- 判断 cashflow_direction。
- 大额交易标记。
- 初始 financial_type = unknown。

验收：

```bash
python app/scripts/normalize.py --db data/processed/cashflow.db
sqlite3 data/processed/cashflow.db 'select count(*) from normalized_transactions;'
```

数量应等于 raw_transactions 数量。

### Task 5：实现规则分类器 classify.py

功能：

- 按 priority 顺序读取 classification_rules。
- 根据关键词、金额、方向、账户匹配。
- 更新 financial_type / category / flags / confidence。

验收：

```bash
python app/scripts/classify.py --db data/processed/cashflow.db
sqlite3 data/processed/cashflow.db "select financial_type, count(*) from normalized_transactions group by financial_type order by count(*) desc;"
```

应能看到 internal_transfer、credit_card_payment、debt_payment、investment、unknown 等分类。

### Task 6：实现月度现金流生成器

功能：

- 聚合 normalized_transactions。
- 生成 monthly_cashflow。
- 计算基础经营现金流。
- 计算总现金流。

验收：

```bash
python app/scripts/generate_monthly_cashflow.py --db data/processed/cashflow.db
sqlite3 data/processed/cashflow.db 'select year, month, stable_income, debt_payment, net_operating_cashflow from monthly_cashflow order by year, month;'
```

应能按月份看到核心结果。

### Task 7：实现最简 Web 仪表盘

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

浏览器打开本地地址能看到仪表盘。

## 5. 第一版质量门槛

必须满足：

1. 重复导入不会产生重复交易。
2. 信用卡还款不被重复算作支出。
3. 内部转账不污染收入/支出。
4. 投资和资产购入不进入日常消费。
5. 借款不被算作收入。
6. 2021/2022 不污染日常消费模型。
7. 2023 到 2026 能生成月度基础结余。
8. 大额 unknown 能集中审核。

## 6. 下一步

进入代码实施前，先落地两个文件：

- `app/db/schema.sql`
- `app/db/seed_rules.sql`

然后交给 Claude Code 从 Task 1 开始执行。
