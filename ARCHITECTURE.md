# Architecture — Polymarket Bot

## Arborescence du projet

```
Bot Polymarket/
├── data/                        # Fichiers Parquet bruts (non versionnés)
│   ├── markets.parquet          # 268K marchés
│   ├── quant.parquet            # 170M trades (gros fichier)
│   └── users.parquet            # 340M records (phase avancée)
│
├── src/                         # Code source organisé par phase
│   ├── phase1_ingestion/        # Phase 1 : Ingestion & audit
│   │   ├── download_data.py     # Téléchargement depuis HuggingFace
│   │   └── audit_data.py        # Audit qualité des données
│   │
│   ├── phase2_eda/              # Phase 2 : Analyse exploratoire
│   │   ├── markets_eda.py
│   │   └── trades_eda.py
│   │
│   ├── phase3_edges/            # Phase 3 : Identification des edges
│   │   ├── bias_detection.py
│   │   └── edge_scoring.py
│   │
│   ├── phase4_backtest/         # Phase 4 : Backtesting
│   │   ├── strategy_base.py
│   │   └── backtest_engine.py
│   │
│   ├── phase5_paper/            # Phase 5 : Paper trading
│   │   └── paper_trader.py
│   │
│   └── phase6_bot/              # Phase 6 : Bot live
│       ├── polymarket_api.py
│       └── bot_runner.py
│
├── notebooks/                   # Jupyter notebooks d'exploration
│
├── logs/                        # Logs générés par les scripts
│
├── outputs/                     # Résultats d'analyse (graphiques, CSV)
│
├── tests/                       # Tests unitaires
│
├── .claude/
│   └── settings.json
├── ARCHITECTURE.md
├── CLAUDE.md
├── requirements.txt
└── .gitignore
```

## Conventions
- Scripts : snake_case, préfixe de phase (`p1_`, `p2_`, etc.)
- Branches Git : `phase/1-ingestion`, `phase/2-eda`, etc.
- Logs : `logs/YYYY-MM-DD_nom_script.log`
- Outputs : `outputs/phase{N}/nom_analyse/`
