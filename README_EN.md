[English](README_EN.md) | 中文

# Family Cashflow Radar

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![SQLite](https://img.shields.io/badge/SQLite-3-003B57?logo=sqlite&logoColor=white)](https://www.sqlite.org/)
[![Tests](https://img.shields.io/badge/Tests-Pytest-0A9EDC?logo=pytest&logoColor=white)](tests/)

> BeeCount Cloud transaction layer + local family cashflow analysis and decision system.

## Table of Contents

- [Project Goals](#project-goals)
- [System Architecture](#system-architecture)
- [Getting Started](#getting-started)
- [Project Structure](#project-structure)
- [Core Features](#core-features)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)

## Project Goals

This project is not another daily expense tracker. BeeCount / BeeCount Cloud handles transaction recording, mobile input, multi-device sync, accounts, categories, budgets, and basic ledger management. This project translates ledger transactions into a family financial decision system.

Version 1 answers 6 questions:

1. What is the actual monthly base surplus?
2. Is the current real cashflow safe?
3. How much pressure do mortgage, car loan, and fixed household expenses create?
4. Is there a cashflow break risk in the next 3-6 months?
5. Can we afford a major financial decision?
6. If not, how much is missing and when can we afford it?

## System Architecture

```text
┌─────────────────────────────────────────────────────────────┐
│                    BeeCount Cloud                           │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐    │
│  │ iOS App  │  │ Android  │  │ Web App  │  │   MCP    │    │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘    │
│       └──────────────┼──────────────┼──────────────┘         │
│                      ▼                                      │
│              ┌───────────────┐                              │
│              │ Transaction   │                              │
│              │ Recording     │                              │
│              └───────┬───────┘                              │
└──────────────────────┼──────────────────────────────────────┘
                       │ Read-only sync
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                Family Cashflow Radar                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │ Data Import  │  │ Rule-based   │  │ Monthly      │      │
│  │ (API/CSV)    │→│ Classification│→│ Cashflow     │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│                            │                                │
│                            ▼                                │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │ Safety Net   │  │ Decision     │  │ Actionable   │      │
│  │ Analysis     │  │ Simulation   │  │ Advice       │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
└─────────────────────────────────────────────────────────────┘
```

### Product Layers

| Layer | Responsibility |
|-------|---------------|
| **BeeCount Cloud** | Transaction recording: iOS / Android / Web recording, sync, backup, attachments, accounts, categories, and budgets |
| **Family Cashflow Radar** | Analysis & decision: cashflow semantics, rule classification, monthly cashflow, family safety net, advice, and decision simulation |
| **Local SQLite** | Stores only analysis data: raw mirrors, normalized results, manual overrides, predictions, advice, and simulation results |
| **CSV Import** | Retained as historical ledger migration and fallback channel |

### Data Source Integration

BeeCount Cloud can serve as a data source through:

1. **MCP Read-only Tools**: Read ledgers, transactions, accounts, categories, budgets, and analytics.
2. **BeeCount Cloud Read API**: Read transactions and workspace data from server projection using a regular access token.
3. **BeeCount Cloud SQLite / Backup Offline Import**: For local batch sync and disaster recovery analysis.

Default policy: read-only consumption of BeeCount data. This project does not write transactions to BeeCount Cloud unless explicitly authorized by the user.

## Getting Started

### Prerequisites

- Python 3.11+
- SQLite 3
- (Optional) BeeCount Cloud instance

### Option 1: Using BeeCount Cloud API (Recommended)

```bash
# 1. Clone the repository
git clone https://github.com/Robs87/family-cashflow-radar.git
cd family-cashflow-radar

# 2. Set environment variables
export BEECOUNT_ACCESS_TOKEN=your_a...  export BEECOUNT_REFRESH_TOKEN=your_r..._token

# 3. Start the web dashboard
python3 app/main.py \
  --db data/processed/cashflow.db \
  --beecount-base-url https://your-beecount-server.com \
  --beecount-ledger-id <your-ledger-id>
```

Open the local address shown in the terminal to view analysis results.

### Option 2: Using Local CSV

```bash
# 1. Place CSV files in data/raw/ directory

# 2. Initialize database
mkdir -p data/processed
sqlite3 data/processed/cashflow.db < app/db/schema.sql
sqlite3 data/processed/cashflow.db < app/db/seed_rules.sql

# 3. Run the data processing pipeline
python3 app/scripts/import_csv.py --db data/processed/cashflow.db --input data/raw
python3 app/scripts/normalize.py --db data/processed/cashflow.db
python3 app/scripts/classify.py --db data/processed/cashflow.db
python3 app/scripts/generate_monthly_cashflow.py --db data/processed/cashflow.db
python3 app/scripts/print_summary.py --db data/processed/cashflow.db

# 4. Start the web dashboard
python3 app/main.py --db data/processed/cashflow.db --input data/raw
```

### Configuration File

You can save the BeeCount source as a local configuration file `data/processed/beecount_source.json` (ignored by `.gitignore`):

```json
{
  "base_url": "https://your-beecount-server.com",
  "ledger_id": "1",
  "access_token_env": "BEECOUNT_ACCESS_TOKEN",
  "refresh_token_env": "BEECOUNT_REFRESH_TOKEN",
  "limit": 500
}
```

> ⚠️ Do not write token plaintext into configuration files. When `access_token` is missing or expired, the synchronizer will automatically refresh using `refresh_token`.

## Project Structure

```text
family-cashflow-radar/
├── app/
│   ├── main.py                 # Web dashboard entry point
│   ├── db/
│   │   ├── schema.sql          # Database schema
│   │   └── seed_rules.sql      # Seed classification rules
│   └── scripts/
│       ├── import_csv.py       # CSV import
│       ├── import_beecount.py  # BeeCount API sync
│       ├── normalize.py        # Transaction normalization
│       ├── classify.py         # Rule-based classifier
│       ├── generate_monthly_cashflow.py  # Monthly cashflow generation
│       ├── analyze_cashflow.py # Cashflow analysis
│       ├── simulate_decision.py # Decision simulation
│       └── ...                 # Other utility scripts
├── data/
│   ├── raw/                    # Raw CSV placement (not committed)
│   └── processed/              # Processed data (not committed)
├── docs/
│   ├── prd/                    # Product requirements
│   ├── design/                 # Design documents
│   ├── plans/                  # Implementation plans
│   └── logs/                   # Project logs
├── tests/
│   └── fixtures/               # Synthetic test data
├── AGENTS.md                   # Agent development rules
└── README.md
```

## Core Features

### Cashflow Semantic Classification

Rule-first, AI-back. All conclusions must be explainable.

- **Internal Transfers**: Credit card payments, account transfers, etc. — not counted as real income/expense
- **Debt-related**: Mortgage, car loan, borrowed funds, etc.
- **Investment-related**: Funds, stocks, financial products, etc.
- **Asset Events**: Large asset purchases/sales
- **Stable Income**: Salary, freelance fees, etc.
- **Fixed Expenses**: Rent, property fees, insurance, etc.
- **Daily Living**: Dining, transportation, shopping, etc.

### Monthly Cashflow Analysis

- Monthly base surplus
- Real cashflow direction (inflow/outflow/neutral)
- Expense structure breakdown

### Safety Net & Risk Assessment

- Current cash balance calibration
- Safety months calculation
- 3-6 month cashflow break risk alert

### Decision Simulation

- Mortgage prepayment simulation
- Large purchase / car purchase simulation
- Investment increase simulation
- Installment vs. one-time payment comparison

### Recurring Bill Management

- Mortgage repayment schedule
- Fixed bill templates
- Prepayment event recording

## Documentation

- [Product Requirements v0.2](docs/prd/prd-v0.2.md)
- [v0.2 Implementation Plan](docs/plans/v0.2-action-advice-plan.md)
- [BeeCount Cloud Data Source Plan v0.3](docs/plans/v0.3-beecount-cloud-source-plan.md)
- [Project Log](docs/logs/project-log.md)

### Historical Documents

- [PRD v0.1](docs/prd/prd-v0.1.md)
- [Database Schema & Auto Classification Rules v0.1](docs/design/database-schema-and-classification-rules-v0.1.md)
- [MVP Implementation Plan v0.1](docs/plans/mvp-implementation-plan-v0.1.md)

## Contributing

Issues and Pull Requests are welcome!

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Commit your changes: `git commit -m 'feat: add your feature'`
4. Push to the branch: `git push origin feature/your-feature`
5. Submit a Pull Request

Please read [CONTRIBUTING.md](CONTRIBUTING.md) for detailed contribution guidelines.

## License

This project is licensed under the [MIT License](LICENSE).

---

**Classification and advice: rule-first, AI-back. All conclusions must be explainable.**
