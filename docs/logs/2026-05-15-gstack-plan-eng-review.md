---
title: gstack 工程方案评审 - 家庭现金流雷达
created: 2026-05-15
updated: 2026-05-15
type: review
skill: gstack-plan-eng-review
commit: 94c1ad6
status: issues_open
---

# gstack 工程方案评审 - 家庭现金流雷达

评审对象：

- `README.md`
- `docs/prd/prd-v0.1.md`
- `docs/design/database-schema-and-classification-rules-v0.1.md`
- `docs/plans/mvp-implementation-plan-v0.1.md`

评审方式：按 gstack `plan-eng-review` 的工程视角检查范围、架构、代码质量、测试、性能、失败模式和并行实施策略。

结论先说：**方向正确，MVP 顺序正确，但数据层还有 5 个必须在写代码前修掉的坑。** 否则后面仪表盘会看起来能跑，但财务结论可能错。

## Step 0：范围挑战

### 现有代码是否已经解决部分问题

当前仓库只有文档，没有实现代码。不存在可复用代码。

但已经存在可复用的设计资产：

- PRD 已经明确“不做普通记账 App”。
- Schema 文档已经列出核心实体。
- MVP 实施计划已经把顺序定为 CLI 优先，Web 后置。

这三点是正确基线。

### 最小变更集合

MVP 最小闭环应该是：

```text
CSV 文件
  ↓
raw_transactions 原始落库
  ↓
normalized_transactions 标准化
  ↓
classification_rules 规则分类
  ↓
monthly_cashflow 月度聚合
  ↓
CLI 输出核心指标
```

Web 仪表盘可以后置到 CLI 数据闭环验证之后。

### 复杂度检查

计划涉及文件数：

- `schema.sql`
- `seed_rules.sql`
- `import_csv.py`
- `normalize.py`
- `classify.py`
- `generate_monthly_cashflow.py`
- `app/main.py`
- 测试文件若干

接近 8 个文件，但复杂度合理。问题不在文件数，而在**数据语义必须先锁死**。

### Completeness 检查

当前计划不是过度设计，反而在几个关键边界上不够完整：

- 金额正负和收入/支出方向没有硬性规范。
- 自动分类规则表无法表达年份、复合条件和人工覆盖。
- 重复 normalize / classify 的幂等性没有写进 schema。
- 财务金额用 `REAL` 有精度风险。
- 缺少合成测试账本，无法验证“信用卡还款不重复计支出”等核心财务规则。

这些不是锦上添花，是财务模型的地基。

## 架构评审

### 问题 1：金额语义未锁定

`raw_transactions.amount` 和 `normalized_transactions.amount` 都是 `REAL`。文档没有明确：

- 支出金额是正数还是负数？
- 收入金额是正数还是负数？
- 聚合公式里的 `SUM(amount)` 是否假设所有金额为正数？
- CSV 如果有“收入金额/支出金额”两个字段，如何合并？

当前月度公式默认所有分类金额都是正数，然后通过 financial_type 决定加减。如果导入器把支出写成负数，`net_operating_cashflow = stable_income - fixed_expense` 会变成反向加。

**建议：必须在实现前增加金额规范。**

推荐规范：

```text
raw_transactions.amount_original：原始金额文本或原始数值
normalized_transactions.amount_cents：绝对金额，单位分，永远非负
cashflow_direction：inflow / outflow / neutral 决定方向
```

评分：P0，confidence 9/10。

### 问题 2：用 REAL 存钱不合适

SQLite `REAL` 是浮点数。财务计算应避免浮点误差。

**建议：所有金额字段改为 INTEGER cents。**

示例：

```sql
amount_cents INTEGER NOT NULL
stable_income_cents INTEGER DEFAULT 0
net_operating_cashflow_cents INTEGER DEFAULT 0
```

页面展示时再除以 100。

评分：P1，confidence 9/10。

### 问题 3：normalized_transactions 缺少幂等约束

当前 schema 没有：

```sql
UNIQUE(raw_transaction_id)
```

如果重复运行 `normalize.py`，可能同一条 raw 生成多条 normalized，后续现金流翻倍。

**建议：加唯一约束，并让 normalize 使用 upsert。**

```sql
raw_transaction_id INTEGER NOT NULL UNIQUE
```

评分：P0，confidence 9/10。

### 问题 4：分类规则表表达不了真实规则

当前 `classification_rules` 只有：

- `match_field`
- `match_pattern`
- `amount_min/max`
- `direction_raw`
- `account_pattern`

但计划里的关键规则需要复合条件：

- `year IN (2021, 2022)`
- 备注或商户或分类任一字段命中关键词
- 方向为收入且关键词为赎回
- 大额且包含 Tesla
- 信用卡关键词 + account_pattern

当前表结构难以表达这些组合，最后会变成代码里写死大量 if/else，规则表失去意义。

**建议：保留简单字段，同时增加 `condition_json`。**

```sql
condition_json TEXT
```

示例：

```json
{
  "any_text_contains": ["信用卡还款", "账单还款"],
  "direction": ["支出", "转账"],
  "account_contains": ["信用卡"]
}
```

MVP 实现可以先只支持 4 种条件：

- `year_in`
- `any_text_contains`
- `direction_in`
- `amount_cents_min/max`

评分：P1，confidence 8/10。

### 问题 5：人工审核结果可能被 classify 覆盖

文档有 `review_status`，但没有人工覆盖表，也没有 `manual_override` 字段。

如果用户把一笔交易改成“资产购入”，下次运行 `classify.py` 可能又按规则覆盖掉。

**建议：增加人工覆盖机制。**

最小实现：在 `normalized_transactions` 增加：

```sql
manual_financial_type TEXT
manual_category_l1 TEXT
manual_category_l2 TEXT
manual_cashflow_direction TEXT
manual_note TEXT
manual_updated_at TEXT
```

分类器规则：

```text
如果 manual_financial_type 不为空，则分类器不得覆盖。
```

评分：P1，confidence 8/10。

### 问题 6：`raw_hash` 去重规则可能误杀真实重复交易

当前 hash 拼接：

```text
transaction_time | amount | direction_raw | account | category | merchant | note
```

如果同一天同一商户发生两笔同金额交易，且备注相同，会被当作重复。

**建议：MVP 先引入双层去重。**

- `raw_fingerprint`：用于识别高度相似交易。
- `source_row_hash`：包含 source_file + source_row_no + raw_payload，用于重复导入同一文件。

更保守的第一版：保留 `raw_hash UNIQUE`，但 hash 中加入完整 `raw_payload`。如果貔貅 CSV 有交易 ID，优先使用交易 ID。

评分：P1，confidence 7/10。

## 架构数据流图

```text
[data/raw/*.csv]
      │
      ▼
import_csv.py
  - 字段别名识别
  - 金额原始值保留
  - source_row_hash 去重
      │
      ▼
raw_transactions
      │
      ▼
normalize.py
  - 日期解析
  - amount_cents 绝对金额
  - cashflow_direction 初判
  - UNIQUE(raw_transaction_id)
      │
      ▼
normalized_transactions
      │
      ▼
classify.py
  - manual override 优先
  - condition_json 规则匹配
  - confidence / review_status
      │
      ▼
generate_monthly_cashflow.py
  - 过滤 historical_debt_asset_event
  - neutral 不进真实收支
  - cents 聚合
      │
      ▼
monthly_cashflow
      │
      ▼
CLI summary / Web dashboard
```

## 代码质量评审

### 问题 7：脚本边界需要再明确

计划中脚本顺序正确，但每个脚本的输入输出契约还不够硬。

建议每个脚本统一支持：

```bash
--db data/processed/cashflow.db
--dry-run
--verbose
```

并输出固定摘要，例如：

```text
imported=1200 skipped_duplicate=14 failed=0
normalized=1200 skipped_existing=1200 failed=0
classified=980 unknown=220 manual_skipped=5
monthly_generated=38
```

这样后续 Claude Code 和 gstack QA 才能快速判断是否跑偏。

评分：P2，confidence 8/10。

### 问题 8：缺少项目级开发约束文件

仓库没有 `AGENTS.md`。这个项目后续会由 Claude Code 施工，应该告诉代理：

- 不提交真实 CSV 和数据库。
- 金额用分保存。
- 仓库代码改动要配测试。
- 真实账本只在本地 `data/raw/`，不进入 git。
- 分类规则要可解释，不要先上 AI 黑箱。

建议新增 `AGENTS.md`，这不是 gstack 路由问题，是项目安全边界。

评分：P1，confidence 8/10。

## 测试评审

当前计划有 pytest，但测试样例不够具体。

### 覆盖图

```text
CODE PATHS                                           TEST STATUS
[+] import_csv.py
  ├── [GAP] 字段别名：交易时间/日期/记账时间
  ├── [GAP] 收入金额和支出金额分列 CSV
  ├── [GAP] 重复导入同一文件不增加行数
  ├── [GAP] 同日同商户同金额的真实重复交易不误删
  └── [GAP] CSV 编码 UTF-8 / GBK 兼容

[+] normalize.py
  ├── [GAP] 支出转为 amount_cents 正数 + outflow
  ├── [GAP] 收入转为 amount_cents 正数 + inflow
  ├── [GAP] 转账转为 neutral
  ├── [GAP] year/month 抽取正确
  └── [GAP] 重复运行不产生重复 normalized

[+] classify.py
  ├── [GAP] 2021/2022 标记为 historical_debt_asset_event
  ├── [GAP] 信用卡还款为 neutral，不进入支出
  ├── [GAP] 内部转账为 neutral，不进入收入/支出
  ├── [GAP] 借入资金为 debt_inflow，不算收入
  ├── [GAP] 投资流出不算生活消费
  ├── [GAP] 特斯拉大额支出为 asset_purchase
  └── [GAP] manual override 不被覆盖

[+] generate_monthly_cashflow.py
  ├── [GAP] 基础经营现金流只用 stable_income
  ├── [GAP] one_time_income 不进入 net_operating_cashflow
  ├── [GAP] neutral 不进入 total_real_income / expense
  ├── [GAP] refund 不算真实收入
  └── [GAP] 2021/2022 不污染日常消费模型

[+] app/main.py
  ├── [LATER] 首页指标加载
  ├── [LATER] unknown 待审核数量显示
  └── [LATER] 响应式布局

COVERAGE: 0/25 planned paths tested
QUALITY: 当前为计划阶段，必须先补测试夹具和测试清单
```

### 必须增加的测试夹具

建议创建：

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
- 两笔同日同商户同金额真实重复交易

## 性能评审

MVP 数据量是多年个人账本，SQLite 足够。

需要补的索引：

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_norm_raw_transaction_id
ON normalized_transactions(raw_transaction_id);

CREATE INDEX IF NOT EXISTS idx_norm_direction_type_month
ON normalized_transactions(cashflow_direction, financial_type, year, month);

CREATE INDEX IF NOT EXISTS idx_rules_priority_enabled
ON classification_rules(enabled, priority);
```

规则分类性能不应逐规则逐交易 O(R*T) 做到不可控。MVP 可以接受，但要先限制数据量和输出耗时。后续如果慢，再优化为先拼接 searchable_text 字段。

## NOT in scope

以下内容确认不进入第一阶段：

- 手机原生 App：模型未验证前没有价值。
- 云同步：涉及隐私和账户体系，后置。
- 多用户：当前只服务用户本人。
- 银行/微信/支付宝自动连接：高风险，后置。
- AI 自动分类主导：第一阶段规则优先，AI 只做后续辅助。
- 复杂图表：先 CLI 验证现金流模型。
- 决策模拟器完整 UI：先把 cashflow_forecast 输入基础跑准。

## What already exists

当前已有：

- PRD：产品边界清楚。
- Schema 草案：实体方向正确。
- MVP 计划：实施顺序正确。
- git 仓库：已初始化并提交 baseline。

当前缺少：

- `schema.sql` 可执行文件。
- `seed_rules.sql` 可执行规则。
- 合成 CSV 测试夹具。
- 金额单位和方向规范。
- 人工覆盖机制。
- 项目级 `AGENTS.md`。

## 失败模式

| codepath | 真实失败方式 | 当前是否覆盖 | 用户影响 |
|---|---|---|---|
| import_csv | GBK CSV 读取失败 | 否 | 无法导入账本 |
| import_csv | 真实重复交易被 hash 误删 | 否 | 支出或收入少算 |
| normalize | 支出负数进入聚合 | 否 | 基础结余被算反 |
| normalize | 重复运行生成重复 normalized | 否 | 月度现金流翻倍 |
| classify | 信用卡还款被算作支出 | 否 | 家庭支出虚高 |
| classify | 借入资金被算作收入 | 否 | 收入和安全感虚高 |
| classify | 人工修正被规则覆盖 | 否 | 用户越改越乱 |
| monthly_cashflow | 2021/2022 混入日常消费模型 | 否 | 历史异常污染趋势 |
| dashboard | unknown 数量未展示 | 后置 | 用户不知道模型哪里不可靠 |

关键沉默失败：**金额方向算反、重复 normalize、信用卡还款重复计支出。** 这三个必须在 CLI 阶段用测试卡死。

## TODO 建议

建议写入 `TODOS.md` 的事项：

1. 增加金额规范：所有标准化金额使用 `amount_cents INTEGER` 且永远非负。
2. 增加 `UNIQUE(raw_transaction_id)`，保证 normalize 幂等。
3. 增加人工覆盖字段或覆盖表，保证人工审核结果不被分类器覆盖。
4. 增加 `condition_json`，让规则表支持年份、复合关键词和金额条件。
5. 增加合成 CSV fixtures，覆盖信用卡、内部转账、借款、投资、资产、退款和重复交易。
6. 增加 `AGENTS.md`，声明隐私、测试、金额单位和 Claude Code 施工规则。

## 并行实施策略

当前最好顺序执行，不建议并行切太细。

原因：金额规范、schema 和测试夹具是共同地基。并行写 import/normalize/classify 容易对金额方向和字段名理解不一致。

推荐顺序：

```text
Lane A：schema.sql + seed_rules.sql + AGENTS.md
  ↓
Lane B：fixtures + import_csv.py tests
  ↓
Lane C：normalize.py + classify.py tests
  ↓
Lane D：monthly_cashflow.py tests
  ↓
Lane E：Web dashboard + gstack browser QA
```

## 建议的计划修订

在进入 Claude Code 前，先改 MVP 实施计划：

1. Task 1 前新增“数据语义锁定”：金额用 cents，方向由 cashflow_direction 表达。
2. Task 1 同步增加幂等约束和人工覆盖字段。
3. Task 2 的 seed_rules 改成支持 `condition_json` 的规则种子。
4. Task 3 前新增 synthetic fixtures。
5. 每个脚本都要求 `--dry-run` 和固定摘要输出。
6. Web 仪表盘从 Task 7 后置，必须等 CLI 测试全部通过。

## Completion Summary

- Step 0: Scope Challenge — scope accepted as-is, but必须先修数据语义。
- Architecture Review: 6 issues found。
- Code Quality Review: 2 issues found。
- Test Review: coverage diagram produced, 25 gaps identified。
- Performance Review: 3 index / precision recommendations。
- NOT in scope: written。
- What already exists: written。
- TODOS.md updates: 6 items proposed。
- Failure modes: 3 critical silent failures flagged。
- Outside voice: skipped。
- Parallelization: sequential first, no useful parallel worktree split before schema/test fixtures稳定。
- Lake Score: 6/6 recommendations choose complete option。

## Verdict

**DONE_WITH_CONCERNS**

方案方向正确，可以继续。但不要直接让 Claude Code 从原计划 Task 1 开始写。应先修订 schema 和实施计划，尤其是：

- 金额 cents 规范
- normalized 幂等约束
- 人工覆盖机制
- condition_json 规则表达
- 合成 CSV 测试夹具
- AGENTS.md 项目施工规则
