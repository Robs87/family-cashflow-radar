---
title: 项目日志
created: 2026-05-15
updated: 2026-05-15
type: log
---

# 项目日志

## 2026-05-15

- 创建项目：家庭现金流雷达。
- 项目落点：`/Users/rainbow/Downloads/project/family-cashflow-radar`。
- 明确第一阶段路线：本地 Web App + SQLite + CSV 导入 + 自动分类 + 现金流仪表盘。
- 落地文档：PRD v0.1、数据库 Schema 与自动分类规则 v0.1、MVP 实施计划 v0.1。
- 决策：第一阶段先 CLI 跑通数据模型，再做 Web 仪表盘；暂不做手机原生 App、云同步、多用户、自动银行连接。

## 待办

- 写入 `app/db/schema.sql`。
- 写入 `app/db/seed_rules.sql`。
- 交给 Claude Code 实现导入器、标准化转换器、分类器和月度现金流生成器。
