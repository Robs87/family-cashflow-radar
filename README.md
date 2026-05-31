[English](README_EN.md) | 中文

# 家庭现金流雷达

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![SQLite](https://img.shields.io/badge/SQLite-3-003B57?logo=sqlite&logoColor=white)](https://www.sqlite.org/)
[![Tests](https://img.shields.io/badge/Tests-Pytest-0A9EDC?logo=pytest&logoColor=white)](tests/)

> BeeCount Cloud 流水记录层 + 本地家庭现金流分析和决策系统。

## 目录

- [项目目标](#项目目标)
- [系统架构](#系统架构)
- [快速开始](#快速开始)
- [项目结构](#项目结构)
- [核心功能](#核心功能)
- [开发文档](#开发文档)
- [贡献指南](#贡献指南)
- [许可证](#许可证)

## 项目目标

这个项目不再重新做日常记账 App。BeeCount / BeeCount Cloud 负责流水记录、移动端录入、多端同步、账户、分类、预算和基础账本管理；本项目负责把账本流水翻译成家庭财务决策系统。

第一版只回答 6 个问题：

1. 每月基础结余到底是多少。
2. 当前真实现金流是否安全。
3. 房贷、车贷、家庭刚性支出压力有多大。
4. 未来 3 到 6 个月是否有断流风险。
5. 大额决策能不能做。
6. 如果不能，差多少钱、要等到什么时候。

## 系统架构

```text
┌─────────────────────────────────────────────────────────────┐
│                    BeeCount Cloud                           │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐    │
│  │ iOS App  │  │ Android  │  │ Web App  │  │   MCP    │    │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘    │
│       └──────────────┼──────────────┼──────────────┘         │
│                      ▼                                      │
│              ┌───────────────┐                              │
│              │  流水记录层    │                              │
│              │  (同步/备份)   │                              │
│              └───────┬───────┘                              │
└──────────────────────┼──────────────────────────────────────┘
                       │ 只读同步
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                Family Cashflow Radar                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │  数据导入     │  │  规则分类     │  │  月度现金流   │      │
│  │  (API/CSV)   │→│  (可解释)     │→│  (聚合分析)   │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│                            │                                │
│                            ▼                                │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │  安全垫分析   │  │  决策模拟     │  │  行动建议     │      │
│  │  (风险评估)   │  │  (提前还贷等) │  │  (可解释)     │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
└─────────────────────────────────────────────────────────────┘
```

### 产品分层

| 层级 | 职责 |
|------|------|
| **BeeCount Cloud** | 流水记录层：iOS / Android / Web 记录、同步、备份、附件、账户、分类和预算 |
| **Family Cashflow Radar** | 分析决策层：现金流语义、规则分类、月度现金流、家庭安全垫、建议和决策模拟 |
| **本地 SQLite** | 只保存分析所需的原始镜像、标准化结果、人工覆盖、预测、建议和模拟结果 |
| **CSV 导入** | 保留为历史账本迁移和兜底通道，不再作为长期日常记录入口 |

### 数据源接入

BeeCount Cloud 当前可作为数据源的候选路径：

1. **MCP 只读工具**：读取账本、交易、账户、分类、预算和统计。
2. **BeeCount Cloud read API**：从服务端 projection 读取交易和工作区数据，需要普通 access token。
3. **BeeCount Cloud SQLite / 备份离线导入**：用于本地批量同步和灾难恢复分析。

默认策略是只读消费 BeeCount 数据。除非用户明确授权，本项目不向 BeeCount Cloud 写交易。

## 快速开始

### 环境要求

- Python 3.11+
- SQLite 3
- （可选）BeeCount Cloud 实例

### 方式一：使用 BeeCount Cloud API（推荐）

```bash
# 1. 克隆仓库
git clone https://github.com/Robs87/family-cashflow-radar.git
cd family-cashflow-radar

# 2. 设置环境变量
export BEECOUNT_ACCESS_TOKEN=your_access_token
export BEECOUNT_REFRESH_TOKEN=your_refresh_token

# 3. 启动 Web 仪表盘
python3 app/main.py \
  --db data/processed/cashflow.db \
  --beecount-base-url https://your-beecount-server.com \
  --beecount-ledger-id <your-ledger-id>
```

浏览器打开命令行显示的本地地址即可查看分析结果。

### 方式二：使用本地 CSV

```bash
# 1. 将 CSV 文件放入 data/raw/ 目录

# 2. 初始化数据库
mkdir -p data/processed
sqlite3 data/processed/cashflow.db < app/db/schema.sql
sqlite3 data/processed/cashflow.db < app/db/seed_rules.sql

# 3. 运行数据处理流水线
python3 app/scripts/import_csv.py --db data/processed/cashflow.db --input data/raw
python3 app/scripts/normalize.py --db data/processed/cashflow.db
python3 app/scripts/classify.py --db data/processed/cashflow.db
python3 app/scripts/generate_monthly_cashflow.py --db data/processed/cashflow.db
python3 app/scripts/print_summary.py --db data/processed/cashflow.db

# 4. 启动 Web 仪表盘
python3 app/main.py --db data/processed/cashflow.db --input data/raw
```

### 配置文件

可以将 BeeCount 来源保存为本地配置文件 `data/processed/beecount_source.json`（已被 `.gitignore` 忽略）：

```json
{
  "base_url": "https://your-beecount-server.com",
  "ledger_id": "1",
  "access_token_env": "BEECOUNT_ACCESS_TOKEN",
  "refresh_token_env": "BEECOUNT_REFRESH_TOKEN",
  "limit": 500
}
```

> ⚠️ 不要把 token 原文写进配置文件。`access_token` 缺失或过期时，同步器会用 `refresh_token` 自动换新。

## 项目结构

```text
family-cashflow-radar/
├── app/
│   ├── main.py                 # Web 仪表盘入口
│   ├── db/
│   │   ├── schema.sql          # 数据库 Schema
│   │   └── seed_rules.sql      # 种子分类规则
│   └── scripts/
│       ├── import_csv.py       # CSV 导入
│       ├── import_beecount.py  # BeeCount API 同步
│       ├── normalize.py        # 交易标准化
│       ├── classify.py         # 规则分类器
│       ├── generate_monthly_cashflow.py  # 月度现金流生成
│       ├── analyze_cashflow.py # 现金流分析
│       ├── simulate_decision.py # 决策模拟
│       └── ...                 # 其他工具脚本
├── data/
│   ├── raw/                    # 原始 CSV 放置区（不提交）
│   └── processed/              # 处理后数据（不提交）
├── docs/
│   ├── prd/                    # 产品需求文档
│   ├── design/                 # 设计文档
│   ├── plans/                  # 实施计划
│   └── logs/                   # 项目日志
├── tests/
│   └── fixtures/               # 测试用合成数据
├── AGENTS.md                   # Agent 施工规则
└── README.md
```

## 核心功能

### 现金流语义分类

规则优先，AI 后置。所有结论必须可解释。

- **内部转账**：信用卡还款、账户互转等，不计入真实收支
- **债务相关**：房贷、车贷、借入资金等
- **投资相关**：基金、股票、理财等
- **资产事件**：大额资产购入/出售
- **稳定收入**：工资、劳务费等
- **固定支出**：房租、物业、保险等
- **日常生活**：餐饮、交通、购物等

### 月度现金流分析

- 每月基础结余
- 真实现金流方向（流入/流出/中性）
- 支出结构分析

### 安全垫与风险评估

- 当前现金余额校准
- 安全月数计算
- 未来 3-6 个月断流风险预警

### 决策模拟

- 提前还贷模拟
- 大额消费/买车模拟
- 投资加仓模拟
- 分期/一次性支付方案对比

### 周期账单管理

- 房贷还款计划
- 固定账单模板
- 提前还贷事件记录

## 开发文档

- [产品需求文档 v0.2](docs/prd/prd-v0.2.md)
- [v0.2 实施计划](docs/plans/v0.2-action-advice-plan.md)
- [BeeCount Cloud 数据源适配计划 v0.3](docs/plans/v0.3-beecount-cloud-source-plan.md)
- [项目日志](docs/logs/project-log.md)

### 历史文档

- [PRD v0.1](docs/prd/prd-v0.1.md)
- [数据库 Schema 与自动分类规则 v0.1](docs/design/database-schema-and-classification-rules-v0.1.md)
- [MVP 实施计划 v0.1](docs/plans/mvp-implementation-plan-v0.1.md)

## 贡献指南

欢迎提交 Issue 和 Pull Request！

1. Fork 本仓库
2. 创建特性分支：`git checkout -b feature/your-feature`
3. 提交变更：`git commit -m 'feat: add your feature'`
4. 推送分支：`git push origin feature/your-feature`
5. 提交 Pull Request

请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 了解详细的贡献规范。

## 许可证

本项目采用 [MIT 许可证](LICENSE)。

---

**分类和建议：规则优先，AI 后置，所有结论必须可解释。**
