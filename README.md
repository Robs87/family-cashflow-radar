# 家庭现金流雷达

> 基于自部署 BeeCount Cloud MCP 的家庭现金流计划、预测和决策系统。

## 项目目标

家庭现金流雷达不是记账 App，也不替代 BeeCount Cloud。

本项目的正确分工是：

```text
BeeCount Cloud：记录已经发生的家庭流水，是账本事实源。
家庭现金流雷达：维护未来计划、房贷计划、周期义务，并做现金流预测和决策分析。
```

第一版回答 6 个问题：

1. BeeCount Cloud 里的已发生流水反映出当前现金流是否安全。
2. 未来 3 / 6 / 12 个月有哪些已知收入和支出。
3. 房贷、车贷、保险、学费等固定义务未来会形成多大压力。
4. 已发生流水和未来计划合并后，哪几个月存在断流风险。
5. 大额决策能不能做，例如提前还贷、买车、教育支出、装修或投资。
6. 如果不能做，差多少钱、要等到什么时候、应该推迟哪些支出。

## 当前技术路线

- BeeCount Cloud 负责日常记账、账户、分类、标签和预算等基础账本能力。
- 本项目通过 BeeCount Cloud MCP 读取已发生流水。
- SQLite 保存 BeeCount 流水分析缓存、未来计划事件、房贷计划和预测快照。
- 本项目不重复实现 BeeCount 已有的记账系统。
- 第一阶段先 CLI 跑通数据和预测模型，再做最简 Web 仪表盘。
- AI 只做分析、解释、预警和建议，不静默修改 BeeCount 事实流水。

## 核心能力

- BeeCount MCP 流水读取与本地只读缓存。
- 未来计划事件管理。
- 房贷 / 贷款计划自动展开。
- 周期性义务管理，例如保险、物业、学费、固定订阅。
- 已发生流水与计划事件匹配，避免重复计算。
- 未来现金流预测。
- 大额决策模拟。
- AI 现金流分析报告和风险解释。

## 目录

- `docs/prd/`：产品需求文档。
- `docs/design/`：数据库、MCP 接入、计划现金流和算法设计。
- `docs/plans/`：实施计划。
- `docs/logs/`：项目过程记录。
- `app/`：后续代码目录。
- `data/raw/`：本地临时原始数据放置区，不提交真实账本。
- `data/processed/`：处理后本地数据，不提交真实账本。
- `tests/`：测试。

## 当前文档

- [PRD v0.2：BeeCount Cloud MCP 事实源](docs/prd/prd-v0.2-beecount-mcp-cashflow-radar.md)
- [BeeCount MCP 与计划现金流设计 v0.1](docs/design/beecount-mcp-and-planned-cashflow-v0.1.md)
- [MVP 实施计划 v0.2：BeeCount MCP](docs/plans/mvp-implementation-plan-v0.2-beecount-mcp.md)
- [PRD v0.1](docs/prd/prd-v0.1.md)：历史版本，基于貔貅 CSV 的旧路线
- [数据库 Schema 与自动分类规则 v0.1](docs/design/database-schema-and-classification-rules-v0.1.md)：历史设计，部分金额字段已被 v0.2 取代
- [MVP 实施计划 v0.1](docs/plans/mvp-implementation-plan-v0.1.md)：历史计划
- [项目日志](docs/logs/project-log.md)
