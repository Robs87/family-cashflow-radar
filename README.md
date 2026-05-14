# 家庭现金流雷达

> 本地 Web App + SQLite + CSV 导入 + 自动分类 + 现金流仪表盘。

## 项目目标

这个项目不是重新做一个记账 App，而是把多年貔貅记账流水翻译成家庭财务决策系统。

第一版只回答 6 个问题：

1. 每月基础结余到底是多少。
2. 当前真实现金流是否安全。
3. 房贷、车贷、家庭刚性支出压力有多大。
4. 未来 3 到 6 个月是否有断流风险。
5. 大额决策能不能做。
6. 如果不能，差多少钱、要等到什么时候。

## 当前技术路线

- 本地运行，不上云。
- SQLite 保存清洗后的账本与模型结果。
- CSV 从貔貅记账导出。
- 分类采用规则优先，AI 后置。
- 先 CLI 跑通模型，再做 Web 仪表盘。

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
- [数据库 Schema 与自动分类规则 v0.1](docs/design/database-schema-and-classification-rules-v0.1.md)
- [MVP 实施计划 v0.1](docs/plans/mvp-implementation-plan-v0.1.md)
- [项目日志](docs/logs/project-log.md)
