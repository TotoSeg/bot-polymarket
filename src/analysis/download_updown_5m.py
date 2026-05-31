"""
Téléchargement des données Polymarket Up/Down 5 minutes
=======================================================
Source : HuggingFace BrockMisner/polymarket-btc-updown (MIT)

Structure réelle du dataset :
  data/markets.parquet                                        ← fichier unique (~10 MB)
  data/prices/crypto={ASSET}/timeframe={TF}/part-0.parquet   ← hive-partitionné
  data/ticks/crypto={ASSET}/timeframe={TF}/part-0.parquet    ← hive-partitionné

Subsets téléchargés :
  - markets  : métadonnées + résolution finale Up/Down
  - prices   : évolution du prix intra-fenêtre par marché
  - ticks    : trades individuels on-chain (timestamp ms, prix, taille USDC)

Assets : BTC, ETH, SOL, BNB, XRP, DOGE, HYPE
Timeframe : 5-minute uniquement

Sorties :
  data/updown/markets_5m.parquet
  data/updown/prices_5m.parquet
  data/updown/ticks_5m.parquet

Usage :
    python src/analysis/download_updown_5m.py
"""

import sys
import time
from pathlib import Path

from loguru import logger

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Configuration ────────────────────────────────────────────────────────────

DATASET   = "BrockMisner/polymarket-btc-updown"
HF_BASE   = f"hf://datasets/{DATASET}"
TIMEFRAME = "5-minute"
ASSETS    = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "HYPE"]
OUT_DIR   = Path("data/updown")


def _hive_glob(subset: str) -> str:
    """
    Construit la liste des chemins hive pour un subset donné.
    Chaque asset/timeframe = un fichier part-0.parquet.
    Retourne une liste SQL compatible avec read_parquet([...]).
    """
    paths = [
        f"'{HF_BASE}/data/{subset}/crypto={a}/timeframe={TIMEFRAME}/part-0.parquet'"
        for a in ASSETS
    ]
    return "[" + ", ".join(paths) + "]"


def download_markets(conn, out_path: Path) -> int:
    """
    Markets : fichier unique, filtre sur timeframe et assets dans la requête SQL.
    """
    src = f"'{HF_BASE}/data/markets.parquet'"
    assets_sql = ", ".join(f"'{a}'" for a in ASSETS)

    logger.info(f"[markets] Source : {HF_BASE}/data/markets.parquet")
    t0 = time.time()

    query = f"""
        COPY (
            SELECT
                market_id, question, crypto, timeframe,
                volume, resolution,
                start_ts, end_ts,
                condition_id, up_token_id, down_token_id
            FROM read_parquet({src})
            WHERE timeframe = '{TIMEFRAME}'
              AND crypto IN ({assets_sql})
        )
        TO '{out_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD)
    """
    conn.execute(query)
    rows = conn.execute(f"SELECT COUNT(*) FROM read_parquet('{out_path}')").fetchone()[0]
    size_mb = out_path.stat().st_size / 1024 / 1024
    logger.info(f"[markets] OK — {rows:,} lignes | {size_mb:.1f} MB | {time.time()-t0:.0f}s")
    return rows


def download_hive(conn, subset: str, out_path: Path, columns: list[str]) -> int:
    """
    Prices / ticks : hive-partitionné par crypto et timeframe.
    On lit directement les bons dossiers → pas besoin de WHERE.
    crypto et timeframe sont injectés depuis le chemin hive.
    """
    paths = _hive_glob(subset)
    cols_sql = ", ".join(columns)

    logger.info(f"[{subset}] Source : {HF_BASE}/data/{subset}/crypto=*/timeframe={TIMEFRAME}/")
    t0 = time.time()

    query = f"""
        COPY (
            SELECT {cols_sql}
            FROM read_parquet({paths}, hive_partitioning=true, union_by_name=true)
        )
        TO '{out_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 200000)
    """
    conn.execute(query)
    rows = conn.execute(f"SELECT COUNT(*) FROM read_parquet('{out_path}')").fetchone()[0]
    size_mb = out_path.stat().st_size / 1024 / 1024
    logger.info(f"[{subset}] OK — {rows:,} lignes | {size_mb:.1f} MB | {time.time()-t0:.0f}s")
    return rows


def main():
    import duckdb

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("TÉLÉCHARGEMENT UP/DOWN 5M — HuggingFace")
    logger.info(f"  Dataset   : {DATASET}")
    logger.info(f"  Assets    : {', '.join(ASSETS)}")
    logger.info(f"  Timeframe : {TIMEFRAME}")
    logger.info(f"  Sortie    : {OUT_DIR.resolve()}")
    logger.info("=" * 60)

    conn = duckdb.connect()
    conn.execute("INSTALL httpfs; LOAD httpfs;")
    conn.execute("SET enable_progress_bar = true;")
    conn.execute("SET http_timeout = 600000;")
    conn.execute("SET http_retries = 5;")
    conn.execute("SET threads = 4;")

    results = {}

    # ── 1. Markets ────────────────────────────────────────────────────────────
    out = OUT_DIR / "markets_5m.parquet"
    if out.exists():
        n = conn.execute(f"SELECT COUNT(*) FROM read_parquet('{out}')").fetchone()[0]
        logger.info(f"[markets] Déjà présent ({n:,} lignes) → skip")
        results["markets"] = n
    else:
        try:
            results["markets"] = download_markets(conn, out)
        except Exception as e:
            logger.error(f"[markets] ÉCHEC : {e}")
            if out.exists(): out.unlink()

    # ── 2. Prices ─────────────────────────────────────────────────────────────
    out = OUT_DIR / "prices_5m.parquet"
    if out.exists():
        n = conn.execute(f"SELECT COUNT(*) FROM read_parquet('{out}')").fetchone()[0]
        logger.info(f"[prices] Déjà présent ({n:,} lignes) → skip")
        results["prices"] = n
    else:
        try:
            results["prices"] = download_hive(conn, "prices", out, [
                "market_id", "crypto", "timeframe",
                "timestamp", "up_price", "down_price",
            ])
        except Exception as e:
            logger.error(f"[prices] ÉCHEC : {e}")
            if out.exists(): out.unlink()

    # ── 3. Ticks ──────────────────────────────────────────────────────────────
    out = OUT_DIR / "ticks_5m.parquet"
    if out.exists():
        n = conn.execute(f"SELECT COUNT(*) FROM read_parquet('{out}')").fetchone()[0]
        logger.info(f"[ticks] Déjà présent ({n:,} lignes) → skip")
        results["ticks"] = n
    else:
        try:
            results["ticks"] = download_hive(conn, "ticks", out, [
                "market_id", "timestamp_ms", "crypto", "timeframe",
                "outcome", "side", "price", "size_usdc",
                "spot_price_usdt",
            ])
        except Exception as e:
            logger.error(f"[ticks] ÉCHEC : {e}")
            if out.exists(): out.unlink()

    # ── Résumé ────────────────────────────────────────────────────────────────
    logger.info("\n" + "=" * 60)
    logger.info("RÉSUMÉ")
    logger.info("=" * 60)
    total = 0
    for name, rows in results.items():
        p = OUT_DIR / f"{name}_5m.parquet"
        mb = p.stat().st_size / 1024 / 1024 if p.exists() else 0
        logger.info(f"  {name:8s} : {rows:>10,} lignes | {mb:>7.1f} MB")
        total += rows
    logger.info(f"  {'TOTAL':8s} : {total:>10,} lignes")

    # ── Aperçu markets ────────────────────────────────────────────────────────
    mkt_path = OUT_DIR / "markets_5m.parquet"
    if mkt_path.exists():
        logger.info("\n--- Distribution par asset ---")
        df = conn.execute(f"""
            SELECT
                crypto,
                COUNT(*)                                        AS nb_marches,
                SUM(CASE WHEN resolution=1 THEN 1 ELSE 0 END)  AS up,
                SUM(CASE WHEN resolution=0 THEN 1 ELSE 0 END)  AS down,
                ROUND(AVG(volume), 0)                          AS vol_moyen_usdc,
                MIN(epoch_ms(start_ts * 1000))::DATE           AS debut,
                MAX(epoch_ms(end_ts   * 1000))::DATE           AS fin
            FROM read_parquet('{mkt_path}')
            GROUP BY crypto ORDER BY crypto
        """).df()
        logger.info("\n" + df.to_string(index=False))

    conn.close()
    logger.info("\nTéléchargement terminé.")


if __name__ == "__main__":
    main()
