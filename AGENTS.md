# 家庭现金流雷达 - Agent 施工规则

本仓库是用户的本地家庭现金流分析项目。最新目标是把 BeeCount Cloud 作为流水记录和同步层，本项目负责把账本流水翻译成家庭现金流分析和决策系统。

当前事实源文档：

- 产品需求：`docs/prd/prd-v0.2.md`
- 当前实施计划：`docs/plans/v0.2-action-advice-plan.md`
- BeeCount 数据源计划：`docs/plans/v0.3-beecount-cloud-source-plan.md`

`docs/plans/mvp-implementation-plan-v0.2-beecount-mcp.md` 已废弃，仅作历史参考；不要按其中 MCP cache、全新 planned events schema、CLI-first 的严格顺序施工。

## 0. 核心边界

代码施工默认交给 Claude Code。其他代理可以评审、QA、整理文档，但仓库代码改动必须遵守本文件。

当前产品分层：

```text
BeeCount / BeeCount Cloud 负责记录、同步、附件、账户、分类、预算和基础图表
Family Cashflow Radar 负责读取 BeeCount 流水、标准化现金流语义、规则分类、月度现金流、预测、建议和决策模拟
```

不要做：

- 手机原生 App
- 自建一套与 BeeCount 重叠的日常记账 App
- 自建账本云同步
- 多用户
- 登录权限系统
- 银行/微信/支付宝自动连接
- AI 自动分类主导
- 复杂图表

允许做：

- BeeCount Cloud 数据源适配。
- BeeCount Cloud MCP / API / SQLite 备份只读导入。
- CSV 导入作为历史迁移和兜底通道。
- 本地分析库保存标准化结果、人工覆盖、预测、建议和决策模拟结果。

## 1. 隐私与数据安全

真实账本是私人财务数据。

严禁提交：

- `data/raw/*` 中的真实 CSV
- `data/processed/*` 中的 SQLite 数据库
- BeeCount Cloud 数据库、备份、附件或导出文件
- 任何包含真实收入、支出、账户、商户、备注的导出文件
- 任何密钥、token、cookie、账户凭证、BeeCount Cloud PAT

`.gitignore` 已忽略 data 目录中的真实数据。不要绕过。

测试必须使用 `tests/fixtures/` 下的合成假账本，不要用真实账本写测试断言。

## 2. 金额语义硬规则

所有标准化和聚合后的金额字段必须用整数分：

```text
amount_cents INTEGER
```

禁止在标准化表、月度表、预测表和模拟表里用 SQLite `REAL` 保存钱。

`amount_cents` 永远是非负绝对值。方向只能由 `cashflow_direction` 表达：

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

原始金额必须保留在 `raw_transactions`，例如：

```text
amount_original
income_amount_original
expense_amount_original
raw_payload
```

真实计算只使用 normalized 后的 cents 字段。

## 3. 幂等性要求

所有脚本必须可重复运行，不得重复计数。

必须满足：

- 重复导入同一 CSV，不增加 raw_transactions 行数。
- 重复同步同一批 BeeCount 交易，不增加 raw_transactions 行数。
- 重复运行 normalize，不增加 normalized_transactions 行数。
- 重复运行 classify，不覆盖人工修正。
- 重复运行 monthly aggregation，不重复累计月度结果。

关键约束：

```sql
normalized_transactions.raw_transaction_id UNIQUE
monthly_cashflow(year, month) UNIQUE
```

## 4. 分类规则

第一阶段必须规则优先，AI 后置。

分类规则必须可解释。不要写一个不可审计的 AI 分类黑箱。

`classification_rules` 应支持 `condition_json`，MVP 操作符：

```text
year_in
any_text_contains
direction_in
account_contains
amount_cents_min
amount_cents_max
```

分类优先级：

1. 人工覆盖
2. 2021/2022 historical_debt_asset_event
3. 内部转账
4. 信用卡还款
5. 债务还款
6. 借入资金
7. 投资流入/流出
8. 资产购入/出售
9. 稳定收入
10. 一次性收入
11. 固定刚性支出
12. 日常生活支出
13. unknown

## 5. 人工覆盖规则

用户人工修正结果优先于所有自动规则。

如果 `manual_financial_type` 不为空，分类器不得覆盖该交易。

建议字段：

```text
manual_financial_type
manual_category_l1
manual_category_l2
manual_cashflow_direction
manual_note
manual_updated_at
```

## 6. CLI 脚本契约

所有脚本必须支持：

```bash
--db data/processed/cashflow.db
--dry-run
--verbose
```

所有脚本必须输出稳定摘要：

```text
imported=1200 skipped_duplicate=14 failed=0
normalized=1200 skipped_existing=1200 failed=0
classified=980 unknown=220 manual_skipped=5
monthly_generated=38
```

错误必须写 stderr，并以非零 exit code 退出。

## 7. 测试要求

仓库代码改动必须配 pytest。

最低测试文件：

```text
tests/test_schema.py
tests/test_fixtures.py
tests/test_import_csv.py
tests/test_normalize.py
tests/test_classify.py
tests/test_monthly_cashflow.py
```

必须先创建合成 fixtures：

```text
tests/fixtures/sample_pixiu_minimal.csv
tests/fixtures/sample_pixiu_edge_cases.csv
tests/fixtures/sample_pixiu_2021_2022.csv
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

## 8. 开发顺序

旧的 v0.1 CSV 闭环已经作为历史基础存在。新需求优先按 BeeCount Cloud 作为流水源的路线执行。

当前推荐顺序：

```text
Task 1：补齐 BeeCount 交易更新 / 删除 / 最新版本选择语义
Task 2：实现当前现金余额校准
Task 3：实现未来计划事件和实际 BeeCount 流水匹配去重
Task 4：继续完善大额消费 / 买车分期成本模拟
Task 5：实现投资仓位安全上限
Task 6：持续优化 BeeCount 分类映射和支出控制建议
```

任何 BeeCount 写入能力必须谨慎处理。默认只读；如未来接入写入，必须由用户显式授权，并且不得绕过 BeeCount 的审计和确认机制。

## 9. Git 规则

- 不要 `git add -A`。
- 只 stage 本次任务相关文件。
- 每个可验证任务完成后提交一次。
- 提交前运行相关测试。
- 不要提交真实 data 文件。

推荐提交粒度：

```text
chore: add project agent rules
feat: add sqlite schema and seed rules
feat: add synthetic pixiu fixtures
feat: implement csv importer
feat: implement transaction normalization
feat: implement rule classifier
feat: generate monthly cashflow
```

## 10. gstack 使用边界

gstack 用于：

- 评审 PRD / 计划 / 架构。
- Web 阶段浏览器 QA。
- 发现沉默失败和测试缺口。

gstack 不替代 Claude Code 做仓库代码施工，除非用户明确指定。
