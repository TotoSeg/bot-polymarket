"""
Enrichissement des résolutions via API Gamma Polymarket
=======================================================
Pagine les marchés fermés "up or down" depuis l'API Gamma (gratuit, sans clé).
Récupère la résolution (Up/Down) pour chaque market_id de nos données HuggingFace.
Sauve : data/updown/resolutions_gamma.parquet

Usage :
    python src/analysis/fetch_resolutions_gamma.py
"""

import sys
import time
import json
from pathlib import Path

import requests
import pandas as pd
from loguru import logger

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

GAMMA_API  = "https://gamma-api.polymarket.com"
PAGE_SIZE  = 100
DELAY      = 0.15
OUT_PATH   = Path("data/updown/resolutions_gamma.parquet")

# Mots-clés identifiant les marchés up/down
KEYWORDS   = ["up or down"]
# Noms complets tels qu'ils apparaissent dans les questions Gamma
ASSET_NAMES = {
    "bitcoin":     "BTC",
    "ethereum":    "ETH",
    "solana":      "SOL",
    "bnb":         "BNB",
    "xrp":         "XRP",
    "dogecoin":    "DOGE",
    "hyperliquid": "HYPE",
}


def _parse_resolution(market: dict):
    """
    Extrait la résolution depuis outcomePrices.
    ["1","0"] → 1 (Up a gagné)
    ["0","1"] → 0 (Down a gagné)
    None      → non résolu ou inconnu
    """
    raw = market.get("outcomePrices")
    if raw is None:
        return None
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        first = float(prices[0])
        if first == 1.0:
            return 1
        elif first == 0.0:
            return 0
    except (ValueError, IndexError, TypeError):
        pass
    return None


def _detect_asset(question: str):
    """Retourne le ticker (ex: 'BTC') ou None si non reconnu."""
    q = question.lower()
    for name, ticker in ASSET_NAMES.items():
        if name in q:
            return ticker
    return None


def _is_updown(question: str) -> bool:
    """Retourne True si le marché est un up/down."""
    q = question.lower()
    return any(k in q for k in KEYWORDS) and _detect_asset(question) is not None


def fetch_all_resolutions() -> pd.DataFrame:
    """
    Pagine l'API Gamma sur les marchés fermés et collecte les résolutions.
    Filtre sur les marchés up/down 5m (via question).
    """
    records = []
    offset  = 0
    page    = 0
    total_seen = 0

    logger.info("Démarrage de la collecte via API Gamma (marchés fermés, tri décroissant)...")

    while True:
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={
                    "closed":          "true",
                    "limit":           PAGE_SIZE,
                    "offset":          offset,
                    "order":           "id",
                    "ascending":       "false",   # plus récents en premier
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.warning(f"Page {page} erreur : {e} — abandon pagination")
            break   # l'API Gamma limite à 10 000 résultats, on s'arrête là

        if not data:
            break

        total_seen += len(data)
        found_this_page = 0

        for m in data:
            question = str(m.get("question", ""))
            if not _is_updown(question):
                continue

            market_id  = str(m.get("id", ""))
            resolution = _parse_resolution(m)
            slug       = str(m.get("slug", ""))
            asset      = _detect_asset(question) or "unknown"

            # Déduire timeframe depuis le slug (btc-updown-5m-...)
            timeframe = "unknown"
            if "5m" in slug:
                timeframe = "5-minute"
            elif "15m" in slug:
                timeframe = "15-minute"
            elif "1h" in slug or "hourly" in slug:
                timeframe = "1-hour"

            records.append({
                "market_id":    market_id,
                "slug":         slug,
                "question":     question[:80],
                "asset":        asset,
                "timeframe":    timeframe,
                "resolution":   resolution,
                "start_date":   m.get("startDate") or m.get("start_date"),
                "end_date":     m.get("endDate")   or m.get("end_date"),
                "volume":       float(m.get("volume", 0) or 0),
            })
            found_this_page += 1

        page  += 1
        offset += PAGE_SIZE

        if page % 20 == 0 or found_this_page > 0:
            logger.info(
                f"Page {page:4d} | vus={total_seen:,} | up/down={len(records):,} | dernier_id={data[-1].get('id')}"
            )

        # Les marchés up/down 5m ont des IDs > ~500_000 (lancés fin 2025).
        # Si on descend sous ce seuil, tous les marchés plus anciens sont hors scope.
        last_id = int(data[-1].get("id", 0) or 0)
        if last_id < 500_000 and len(records) > 0:
            logger.info(f"ID {last_id} < 500_000 — sortie de la zone up/down, arrêt.")
            break

        if len(data) < PAGE_SIZE:
            break

        time.sleep(DELAY)

    logger.info(f"Collecte terminée : {total_seen:,} marchés vus, {len(records):,} up/down trouvés")
    return pd.DataFrame(records)


def main():
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    df = fetch_all_resolutions()

    if df.empty:
        logger.error("Aucun résultat collecté.")
        return

    # Stats
    logger.info("\n=== Distribution par asset / timeframe ===")
    summary = df.groupby(["asset", "timeframe"]).agg(
        nb=("market_id", "count"),
        resolus=("resolution", lambda x: (x.isin([0, 1])).sum()),
        up=("resolution", lambda x: (x == 1).sum()),
        down=("resolution", lambda x: (x == 0).sum()),
    ).reset_index()
    logger.info("\n" + summary.to_string(index=False))

    # Sauvegarde
    df.to_parquet(OUT_PATH, index=False, compression="zstd")
    size_mb = OUT_PATH.stat().st_size / 1024 / 1024
    logger.info(f"\nSauvé : {OUT_PATH} ({len(df):,} lignes | {size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
