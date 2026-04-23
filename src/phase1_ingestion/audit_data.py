"""
Phase 1 — Audit qualité des données
=====================================
Ce script inspecte les fichiers Parquet avec DuckDB
pour produire un rapport de qualité sans tout charger en RAM.

Usage :
    python src/phase1_ingestion/audit_data.py
    python src/phase1_ingestion/audit_data.py --file quant.parquet
"""

import sys
import argparse
from pathlib import Path
from datetime import datetime

# Forcer UTF-8 sur Windows pour éviter les erreurs d'encodage dans le terminal
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import duckdb
from loguru import logger

# =============================================================================
# CONFIGURATION
# =============================================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR     = PROJECT_ROOT / "data"
LOGS_DIR     = PROJECT_ROOT / "logs"
OUTPUTS_DIR  = PROJECT_ROOT / "outputs" / "phase1"


def setup_logger():
    """Configure le logger console + fichier."""
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_audit_data.log"
    logger.remove()
    logger.add(sys.stdout, colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
    logger.add(log_file, format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}")
    return log_file


def audit_parquet(filepath: Path, con: duckdb.DuckDBPyConnection):
    """
    Audite un fichier Parquet avec DuckDB.

    DuckDB lit le fichier en streaming : il ne charge que les colonnes
    et les lignes nécessaires en RAM, peu importe la taille du fichier.
    """
    filename = filepath.name
    logger.info(f"\n{'═' * 60}")
    logger.info(f"Audit : {filename}")
    logger.info(f"Chemin : {filepath}")

    # Taille du fichier sur disque
    size_bytes = filepath.stat().st_size
    size_mb = size_bytes / (1024 ** 2)
    size_str = f"{size_mb/1024:.2f} GB" if size_mb > 1024 else f"{size_mb:.1f} MB"
    logger.info(f"Taille sur disque : {size_str}")

    # ── 1. Schéma (noms et types de colonnes) ─────────────────────────────────
    logger.info("\n─── Schéma des colonnes ───")
    schema = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{filepath}')").fetchdf()
    for _, row in schema.iterrows():
        logger.info(f"  {row['column_name']:40s} {row['column_type']}")

    # ── 2. Nombre de lignes et colonnes ───────────────────────────────────────
    n_rows = con.execute(f"SELECT COUNT(*) FROM read_parquet('{filepath}')").fetchone()[0]
    n_cols = len(schema)
    logger.info(f"\n─── Dimensions ───")
    logger.info(f"  Lignes   : {n_rows:>15,}")
    logger.info(f"  Colonnes : {n_cols:>15,}")

    # ── 3. Valeurs nulles par colonne ─────────────────────────────────────────
    logger.info(f"\n─── Valeurs nulles (% sur {n_rows:,} lignes) ───")
    col_names = schema['column_name'].tolist()

    # Construire une requête qui compte les nulls pour chaque colonne
    null_exprs = ", ".join([f"COUNT(*) FILTER (WHERE \"{c}\" IS NULL) AS \"{c}\"" for c in col_names])
    null_counts = con.execute(f"SELECT {null_exprs} FROM read_parquet('{filepath}')").fetchone()

    cols_with_nulls = 0
    for col, count in zip(col_names, null_counts):
        pct = (count / n_rows * 100) if n_rows > 0 else 0
        if count > 0:
            logger.warning(f"  {col:40s} {count:>12,} nulls ({pct:.1f}%)")
            cols_with_nulls += 1
    if cols_with_nulls == 0:
        logger.success("  Aucune valeur nulle détectée")

    # ── 4. Aperçu des premières lignes ────────────────────────────────────────
    logger.info(f"\n─── Aperçu (5 premières lignes) ───")
    preview = con.execute(f"SELECT * FROM read_parquet('{filepath}') LIMIT 5").fetchdf()
    # Afficher colonne par colonne pour rester lisible
    for col in preview.columns:
        values = preview[col].tolist()
        logger.info(f"  {col:40s} {values}")

    # ── 5. Statistiques sur les colonnes numériques ───────────────────────────
    numeric_cols = schema[schema['column_type'].isin(['DOUBLE', 'FLOAT', 'BIGINT', 'INTEGER', 'HUGEINT'])]['column_name'].tolist()
    if numeric_cols:
        logger.info(f"\n─── Statistiques colonnes numériques ───")
        for col in numeric_cols[:10]:  # Limiter à 10 colonnes pour éviter un log trop long
            stats = con.execute(f"""
                SELECT
                    MIN("{col}")  AS min,
                    MAX("{col}")  AS max,
                    AVG("{col}")  AS mean,
                    STDDEV("{col}") AS std
                FROM read_parquet('{filepath}')
            """).fetchone()
            logger.info(f"  {col:40s} min={stats[0]:.4g}  max={stats[1]:.4g}  mean={stats[2]:.4g}  std={stats[3]:.4g}")

    # ── 6. Doublons (clé primaire) ────────────────────────────────────────────
    # On vérifie si la première colonne a des doublons (souvent l'ID)
    first_col = col_names[0]
    n_distinct = con.execute(f"SELECT COUNT(DISTINCT \"{first_col}\") FROM read_parquet('{filepath}')").fetchone()[0]
    logger.info(f"\n─── Unicité ───")
    logger.info(f"  Colonne '{first_col}' : {n_distinct:,} valeurs distinctes / {n_rows:,} lignes")
    if n_distinct == n_rows:
        logger.success(f"  Pas de doublons sur '{first_col}'")
    else:
        logger.warning(f"  {n_rows - n_distinct:,} doublons potentiels sur '{first_col}'")

    return {
        "fichier": filename,
        "taille": size_str,
        "lignes": n_rows,
        "colonnes": n_cols,
        "colonnes_avec_nulls": cols_with_nulls,
    }


def main(files_to_audit: list[str]):
    """Point d'entrée : audite les fichiers Parquet demandés."""
    log_file = setup_logger()
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("PHASE 1 — Audit qualité des données Polymarket")
    logger.info("=" * 60)

    # Connexion DuckDB en mémoire (pas de fichier .duckdb créé)
    con = duckdb.connect()

    results = []
    for filename in files_to_audit:
        filepath = DATA_DIR / filename
        if not filepath.exists():
            logger.warning(f"Fichier absent, skip : {filepath}")
            logger.info(f"  → Lancez d'abord : python src/phase1_ingestion/download_data.py --file {filename}")
            continue
        result = audit_parquet(filepath, con)
        results.append(result)

    # Résumé global
    if results:
        logger.info(f"\n{'=' * 60}")
        logger.info("RÉSUMÉ DE L'AUDIT")
        logger.info("=" * 60)
        for r in results:
            logger.info(f"  {r['fichier']:30s} {r['taille']:>10s}  {r['lignes']:>15,} lignes  {r['colonnes']} cols  {r['colonnes_avec_nulls']} cols avec nulls")

    con.close()
    logger.info(f"\nLog complet : {log_file}")
    logger.success("Audit terminé.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit qualité des fichiers Parquet Polymarket")
    parser.add_argument(
        "--file",
        type=str,
        default=None,
        help="Nom du fichier à auditer (ex: markets.parquet). Si absent, audite tous les fichiers présents."
    )
    args = parser.parse_args()

    if args.file:
        files = [args.file]
    else:
        # Auditer tous les fichiers .parquet présents dans data/
        present = sorted(DATA_DIR.glob("*.parquet")) if DATA_DIR.exists() else []
        files = [f.name for f in present]
        if not files:
            logger.error("Aucun fichier .parquet dans data/ — lancez d'abord download_data.py")
            sys.exit(1)

    main(files)
