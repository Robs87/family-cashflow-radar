---
title: 项目日志
created: 2026-05-15
updated: 2026-05-18
type: log
---

# 项目日志

## 2026-05-15

- 创建项目：家庭现金流雷达。
- 项目落点：`/Users/rainbow/Downloads/project/family-cashflow-radar`。
- 明确第一阶段路线：本地 Web App + SQLite + CSV 导入 + 自动分类 + 现金流仪表盘。
- 落地文档：PRD v0.1、数据库 Schema 与自动分类规则 v0.1、MVP 实施计划 v0.1。
- 决策：第一阶段先 CLI 跑通数据模型，再做 Web 仪表盘；暂不做手机原生 App、云同步、多用户、自动银行连接。

- 使用 gstack 工程评审视角审查 PRD、数据库设计和 MVP 实施计划。
- 新增评审文档：`docs/logs/2026-05-15-gstack-plan-eng-review.md`。
- 评审结论：方向正确，但进入 Claude Code 施工前必须先修订金额 cents 规范、normalize 幂等约束、人工覆盖机制、condition_json 规则表达、合成 CSV 测试夹具和 AGENTS.md 项目规则。
- 根据 gstack finance checklist 修订 `docs/plans/mvp-implementation-plan-v0.1.md`：新增 Task 0、金额语义硬规则、CLI 脚本契约、fixtures 优先、测试要求和 Claude Code 交接口径。
- 新增 `AGENTS.md`：固化隐私边界、金额 cents、幂等性、人工覆盖、规则分类、测试和 git 规则。

## 2026-05-17

- 根据用户进一步澄清，v0.2 目标从“CSV 现金流仪表盘”收敛为“家庭现金流行动建议系统”。
- 明确最重要判断：提前还贷、买车、大额消费、投资仓位、家庭安全垫和每月支出控制。
- 明确记录入口采用双轨：用户可直接发自然语言给 Codex，也可在 Web 端手动输入以节约 token；两者必须复用同一套新增交易逻辑。
- 明确 Web 首页最重要的一句话是家庭现金流安全状态和近期建议，而不是单纯展示图表。
- 明确记录颗粒度按家庭现金流语义设计，最小可用类型包括稳定收入、日常支出、固定刚性支出、债务还款、投资流入/流出、内部转账、工作垫付/报销、大额资产/一次性事件。
- 新增 `docs/prd/prd-v0.2.md`、`docs/plans/v0.2-action-advice-plan.md` 和 `docs/logs/2026-05-17-v0.2-update-summary.md`，作为后续 v0.2 施工依据。

## 2026-05-18

- 根据用户最新需求，项目分层调整为：BeeCount Cloud 作为流水记录层，Family Cashflow Radar 作为家庭现金流分析和决策层。
- 明确本项目不再把日常记账体验作为主战场；BeeCount / BeeCount Cloud 负责移动端/Web 记录、多端同步、账户、分类、标签、预算、附件和基础图表。
- 明确本项目负责 BeeCount 流水镜像、现金流语义、规则分类、月度现金流、家庭安全垫、近期建议和决策模拟。
- BeeCount Cloud 接入默认只读，优先评估 MCP read tools、read API 和离线 SQLite/备份导入；写回 BeeCount 必须另行设计权限、审计和冲突处理。
- 新增 `docs/plans/v0.3-beecount-cloud-source-plan.md`，并同步更新 `README.md`、`AGENTS.md`、`docs/prd/prd-v0.2.md`、`docs/plans/v0.2-action-advice-plan.md`。

## 待办

- 实现 BeeCount Cloud 只读数据源适配。
- 把 BeeCount 交易幂等同步到本地 raw/normalized 分析库。
- 补齐工作垫付、报销、投资、债务、资产事件的记录语义和分类规则。
- 完善固定账单、房贷计划、提前还贷事件的自动生成和编辑。
- 实现首页一句现金流安全判断和近期行动建议。
- 继续推进提前还贷、大额消费/买车、投资仓位安全上限模拟器。
