# Polymarket Bot — Instructions Claude Code

## Objectif du projet
Construire un bot de trading sur Polymarket atteignant ≥75% de win rate,
à partir de 107 GB de données historiques (1.1 milliard de trades).

## Stack technique
- Python 3.12+
- DuckDB (requêtes sur fichiers Parquet sans tout charger en RAM)
- Pandas / Polars (manipulation de données)
- Matplotlib / Plotly (visualisation)
- Polymarket CLOB API (exécution des ordres)
- Git (versioning avec branches par phase)

## Données disponibles
- `data/markets.parquet` — 268K marchés (métadonnées, questions, résolutions)
- `data/quant.parquet` — 170M trades nettoyés, perspective YES unifiée
- `data/users.parquet` — 340M records comportement utilisateurs (phase avancée)

## Structure du projet
Respecter strictement l'arborescence décrite dans ARCHITECTURE.md.

## Règles de développement
1. Chaque phase majeure = nouvelle branche Git (ex: phase/2-eda, phase/3-edge)
2. Commit après chaque fichier fonctionnel créé
3. Toujours travailler avec DuckDB pour les gros fichiers (jamais pd.read_parquet sur quant.parquet entier)
4. Chaque script doit logger ses résultats dans `logs/`
5. Les résultats d'analyse vont dans `outputs/`

## Phases du projet
1. Ingestion & audit des données
2. Analyse exploratoire (EDA)
3. Identification des edges (biais systématiques)
4. Backtesting des stratégies
5. Paper trading (simulation live)
6. Déploiement du bot

## Contexte utilisateur
Niveau Python basique — commenter le code abondamment, expliquer les choix techniques.

# Nouveau module LP farming — lire avant toute action sur le LP

Trois fichiers de contexte à lire dans cet ordre avant de travailler sur le bot LP :
1. ./polymarket_common_setup.md
2. ./polymarket_migration_instructions.md
3. ./polymarket_lp_bot_context.md

Ne pas commencer à coder le bot LP avant d'avoir lu les trois
et fait l'audit décrit dans polymarket_migration_instructions.md étape 1.