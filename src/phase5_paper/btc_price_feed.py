"""
Phase 5 — Flux de prix Bitcoin temps réel (Binance)
=====================================================
Accès aux données BTC via l'API publique Binance (aucune clé API requise).

Endpoints utilisés :
  GET /api/v3/ticker/price     → prix spot actuel
  GET /api/v3/klines           → chandeliers OHLCV (5m, 1h, 1d…)

Cache local : les klines téléchargées sont sauvegardées en CSV dans
outputs/btc_cache/ pour éviter les re-téléchargements coûteux.
"""

import sys
import time
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
import pandas as pd
import numpy as np
from loguru import logger

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BINANCE_BASE = "https://api.binance.com/api/v3"
SYMBOL       = "BTCUSDT"
CACHE_DIR    = Path(__file__).resolve().parents[2] / "outputs" / "btc_cache"


# ── Prix spot en temps réel ───────────────────────────────────────────────────

def get_current_price() -> Optional[float]:
    """Retourne le prix BTC/USDT actuel (dernier trade sur Binance)."""
    try:
        r = requests.get(f"{BINANCE_BASE}/ticker/price",
                         params={"symbol": SYMBOL}, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        logger.warning(f"Binance prix spot : {e}")
        return None


def get_recent_klines(interval: str = "5m", n: int = 60) -> Optional[pd.DataFrame]:
    """
    Retourne les N derniers chandeliers BTC (défaut : 60 bougies de 5 min = 5h).

    interval : '1m', '5m', '15m', '1h', '4h', '1d'
    n        : nombre de bougies (max 1000)

    Colonnes retournées :
        open_time (datetime UTC), open, high, low, close, volume,
        close_time, quote_volume, trades, return_pct
    """
    try:
        r = requests.get(f"{BINANCE_BASE}/klines",
                         params={"symbol": SYMBOL, "interval": interval, "limit": n},
                         timeout=10)
        r.raise_for_status()
        return _parse_klines(r.json())
    except Exception as e:
        logger.warning(f"Binance klines récentes : {e}")
        return None


# ── Klines historiques avec cache ─────────────────────────────────────────────

def get_klines_range(start: datetime, end: datetime,
                     interval: str = "5m") -> Optional[pd.DataFrame]:
    """
    Récupère les klines BTC pour une plage de dates, avec cache disque.

    start / end : datetime UTC
    interval    : '5m', '1h', '1d'

    Retourne un DataFrame trié par open_time.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_key = f"BTC_{interval}_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}.csv"
    cache_path = CACHE_DIR / cache_key

    if cache_path.exists():
        df = pd.read_csv(cache_path, parse_dates=["open_time", "close_time"])
        logger.debug(f"Cache BTC : {len(df)} bougies ({cache_key})")
        return df

    logger.info(f"Téléchargement BTC {interval} de {start.date()} à {end.date()}...")
    all_klines = []
    current = start
    LIMIT = 1000

    while current < end:
        try:
            r = requests.get(f"{BINANCE_BASE}/klines", params={
                "symbol":    SYMBOL,
                "interval":  interval,
                "startTime": int(current.timestamp() * 1000),
                "endTime":   int(end.timestamp() * 1000),
                "limit":     LIMIT,
            }, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning(f"Binance klines historiques : {e}")
            break

        if not data:
            break

        all_klines.extend(data)
        # Avancer au-delà de la dernière bougie reçue
        last_ts = data[-1][6]  # close_time en ms
        current = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc) + timedelta(milliseconds=1)

        if len(data) < LIMIT:
            break
        time.sleep(0.1)

    if not all_klines:
        return None

    df = _parse_klines(all_klines)
    df = df[(df["open_time"] >= start) & (df["open_time"] <= end)]
    df.to_csv(cache_path, index=False)
    logger.info(f"  {len(df)} bougies sauvegardées → {cache_key}")
    return df


def _parse_klines(raw: list) -> pd.DataFrame:
    """Convertit la réponse Binance klines en DataFrame propre."""
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    df["open_time"]  = pd.to_datetime(df["open_time"],  unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[col] = df[col].astype(float)
    df["trades"] = df["trades"].astype(int)
    # Rendement de la bougie
    df["return_pct"] = (df["close"] - df["open"]) / df["open"] * 100
    return df[["open_time", "open", "high", "low", "close", "volume",
               "quote_volume", "trades", "close_time", "return_pct"]].copy()


# ── Indicateurs techniques ─────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ajoute des indicateurs utiles pour les stratégies BTC.

    Colonnes ajoutées :
      ret_1        : rendement sur la dernière bougie (%)
      ret_3        : rendement cumulé sur les 3 dernières bougies
      ret_6        : rendement cumulé sur les 6 dernières bougies
      vol_20       : volatilité réalisée sur 20 bougies (std des rendements)
      vol_ratio    : volatilité actuelle / volatilité moyenne 100 bougies
                     (> 1 = plus volatile que d'habitude)
      direction    : +1 si ret_3 > 0, -1 si ret_3 < 0, 0 sinon
      momentum_pct : ret_6 (momentum à ~30 min sur 5m)
    """
    df = df.copy()
    df["ret_1"]      = df["close"].pct_change() * 100
    df["ret_3"]      = df["close"].pct_change(3) * 100
    df["ret_6"]      = df["close"].pct_change(6) * 100
    df["vol_20"]     = df["ret_1"].rolling(20).std()
    vol_mean_100     = df["ret_1"].rolling(100).std()
    df["vol_ratio"]  = df["vol_20"] / vol_mean_100.replace(0, np.nan)
    df["direction"]  = np.sign(df["ret_3"])
    df["momentum_pct"] = df["ret_6"]
    return df


# ── Snapshot résumé ───────────────────────────────────────────────────────────

def get_btc_snapshot() -> dict:
    """
    Retourne un snapshot complet du contexte BTC actuel.

    Exemple de retour :
        {
          "price":       105432.5,
          "ret_5m":      +0.23,    # rendement de la dernière bougie 5m (%)
          "ret_30m":     -0.41,    # rendement des 6 dernières bougies
          "vol_ratio":   1.15,     # volatilité relative (1 = normale)
          "direction":   +1,       # tendance court-terme (+1 hausse, -1 baisse)
          "timestamp":   "2026-04-28T11:00:00+00:00",
        }
    """
    price = get_current_price()
    klines = get_recent_klines("5m", n=120)

    if klines is None or len(klines) < 10:
        return {"price": price, "ret_5m": None, "ret_30m": None,
                "vol_ratio": None, "direction": 0,
                "timestamp": datetime.now(tz=timezone.utc).isoformat()}

    df = compute_indicators(klines)
    last = df.iloc[-1]

    return {
        "price":      price or float(last["close"]),
        "ret_5m":     round(float(last["ret_1"]), 4)     if pd.notna(last["ret_1"])     else None,
        "ret_30m":    round(float(last["ret_6"]), 4)     if pd.notna(last["ret_6"])     else None,
        "vol_ratio":  round(float(last["vol_ratio"]), 3) if pd.notna(last["vol_ratio"]) else None,
        "direction":  int(last["direction"])              if pd.notna(last["direction"]) else 0,
        "timestamp":  datetime.now(tz=timezone.utc).isoformat(),
    }
