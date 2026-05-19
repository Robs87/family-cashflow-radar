# 家庭现金流雷达

> BeeCount Cloud 流水记录层 + 本地家庭现金流分析和决策系统。

## 项目目标

这个项目不再重新做日常记账 App。BeeCount / BeeCount Cloud 负责流水记录、移动端录入、多端同步、账户、分类、预算和基础账本管理；本项目负责把账本流水翻译成家庭财务决策系统。

第一版只回答 6 个问题：

1. 每月基础结余到底是多少。
2. 当前真实现金流是否安全。
3. 房贷、车贷、家庭刚性支出压力有多大。
4. 未来 3 到 6 个月是否有断流风险。
5. 大额决策能不能做。
6. 如果不能，差多少钱、要等到什么时候。

## 当前产品分层

- BeeCount Cloud：流水记录层，负责 iOS / Android / Web 记录、同步、备份、附件、账户、分类和预算。
- Family Cashflow Radar：分析决策层，负责现金流语义、规则分类、月度现金流、家庭安全垫、建议和决策模拟。
- 本地 SQLite：只保存分析所需的原始镜像、标准化结果、人工覆盖、预测、建议和模拟结果。
- CSV 导入：保留为历史账本迁移和兜底通道，不再作为长期日常记录入口。
- 分类和建议：规则优先，AI 后置，所有结论必须可解释。

BeeCount Cloud 当前可作为数据源的候选路径：

1. MCP 只读工具：读取账本、交易、账户、分类、预算和统计。
2. BeeCount Cloud read API：从服务端 projection 读取交易和工作区数据，需要普通 access token，不能用 `bcmcp_...` MCP PAT。
3. BeeCount Cloud SQLite / 备份离线导入：用于本地批量同步和灾难恢复分析。

默认策略是只读消费 BeeCount 数据。除非用户明确授权，本项目不向 BeeCount Cloud 写交易。

当前 BeeCount Cloud NAS 地址：

```text
https://bee.332626.xyz:9090
```

## 日常使用

短期仍可用现有本地仪表盘查看分析结果：

```bash
python3 app/main.py --db data/processed/cashflow.db --input data/raw
```

浏览器打开命令行显示的本地地址。未配置 BeeCount 来源时，“刷新数据”仍走本仓库已有 CSV/本地数据闭环；配置 BeeCount 来源后，会优先执行 BeeCount 同步，再标准化、分类并生成月度现金流。

首选使用 BeeCount Cloud read API 作为只读来源：

```bash
export BEECOUNT_ACCESS_TOKEN=...
export BEECOUNT_REFRESH_TOKEN=...
python3 app/main.py \
  --db data/processed/cashflow.db \
  --beecount-base-url https://bee.332626.xyz:9090 \
  --beecount-ledger-id <ledger-id>
```

也可以把 read API 来源保存成本地配置文件，之后启动 Web 不需要每次传 BeeCount 参数。配置文件放在 `data/processed/beecount_source.json`，该目录已被 `.gitignore` 忽略，不要在里面保存 token 原文。

```json
{
  "base_url": "https://bee.332626.xyz:9090",
  "ledger_id": "1",
  "access_token_env": "BEECOUNT_ACCESS_TOKEN",
  "refresh_token_env": "BEECOUNT_REFRESH_TOKEN",
  "limit": 500
}
```

`access_token` 缺失或过期时，同步器会用 `refresh_token` 调用 BeeCount `/api/v1/auth/refresh` 自动换新，并在当前进程环境里更新两枚 token。不要把 token 原文写进配置文件。

BeeCount JSON 导出或 MCP 查询结果只作为离线兜底来源：

```bash
python3 app/main.py \
  --db data/processed/cashflow.db \
  --beecount-input-json /path/to/beecount-transactions.json
```

对应的本地配置写法：

```json
{
  "input_json": "beecount_latest.json",
  "ledger_id": "1"
}
```

此时 `input_json` 相对路径按配置文件所在目录解析，即上例会读取 `data/processed/beecount_latest.json`。

也可以继续使用 CLI 链路：

```bash
mkdir -p data/processed
sqlite3 data/processed/cashflow.db < app/db/schema.sql
sqlite3 data/processed/cashflow.db < app/db/seed_rules.sql
python3 app/scripts/import_csv.py --db data/processed/cashflow.db --input data/raw
python3 app/scripts/normalize.py --db data/processed/cashflow.db
python3 app/scripts/classify.py --db data/processed/cashflow.db
python3 app/scripts/generate_monthly_cashflow.py --db data/processed/cashflow.db
python3 app/scripts/print_summary.py --db data/processed/cashflow.db
```

## 目录

- `docs/prd/`：产品需求文档。
- `docs/design/`：数据库、分类规则、算法设计。
- `docs/plans/`：实施计划。
- `docs/logs/`：项目过程记录。
- `app/`：后续代码目录。
- `data/raw/`：本地原始 CSV 放置区，不提交真实账本。
- `data/processed/`：处理后本地数据，不提交真实账本。
- `tests/`：测试。

## 当前文档

- [PRD v0.1](docs/prd/prd-v0.1.md)
- [PRD v0.2](docs/prd/prd-v0.2.md)
- [BeeCount Cloud 数据源适配计划 v0.3](docs/plans/v0.3-beecount-cloud-source-plan.md)
- [数据库 Schema 与自动分类规则 v0.1](docs/design/database-schema-and-classification-rules-v0.1.md)
- [MVP 实施计划 v0.1](docs/plans/mvp-implementation-plan-v0.1.md)
- [v0.2 实施计划](docs/plans/v0.2-action-advice-plan.md)
- [v0.2 更新摘要](docs/logs/2026-05-17-v0.2-update-summary.md)
- [项目日志](docs/logs/project-log.md)
