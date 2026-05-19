# 家庭现金流雷达 - Agent 施工规则

本仓库是用户的本地家庭现金流计划、预测和决策项目。

## 0. 核心边界

代码施工默认交给 Claude Code。其他代理可以评审、QA、整理文档，但仓库代码改动必须遵守本文件。

当前 v0.2 主线：

```text
BeeCount Cloud MCP 读取已发生流水
→ 本地只读分析缓存
→ 未来计划事件
→ 房贷 / 周期义务展开
→ 已发生流水 + 未来计划合并预测
→ CLI 摘要
→ 最简 Web 仪表盘
→ AI 分析和预警
```

职责分工：

```text
BeeCount Cloud：日常记账事实源，负责已发生交易、账户、分类、标签、预算等基础账本能力。
家庭现金流雷达：不重复记账，只维护 BeeCount 缺失的未来计划、房贷计划、周期义务、提前关联记录、预测和决策模拟。
```

第一阶段只做：

- BeeCount MCP 读取和同步缓存。
- 计划事件管理。
- 房贷 / 贷款计划管理。
- 周期义务展开。
- 计划事件与 BeeCount 实际流水匹配。
- 未来现金流预测。
- CLI 摘要。
- CLI 闭环后再做最简 Web 仪表盘。

不要做：

- 重做 BeeCount Cloud 的日常记账录入。
- 重做 BeeCount Cloud 的账户、分类、标签、预算基础管理。
- 手机原生 App。
- 多用户。
- 登录权限系统。
- 银行/微信/支付宝自动连接。
- AI 自动分类主导。
- AI 静默修改 BeeCount 事实流水。
- 复杂图表。
- CLI 数据闭环前的完整 Web UI。

## 1. 隐私与数据安全

真实账本是私人财务数据。

严禁提交：

- `data/raw/*` 中的真实导出数据。
- `data/processed/*` 中的 SQLite 数据库。
- BeeCount MCP 返回的真实流水 payload 导出。
- 任何包含真实收入、支出、账户、商户、备注的文件。
- 任何密钥、token、cookie、账户凭证。

`.gitignore` 已忽略 data 目录中的真实数据。不要绕过。

测试必须使用 synthetic fixtures 或 mock MCP 返回，不要用真实账本写测试断言。

## 2. 金额语义硬规则

所有标准化、计划、预测和模拟中的金额字段必须用整数分：

```text
*_cents INTEGER
```

禁止在标准化表、计划表、月度表、预测表和模拟表里用 SQLite `REAL` 保存钱。

利率用基点表示：

```text
annual_interest_rate_bps INTEGER
3.45% = 345
```

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

BeeCount MCP 原始 payload 必须保留在缓存表中，便于追溯。

## 3. 数据事实源规则

BeeCount Cloud 是已发生流水事实源。

家庭现金流雷达可以建立本地缓存，但本地缓存只用于分析，不得成为新的记账事实源。

必须满足：

- 读取 BeeCount 交易时保留 BeeCount 原始交易 ID。
- 重复同步同一 BeeCount 交易不得重复插入。
- 已发生流水以 BeeCount Cloud 为准。
- 未来计划事件由家庭现金流雷达维护。
- 计划事件不得伪装成 BeeCount 已发生流水。
- 计划事件和实际 BeeCount 流水匹配后，预测时不得重复计算。

## 4. 幂等性要求

所有脚本必须可重复运行，不得重复计数。

必须满足：

- 重复同步 BeeCount MCP，不增加重复交易缓存行。
- 重复生成周期义务，不增加重复计划事件。
- 重复生成房贷计划，不增加重复还款计划。
- 重复运行匹配，不重复标记或重复影响预测。
- 重复运行 forecast，不重复累计预测结果。

关键约束示例：

```sql
beecount_transactions_cache.beecount_transaction_id UNIQUE
loan_payment_schedule(loan_plan_id, due_date) UNIQUE
```

## 5. AI 使用边界

第一阶段必须规则和确定性计算优先，AI 后置。

AI 可以：

- 总结现金流；
- 解释风险；
- 发现异常；
- 推测可能的固定支出；
- 生成计划事件草案；
- 给出决策建议。

AI 不可以：

- 未经确认直接修改 BeeCount Cloud 流水。
- 未经确认直接新增、删除或停用计划事件。
- 把推测当作事实。
- 输出无法追溯到底层流水、计划事件或预测参数的结论。

## 6. CLI 脚本契约

所有脚本必须支持：

```bash
--db data/processed/cashflow.db
--dry-run
--verbose
```

所有脚本必须输出稳定摘要，便于测试和 QA：

```text
synced=120 skipped_existing=10 failed=0
planned_events_generated=12 skipped_existing=12 failed=0
loan_schedule_generated=12 skipped_existing=12 failed=0
matched=5 candidates=2 unmatched=10
risk_level=watch pressure_month=2026-08
```

错误必须写 stderr，并以非零 exit code 退出。

## 7. 测试要求

仓库代码改动必须配 pytest。

最低测试文件：

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

核心测试必须覆盖：

- 重复 MCP 同步不会产生重复缓存交易。
- 金额全部转为非负整数分。
- 利率使用 bps，不使用浮点金额。
- 计划事件 matched 后不重复进入预测。
- 周期义务重复生成不重复。
- 房贷计划重复生成不重复。
- BeeCount 已发生流水和计划事件合并后能正确识别风险。
- AI 生成建议不得直接写成事实。

## 8. 开发顺序

按 `docs/plans/mvp-implementation-plan-v0.2-beecount-mcp.md` 执行。

当前推荐顺序：

```text
Task 0：更新项目边界文档
Task 1：schema.sql
Task 2：BeeCount MCP client mock
Task 3：sync_beecount.py
Task 4：planned_events.py
Task 5：recurring_obligations.py
Task 6：loan_plans.py
Task 7：match_actuals.py
Task 8：cashflow_forecast.py
Task 9：print_summary.py
Task 10：Web dashboard
```

Web 仪表盘必须等 CLI 数据闭环和 pytest 通过后再做。

## 9. Git 规则

- 不要 `git add -A`。
- 只 stage 本次任务相关文件。
- 每个可验证任务完成后提交一次。
- 提交前运行相关测试。
- 不要提交真实 data 文件。

推荐提交粒度：

```text
docs: update project boundary for BeeCount MCP
feat: add beecount mcp cache schema
feat: sync beecount transactions cache
feat: add planned cashflow events
feat: generate recurring obligations
feat: generate loan payment schedule
feat: match planned events with actual transactions
feat: generate cashflow forecast
```

## 10. gstack 使用边界

gstack 用于：

- 评审 PRD / 计划 / 架构。
- Web 阶段浏览器 QA。
- 发现沉默失败和测试缺口。

gstack 不替代 Claude Code 做仓库代码施工，除非用户明确指定。
