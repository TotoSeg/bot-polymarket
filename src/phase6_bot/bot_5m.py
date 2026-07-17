"""
Bot mean-reversion 5 minutes — Polymarket
==========================================
Stratégie : détecter 3 bougies BTC/ETH/SOL/XRP/DOGE consécutives dans le même
sens (ex: 3 DOWN) + dernière bougie > p90 de l'asset → entrer sur le marché
Polymarket "Up or Down 5m" correspondant côté opposé (mean-reversion).

Stratégie par asset (compromis EV / risque) :
  BTC, XRP : S1 — acheter côté mean-rev, tenir jusqu'à résolution
  ETH, SOL, DOGE : S3 — S1 + ordre limite hedge à 30¢ + ordre limite vente à 95¢

Avantage clé du CLOB Polymarket :
  Les ordres hedge et vente sont des ordres LIMITE GTC (Good Till Cancelled).
  Ils sont placés immédiatement après l'ordre principal, sans latence ni monitoring.
  Le CLOB les exécute automatiquement quand le prix atteint le seuil.

Déclenchement anticipé :
  On surveille la 3e bougie en temps réel via WebSocket Binance.
  À T-10s avant la clôture, si le signal semble se confirmer → on pré-prépare l'ordre.
  À T+0 (clôture bougie = ouverture marché suivant) → ordre envoyé immédiatement.

Usage :
  python src/phase6_bot/bot_5m.py --paper                ← simulation sans ordres réels (défaut)
  python src/phase6_bot/bot_5m.py --status               ← affiche le portfolio et P&L
  python src/phase6_bot/bot_5m.py --live                 ← ORDRES RÉELS wallet par défaut (.env)
  python src/phase6_bot/bot_5m.py --live --env .env.5m   ← ORDRES RÉELS wallet dédié (.env.5m)
  python src/phase6_bot/bot_5m.py --create-keys          ← génère les credentials API du wallet

Multi-wallet :
  Chaque wallet a son propre fichier .env avec ses credentials.
  live_bot.py  → lit .env        (wallet principal, stratégies S3/SP/SY)
  bot_5m.py    → lit .env.5m     (wallet dédié mean-reversion 5m)
  Les deux bots tournent en parallèle, capital totalement séparé.

Ce bot est INDÉPENDANT de live_bot.py :
  - Portfolio séparé : outputs/phase6/portfolio_5m.json
  - P&L séparé      : logs/pnl_5m.csv
  - Capital séparé  : CAPITAL_5M (configurable ci-dessous)
  - Wallet dédié si --env .env.5m est passé
"""

import sys, os, json, time, asyncio, argparse, csv
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal

import requests
import pandas as pd
import numpy as np

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — modifier ces valeurs selon votre situation
# ──────────────────────────────────────────────────────────────────────────────

PAPER_TRADING  = True    # True = simulation, False = ordres réels
CAPITAL_5M     = 50.0    # $ alloués à ce bot (ne pas dépasser le solde disponible)
BET_PER_TRADE  = 2.0     # $ par trade (position principale)
HEDGE_AMOUNT   = 1.0     # $ sur le hedge (pour les assets S3)
RESERVE        = 5.0     # $ à toujours garder disponibles

# Seuils des ordres limite (en centimes)
HEDGE_PRICE    = 0.30    # acheter le hedge quand le côté opposé est à 30¢ (= notre côté à 70%)
SELL_PRICE     = 0.95    # vendre la position principale quand elle atteint 95¢

# Assets et stratégie associée
# S1 = hold (BTC/XRP : signal plus fort, variance acceptable)
# S3 = hedge + vente 95¢ (ETH/SOL/DOGE : signal moins fiable, réduire la variance)
ASSET_STRATEGY = {
    "BTC":  "S1",
    "XRP":  "S1",
    "ETH":  "S3",
    "SOL":  "S3",
    "DOGE": "S3",
}

# Symboles Binance correspondants
BINANCE_SYMBOLS = {
    "BTC":  "btcusdt",
    "ETH":  "ethusdt",
    "SOL":  "solusdt",
    "XRP":  "xrpusdt",
    "DOGE": "dogeusdt",
}

# Mots-clés pour trouver les marchés 5m sur Polymarket
ASSET_KEYWORDS = {
    "BTC":  ["bitcoin", "btc"],
    "ETH":  ["ethereum", "eth"],
    "SOL":  ["solana", "sol"],
    "XRP":  ["xrp", "ripple"],
    "DOGE": ["doge"],
}

# ──────────────────────────────────────────────────────────────────────────────
# CHEMINS DES FICHIERS
# ──────────────────────────────────────────────────────────────────────────────

PROJECT_ROOT   = Path(__file__).resolve().parents[2]
OUT_DIR        = PROJECT_ROOT / "outputs" / "phase6"
PORTFOLIO_FILE = OUT_DIR / "portfolio_5m.json"
PNL_FILE       = PROJECT_ROOT / "logs" / "pnl_5m.csv"
CANDLE_DIR     = PROJECT_ROOT / "data" / "updown"
LOG_FILE       = PROJECT_ROOT / "logs" / "bot_5m.log"

OUT_DIR.mkdir(parents=True, exist_ok=True)
(PROJECT_ROOT / "logs").mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# CHARGEMENT DES SEUILS p90 ADAPTATIFS
# Calculés depuis les candles téléchargées (90 derniers jours).
# Si le fichier n'existe pas, valeurs par défaut issues de l'analyse.
# ──────────────────────────────────────────────────────────────────────────────

P90_DEFAULTS = {
    "BTC": 0.1808, "ETH": 0.2248, "SOL": 0.2501,
    "XRP": 0.2174, "DOGE": 0.2631,
}

BINANCE_REST = "https://api.binance.com/api/v3/klines"
P90_WINDOW_DAYS = 90      # fenêtre glissante en jours
P90_REFRESH_H   = 1       # recalcul toutes les N heures

def fetch_p90_from_binance(symbol: str, days: int = P90_WINDOW_DAYS) -> float | None:
    """
    Récupère les bougies 5m des N derniers jours depuis Binance REST API
    et calcule le p90 des magnitudes.

    Appelé au démarrage ET toutes les heures pour que le seuil suive
    l'évolution de la volatilité du marché.

    days=90 → 90 × 288 = 25 920 bougies par asset.
    """
    try:
        import time as _time
        end_ms   = int(_time.time() * 1000)
        start_ms = end_ms - days * 24 * 3600 * 1000

        all_rows = []
        cursor   = start_ms
        while cursor < end_ms:
            resp = requests.get(BINANCE_REST, params={
                "symbol":    symbol,
                "interval":  "5m",
                "startTime": cursor,
                "limit":     1000,
            }, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            all_rows.extend(data)
            cursor = data[-1][6] + 1   # close_time + 1ms
            if len(data) < 1000:
                break

        if not all_rows:
            return None

        # Calculer les magnitudes |ret%| = |close - open| / open × 100
        ret_abs = [
            abs((float(r[4]) - float(r[1])) / float(r[1]) * 100)
            for r in all_rows if float(r[1]) > 0
        ]
        p90 = float(np.percentile(ret_abs, 90))
        return round(p90, 4)

    except Exception as e:
        log(f"  fetch_p90 {symbol} : erreur ({e})")
        return None


def load_p90_thresholds() -> dict:
    """
    Calcule les seuils p90 glissants depuis Binance (90 derniers jours).
    Tente d'abord l'API Binance en temps réel.
    Fallback sur les fichiers parquet téléchargés, puis sur les valeurs par défaut.
    """
    thresholds = {}
    for asset, default in P90_DEFAULTS.items():
        symbol = BINANCE_SYMBOLS.get(asset, "").upper() + "T" \
                 if not BINANCE_SYMBOLS.get(asset, "").endswith("t") \
                 else BINANCE_SYMBOLS[asset].upper()
        symbol = BINANCE_SYMBOLS[asset].upper()   # ex: "btcusdt" → "BTCUSDT"

        # 1. Essayer l'API Binance (données fraîches)
        p90 = fetch_p90_from_binance(symbol)
        if p90 is not None:
            thresholds[asset] = p90
            continue

        # 2. Fallback : fichier parquet local
        path = CANDLE_DIR / ("btc_5m_candles.parquet" if asset == "BTC"
                              else f"{asset.lower()}_5m_candles.parquet")
        if path.exists():
            try:
                df = pd.read_parquet(path, columns=["open_time", "open", "close"])
                df["ret_abs"] = ((df["close"] - df["open"]) / df["open"] * 100).abs()
                cutoff = df["open_time"].max() - pd.Timedelta(days=P90_WINDOW_DAYS)
                thresholds[asset] = round(float(
                    df[df["open_time"] >= cutoff]["ret_abs"].quantile(0.90)
                ), 4)
                continue
            except Exception:
                pass

        # 3. Dernier recours : valeur par défaut issue de l'analyse
        thresholds[asset] = default

    return thresholds


# ──────────────────────────────────────────────────────────────────────────────
# TRACKER DE BOUGIES PAR ASSET
# Maintient en mémoire les 3 dernières bougies fermées + la bougie en cours.
# ──────────────────────────────────────────────────────────────────────────────

class CandleTracker:
    """
    Suit les bougies 5m d'un asset en temps réel.

    closed_candles : liste des 3 dernières bougies FERMÉES
                     chaque bougie = dict {open, close, ret_pct, direction, open_ts}
    live_candle    : bougie EN COURS (pas encore fermée), mise à jour toutes les ~2s
    """

    def __init__(self, asset: str, p90: float):
        self.asset         = asset
        self.p90           = p90          # seuil de magnitude "forte bougie"
        self.closed_candles = []          # max 4 bougies fermées en mémoire
        self.live_candle    = None        # bougie en cours
        self.pre_staged     = False       # True si signal pré-détecté à T-10s

    def update_closed(self, candle: dict):
        """Appelé quand Binance signale qu'une bougie est fermée (k.x = true)."""
        self.closed_candles.append(candle)
        if len(self.closed_candles) > 4:
            self.closed_candles.pop(0)   # garder max 4 bougies
        self.pre_staged = False          # réinitialiser le pré-staging

    def update_live(self, candle: dict):
        """Appelé à chaque mise à jour de la bougie en cours."""
        self.live_candle = candle

    def check_signal(self, require_acceleration=False) -> int:
        """
        Vérifie si le signal mean-reversion est déclenché.

        Conditions :
          1. Les 3 dernières bougies fermées sont toutes dans la même direction
          2. La dernière bougie fermée dépasse le seuil p90 (grosse bougie)
          3. Optionnel : accélération (chaque bougie plus grande que la suivante)

        Retourne :
           1 = signal UP    (3 bougies DOWN → acheter côté UP)
          -1 = signal DOWN  (3 bougies UP   → acheter côté DOWN)
           0 = pas de signal
        """
        if len(self.closed_candles) < 3:
            return 0

        # Les 3 dernières bougies (la plus récente en dernier)
        c1 = self.closed_candles[-1]   # la plus récente
        c2 = self.closed_candles[-2]
        c3 = self.closed_candles[-3]

        # Condition 1 : même direction sur 3 bougies
        dirs = [c3["direction"], c2["direction"], c1["direction"]]
        all_down = all(d == 0 for d in dirs)
        all_up   = all(d == 1 for d in dirs)

        if not (all_down or all_up):
            return 0

        # Condition 2 : dernière bougie > p90
        if c1["ret_abs"] < self.p90:
            return 0

        # Condition 3 (optionnelle) : accélération
        if require_acceleration:
            if not (c1["ret_abs"] >= c2["ret_abs"] >= c3["ret_abs"]):
                return 0

        return 1 if all_down else -1   # DOWN→UP ou UP→DOWN

    def get_pre_signal(self) -> int:
        """
        Vérifie le pré-signal à T-10s en incluant la bougie en cours.
        La bougie en cours n'est pas encore fermée mais semble confirmer le signal.
        Permet de pré-préparer l'ordre avant la clôture officielle.
        """
        if self.live_candle is None or len(self.closed_candles) < 2:
            return 0

        c1_live = self.live_candle
        c2      = self.closed_candles[-1]
        c3      = self.closed_candles[-2]

        dirs = [c3["direction"], c2["direction"], c1_live["direction"]]
        all_down = all(d == 0 for d in dirs)
        all_up   = all(d == 1 for d in dirs)

        if not (all_down or all_up):
            return 0

        # La bougie en cours doit déjà avoir dépassé p90
        if c1_live["ret_abs"] < self.p90:
            return 0

        return 1 if all_down else -1


def parse_candle(k: dict) -> dict:
    """Convertit un event Binance kline en dict normalisé."""
    open_  = float(k["o"])
    close_ = float(k["c"])
    ret    = (close_ - open_) / open_ * 100 if open_ > 0 else 0
    return {
        "open":      open_,
        "close":     close_,
        "high":      float(k["h"]),
        "low":       float(k["l"]),
        "ret_pct":   ret,
        "ret_abs":   abs(ret),
        "direction": 1 if close_ > open_ else 0,   # 1=UP, 0=DOWN
        "open_ts":   int(k["t"]) // 1000,           # en secondes
        "is_closed": bool(k["x"]),
    }


# ──────────────────────────────────────────────────────────────────────────────
# SCANNER DE MARCHÉS POLYMARKET
# Trouve le marché 5m ouvert correspondant à l'asset et au timestamp actuel.
# ──────────────────────────────────────────────────────────────────────────────

GAMMA_API = "https://gamma-api.polymarket.com/markets"

def find_5m_market(asset: str, open_ts: int) -> dict | None:
    """
    Cherche le marché Polymarket 5m qui :
    - Correspond à l'asset (bitcoin/btc/ethereum/etc.)
    - S'est ouvert dans les 60 dernières secondes (= juste après la fermeture de la bougie)
    - N'est pas encore résolu
    - Se résout dans les 5-10 prochaines minutes

    open_ts : timestamp Unix (secondes) de l'ouverture de la nouvelle bougie.
    """
    keywords = ASSET_KEYWORDS.get(asset, [])
    end_min  = open_ts + 240    # résolution entre maintenant et +10 minutes
    end_max  = open_ts + 600

    try:
        resp = requests.get(GAMMA_API, params={
            "active":   "true",
            "closed":   "false",
            "limit":    200,
            "order":    "end_date_iso",
            "ascending":"true",
        }, timeout=5)
        resp.raise_for_status()
        markets = resp.json()
    except Exception as e:
        log(f"ERREUR Gamma API : {e}")
        return None

    for m in markets:
        q = str(m.get("question", "")).lower()

        # Doit contenir le nom de l'asset et "5" (pour "5 minutes" ou "5 min")
        if not any(kw in q for kw in keywords):
            continue
        if "5" not in q:
            continue
        if "up or down" not in q and "up/down" not in q:
            continue

        # Vérifier le timing de résolution
        end_date = m.get("endDate") or m.get("end_date_iso")
        if end_date:
            try:
                if isinstance(end_date, str):
                    end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    end_ts = int(end_dt.timestamp())
                else:
                    end_ts = int(end_date)
                if end_min <= end_ts <= end_max:
                    return m
            except Exception:
                pass

    return None


# ──────────────────────────────────────────────────────────────────────────────
# PLACEMENT D'ORDRES (paper trading ou réels)
# ──────────────────────────────────────────────────────────────────────────────

def log(msg: str):
    """Log horodaté dans la console et dans le fichier log."""
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def place_orders(asset: str, signal: int, market: dict,
                 strategy: str, portfolio: dict, client=None) -> dict | None:
    """
    Place les ordres selon la stratégie de l'asset.

    signal = 1  : acheter côté UP (après 3 bougies DOWN)
    signal = -1 : acheter côté DOWN (après 3 bougies UP)

    Ordres placés pour S3 (hedge + vente) :
      1. Ordre MARCHÉ : acheter côté signal à ~50¢ (BET_PER_TRADE $)
      2. Ordre LIMITE GTC : acheter côté OPPOSÉ à 30¢ (HEDGE_AMOUNT $)
         → s'exécute automatiquement si le prix adverse descend à 30¢
      3. Ordre LIMITE GTC : vendre le côté signal à 95¢ (BET_PER_TRADE / 0.50 tokens)
         → s'exécute automatiquement quand le prix monte à 95¢

    Avantage : pas de monitoring, pas de latence, tout est délégué au CLOB.
    """
    # Identifier les token IDs YES et NO
    clob_ids = market.get("clobTokenIds", "[]")
    if isinstance(clob_ids, str):
        try:
            clob_ids = json.loads(clob_ids)
        except Exception:
            clob_ids = []

    if len(clob_ids) < 2:
        log(f"  [{asset}] Impossible de récupérer les token IDs")
        return None

    # Par convention Polymarket : clob_ids[0] = YES (Up), clob_ids[1] = NO (Down)
    yes_token = clob_ids[0]
    no_token  = clob_ids[1]

    # Le côté qu'on achète (mean-reversion)
    buy_token    = yes_token if signal ==  1 else no_token
    oppose_token = no_token  if signal ==  1 else yes_token
    buy_side     = "Up" if signal == 1 else "Down"
    oppose_side  = "Down" if signal == 1 else "Up"

    # Vérifier le capital disponible
    capital_dispo = portfolio.get("capital_disponible", 0) - RESERVE
    total_needed  = BET_PER_TRADE + (HEDGE_AMOUNT if strategy == "S3" else 0)
    if capital_dispo < total_needed:
        log(f"  [{asset}] Capital insuffisant : {capital_dispo:.2f}$ dispo, {total_needed:.2f}$ nécessaires")
        return None

    trade_id = f"{asset}_{int(time.time())}"

    if PAPER_TRADING:
        # ── MODE PAPER TRADING : on simule sans placer d'ordres réels ─────────
        tokens_achetes = BET_PER_TRADE / 0.50   # 4 tokens à 50¢

        log(f"  [PAPER] {asset} signal={'UP' if signal==1 else 'DOWN'} | "
            f"Achat {buy_side} à ~50¢ | Mise : {BET_PER_TRADE}$")

        if strategy == "S3":
            hedge_tokens = HEDGE_AMOUNT / HEDGE_PRICE   # ~3.33 tokens à 30¢
            log(f"  [PAPER] {asset} ordre limite HEDGE : acheter {oppose_side} à {HEDGE_PRICE:.2f}¢ "
                f"({hedge_tokens:.2f} tokens pour {HEDGE_AMOUNT}$)")
            log(f"  [PAPER] {asset} ordre limite VENTE : vendre {buy_side} à {SELL_PRICE:.2f}¢ "
                f"(quand le prix atteint {SELL_PRICE*100:.0f}¢)")

        return {
            "trade_id":       trade_id,
            "asset":          asset,
            "strategy":       strategy,
            "signal":         signal,
            "buy_side":       buy_side,
            "buy_token":      buy_token,
            "oppose_token":   oppose_token,
            "entry_price":    0.50,
            "tokens":         BET_PER_TRADE / 0.50,
            "bet":            BET_PER_TRADE,
            "hedge_placed":   strategy == "S3",
            "hedge_price":    HEDGE_PRICE if strategy == "S3" else None,
            "hedge_tokens":   HEDGE_AMOUNT / HEDGE_PRICE if strategy == "S3" else 0,
            "sell_placed":    strategy == "S3",
            "sell_price":     SELL_PRICE if strategy == "S3" else None,
            "market_id":      market.get("id", ""),
            "market_end_ts":  market.get("endDate", ""),
            "opened_at":      datetime.now(timezone.utc).isoformat(),
            "status":         "open",
        }

    else:
        # ── MODE RÉEL : placer les ordres via CLOB ────────────────────────────
        if client is None:
            log(f"  [{asset}] Client CLOB non initialisé")
            return None

        # Import ici pour ne pas casser le mode paper si le module est absent
        try:
            from py_clob_client_v2 import MarketOrderArgs, OrderType, PartialCreateOrderOptions, Side
            from py_clob_client_v2 import OrderArgs
        except ImportError as e:
            log(f"  [{asset}] Import py-clob-client-v2 échoué : {e}")
            return None

        try:
            # 1. Ordre marché principal (achat immédiat au meilleur prix dispo)
            opts = PartialCreateOrderOptions(tick_size="0.01")
            main_order = client.create_and_post_market_order(
                MarketOrderArgs(
                    token_id = buy_token,
                    amount   = BET_PER_TRADE,
                ),
                options = opts,
            )
            log(f"  [{asset}] Ordre principal placé : {buy_side} {BET_PER_TRADE}$ | id={main_order}")

            entry_price = 0.50   # approximation ; idéalement lire le fill price
            tokens = BET_PER_TRADE / entry_price

            hedge_placed = False
            hedge_tokens = 0.0

            if strategy == "S3":
                # 2. Ordre limite GTC : acheter le hedge (côté opposé) à 30¢
                # Cet ordre dort dans le carnet et s'exécute seul sans monitoring
                hedge_tokens = HEDGE_AMOUNT / HEDGE_PRICE
                hedge_order = client.create_and_post_order(
                    OrderArgs(
                        token_id = oppose_token,
                        price    = float(HEDGE_PRICE),
                        size     = float(round(hedge_tokens, 2)),
                        side     = Side.BUY,
                    ),
                    options = opts,
                )
                log(f"  [{asset}] Ordre hedge GTC placé : {oppose_side} {hedge_tokens:.2f} tokens "
                    f"à {HEDGE_PRICE:.2f}¢ | id={hedge_order}")
                hedge_placed = True

                # 3. Ordre limite GTC : vendre la position principale à 95¢
                sell_order = client.create_and_post_order(
                    OrderArgs(
                        token_id = buy_token,
                        price    = float(SELL_PRICE),
                        size     = float(round(tokens, 2)),
                        side     = Side.SELL,
                    ),
                    options = opts,
                )
                log(f"  [{asset}] Ordre vente GTC placé : {buy_side} {tokens:.2f} tokens "
                    f"à {SELL_PRICE:.2f}¢ | id={sell_order}")

            return {
                "trade_id":     trade_id,
                "asset":        asset,
                "strategy":     strategy,
                "signal":       signal,
                "buy_side":     buy_side,
                "buy_token":    buy_token,
                "oppose_token": oppose_token,
                "entry_price":  entry_price,
                "tokens":       tokens,
                "bet":          BET_PER_TRADE,
                "hedge_placed": hedge_placed,
                "hedge_price":  HEDGE_PRICE if hedge_placed else None,
                "hedge_tokens": hedge_tokens,
                "sell_placed":  strategy == "S3",
                "sell_price":   SELL_PRICE if strategy == "S3" else None,
                "market_id":    market.get("id", ""),
                "opened_at":    datetime.now(timezone.utc).isoformat(),
                "status":       "open",
            }

        except Exception as e:
            log(f"  [{asset}] ERREUR placement ordre : {e}")
            return None


# ──────────────────────────────────────────────────────────────────────────────
# GESTION DU PORTFOLIO ET DU P&L
# Fichier séparé de live_bot.py pour ne pas mélanger les stratégies.
# ──────────────────────────────────────────────────────────────────────────────

def load_portfolio() -> dict:
    """Charge le portfolio 5m (crée un fichier vide si inexistant)."""
    if PORTFOLIO_FILE.exists():
        return json.loads(PORTFOLIO_FILE.read_text())
    return {
        "capital_total":      CAPITAL_5M,
        "capital_disponible": CAPITAL_5M,
        "positions_ouvertes": [],
        "trades_fermes":      0,
        "pnl_total":          0.0,
    }

def save_portfolio(portfolio: dict):
    PORTFOLIO_FILE.write_text(json.dumps(portfolio, indent=2, ensure_ascii=False))

def record_trade(trade: dict):
    """Ajoute une ligne dans le CSV de P&L."""
    header = not PNL_FILE.exists()
    with open(PNL_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "timestamp", "asset", "strategy", "signal", "buy_side",
            "bet", "hedge", "entry_price", "pnl", "status", "market_id"
        ])
        if header:
            w.writeheader()
        w.writerow({
            "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "asset":       trade.get("asset"),
            "strategy":    trade.get("strategy"),
            "signal":      "UP" if trade.get("signal") == 1 else "DOWN",
            "buy_side":    trade.get("buy_side"),
            "bet":         trade.get("bet"),
            "hedge":       HEDGE_AMOUNT if trade.get("hedge_placed") else 0,
            "entry_price": trade.get("entry_price"),
            "pnl":         trade.get("pnl", ""),
            "status":      trade.get("status"),
            "market_id":   trade.get("market_id"),
        })

def print_status(portfolio: dict):
    """Affiche un résumé du portfolio 5m."""
    print("\n" + "=" * 60)
    print("PORTFOLIO BOT 5M")
    print("=" * 60)
    print(f"  Capital total    : {portfolio['capital_total']:.2f}$")
    print(f"  Capital dispo    : {portfolio['capital_disponible']:.2f}$")
    print(f"  P&L total        : {portfolio['pnl_total']:+.2f}$")
    print(f"  Trades fermés    : {portfolio['trades_fermes']}")
    positions = portfolio.get("positions_ouvertes", [])
    print(f"  Positions open   : {len(positions)}")
    for p in positions:
        print(f"    {p['asset']} {p['buy_side']} | {p['bet']}$ | "
              f"stratégie={p['strategy']} | ouvert le {p['opened_at'][:19]}")
    print("=" * 60)


# ──────────────────────────────────────────────────────────────────────────────
# BOUCLE PRINCIPALE — WebSocket Binance multi-assets
# ──────────────────────────────────────────────────────────────────────────────

async def handle_candle_event(msg: str, trackers: dict,
                               p90_thresholds: dict,
                               portfolio: dict,
                               client=None):
    """
    Traite un événement kline Binance.

    Logique de déclenchement :
      1. Mise à jour du tracker avec la bougie en cours
      2. À T-10s avant la clôture : pré-vérifier le signal (pre_staged)
      3. Quand la bougie se ferme (k.x = true) :
           - Vérifier le signal sur les bougies fermées
           - Si signal confirmé → trouver le marché Polymarket → placer l'ordre
    """
    try:
        data = json.loads(msg)
    except Exception:
        return

    if data.get("e") != "kline":
        return

    k     = data["k"]
    sym   = k["s"].upper().replace("USDT", "")  # "BTCUSDT" → "BTC"
    candle = parse_candle(k)

    if sym not in trackers:
        return

    tracker = trackers[sym]

    # Calculer le temps restant dans la bougie (en secondes)
    now_ms      = int(time.time() * 1000)
    close_ms    = int(k["T"])
    secs_left   = max(0, (close_ms - now_ms) / 1000)

    # Mettre à jour la bougie en cours
    tracker.update_live(candle)

    # ── Pré-staging à T-10s ──────────────────────────────────────────────────
    # On vérifie si le signal semble se confirmer avant la clôture officielle.
    # Cela permet de lancer la recherche du marché Polymarket en avance.
    if 5 <= secs_left <= 15 and not tracker.pre_staged:
        pre_sig = tracker.get_pre_signal()
        if pre_sig != 0:
            tracker.pre_staged = True
            direction = "UP" if pre_sig == 1 else "DOWN"
            log(f"  [{sym}] Pré-signal détecté : {direction} | "
                f"bougie en cours = {candle['ret_pct']:+.3f}% | {secs_left:.1f}s restantes")

    # ── Traitement à la clôture de la bougie ─────────────────────────────────
    if candle["is_closed"]:
        tracker.update_closed(candle)

        # Vérifier le signal sur les bougies FERMÉES (confirmation définitive)
        # On utilise require_acceleration=False pour maximiser la fréquence.
        # Le filtre accélération peut être activé en changeant à True.
        signal = tracker.check_signal(require_acceleration=False)

        if signal == 0:
            return   # pas de signal, on attend la prochaine bougie

        direction = "UP" if signal == 1 else "DOWN"
        strategy  = ASSET_STRATEGY.get(sym, "S1")

        log(f"\n{'='*50}")
        log(f"SIGNAL CONFIRMÉ : {sym} → acheter {direction} | stratégie={strategy}")
        log(f"  Dernières bougies : " +
            " | ".join(f"{'UP' if c['direction']==1 else 'DOWN'} {c['ret_pct']:+.3f}%"
                       for c in tracker.closed_candles[-3:]))

        # Trouver le marché Polymarket qui vient d'ouvrir pour cet asset
        open_ts = candle["open_ts"] + 300   # nouvelle bougie commence juste après
        market  = find_5m_market(sym, open_ts)

        if market is None:
            log(f"  [{sym}] Aucun marché 5m trouvé sur Polymarket pour ce signal")
            return

        log(f"  [{sym}] Marché trouvé : {market.get('question', '')[:60]}")

        # Vérifier qu'on n'a pas déjà une position ouverte sur cet asset
        positions = portfolio.get("positions_ouvertes", [])
        if any(p["asset"] == sym for p in positions):
            log(f"  [{sym}] Position déjà ouverte, on skip ce signal")
            return

        # Placer les ordres
        trade = place_orders(sym, signal, market, strategy, portfolio, client)

        if trade:
            # Mettre à jour le portfolio
            portfolio["positions_ouvertes"].append(trade)
            portfolio["capital_disponible"] -= trade["bet"] + (
                HEDGE_AMOUNT if trade.get("hedge_placed") else 0
            )
            save_portfolio(portfolio)
            record_trade(trade)
            log(f"  [{sym}] Trade enregistré : {trade['trade_id']}")


async def run_bot(paper: bool = True):
    """
    Boucle principale asynchrone.
    Ouvre une connexion WebSocket combinée Binance pour tous les assets.
    """
    import websockets

    p90_thresholds = load_p90_thresholds()
    log("Seuils p90 chargés :")
    for a, v in p90_thresholds.items():
        log(f"  {a} : p90 = {v:.4f}%")

    # Initialiser un tracker par asset
    trackers = {
        asset: CandleTracker(asset, p90_thresholds[asset])
        for asset in ASSET_STRATEGY
    }

    # Charger le portfolio
    portfolio = load_portfolio()
    print_status(portfolio)

    # Construire le client CLOB si mode réel
    client = None
    if not paper:
        try:
            # Import depuis le module existant pour réutiliser la config
            sys.path.insert(0, str(PROJECT_ROOT))
            from src.phase6_bot.order_executor import build_client
            client = build_client()
            log("Client CLOB V2 connecté")
        except Exception as e:
            log(f"ERREUR connexion CLOB : {e}")
            return

    # URL WebSocket combinée Binance (tous les assets en une seule connexion)
    streams = "/".join(f"{sym}@kline_5m" for sym in BINANCE_SYMBOLS.values())
    ws_url  = f"wss://stream.binance.com:9443/stream?streams={streams}"

    mode = "PAPER TRADING" if paper else "RÉEL ⚠️"
    log(f"\nBot 5m démarré — mode {mode}")
    log(f"Assets actifs : {list(ASSET_STRATEGY.keys())}")
    log(f"WebSocket Binance : {ws_url[:60]}...")
    log(f"Refresh p90 : toutes les {P90_REFRESH_H}h (fenêtre glissante {P90_WINDOW_DAYS}j)")
    log("En attente de signaux...\n")

    last_p90_refresh = time.time()

    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20) as ws:
                async for raw_msg in ws:
                    # ── Refresh horaire des seuils p90 ──────────────────────
                    now = time.time()
                    if now - last_p90_refresh > P90_REFRESH_H * 3600:
                        log("Refresh p90 en cours...")
                        new_thr = load_p90_thresholds()
                        for asset, tracker in trackers.items():
                            old = tracker.p90
                            tracker.p90 = new_thr.get(asset, tracker.p90)
                            if abs(tracker.p90 - old) > 0.0001:
                                log(f"  {asset} p90 : {old:.4f}% → {tracker.p90:.4f}%")
                        last_p90_refresh = now
                    # Le flux combiné encapsule les données dans {"stream": ..., "data": ...}
                    try:
                        wrapper = json.loads(raw_msg)
                        msg = json.dumps(wrapper.get("data", wrapper))
                    except Exception:
                        msg = raw_msg

                    await handle_candle_event(
                        msg, trackers, p90_thresholds, portfolio, client
                    )

        except Exception as e:
            log(f"WebSocket déconnecté : {e} — reconnexion dans 5s")
            await asyncio.sleep(5)


# ──────────────────────────────────────────────────────────────────────────────
# POINT D'ENTRÉE
# ──────────────────────────────────────────────────────────────────────────────

def load_env_file(env_path: str):
    """
    Charge un fichier .env spécifique dans os.environ.
    Permet d'utiliser un wallet dédié différent de celui de live_bot.py.
    Format attendu : CLE=VALEUR (une par ligne, # = commentaire).
    """
    p = Path(env_path)
    if not p.exists():
        print(f"Fichier .env introuvable : {env_path}")
        return False
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()
    print(f"Credentials chargés depuis {env_path}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Bot mean-reversion 5m Polymarket")
    parser.add_argument("--paper",       action="store_true", default=True,
                        help="Mode simulation (défaut, aucun ordre réel)")
    parser.add_argument("--live",        action="store_true",
                        help="Mode réel — PLACE DES ORDRES VRAIS")
    parser.add_argument("--status",      action="store_true",
                        help="Affiche le portfolio et quitte")
    parser.add_argument("--env",         default=None, metavar="FICHIER",
                        help="Fichier .env à charger (ex: .env.5m pour wallet dédié)")
    parser.add_argument("--create-keys", action="store_true",
                        help="Génère les credentials API depuis POLYMARKET_PRIVATE_KEY")
    args = parser.parse_args()

    # Charger le fichier .env demandé (wallet dédié)
    # Si absent, live_bot.py et bot_5m.py partagent le même wallet (via .env par défaut)
    env_file = args.env or str(Path(__file__).parent / ".env")
    if Path(env_file).exists():
        load_env_file(env_file)

    if args.create_keys:
        pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        if not pk:
            print("Définir POLYMARKET_PRIVATE_KEY dans le fichier .env avant de générer les clés.")
            return
        try:
            sys.path.insert(0, str(PROJECT_ROOT))
            from src.phase6_bot.order_executor import create_api_keys
            keys = create_api_keys(pk)
            print("\nCredentials générés — à copier dans votre .env :")
            for k, v in keys.items():
                print(f"  POLYMARKET_{k.upper()} = {v}")
        except Exception as e:
            print(f"Erreur : {e}")
        return

    if args.status:
        portfolio = load_portfolio()
        print_status(portfolio)
        return

    if args.live:
        print("\n⚠️  MODE RÉEL DEMANDÉ — des ordres réels seront placés.")
        wallet = os.environ.get("POLYMARKET_PROXY_WALLET", "inconnu")
        print(f"   Wallet : {wallet}")
        print("   Tapez 'CONFIRMER' pour continuer ou Entrée pour annuler : ", end="")
        if input().strip() != "CONFIRMER":
            print("Annulé.")
            return
        paper = False
    else:
        paper = True

    asyncio.run(run_bot(paper=paper))


if __name__ == "__main__":
    main()


# ──────────────────────────────────────────────────────────────────────────────
# EXPLICATION DU FONCTIONNEMENT — LIRE AVANT DE LANCER
# ──────────────────────────────────────────────────────────────────────────────
"""
COMMENT FONCTIONNE CE BOT
==========================

1. CONNEXION TEMPS RÉEL (WebSocket Binance)
   ─────────────────────────────────────────
   Le bot ouvre une connexion WebSocket unique vers Binance qui reçoit
   les mises à jour des bougies 5m de BTC, ETH, SOL, XRP et DOGE.
   Chaque mise à jour arrive toutes les ~2 secondes.
   Quand une bougie se ferme, Binance envoie k.x = true.

2. DÉTECTION DU SIGNAL EN DEUX TEMPS
   ────────────────────────────────────
   ÉTAPE 1 — Pré-staging (T-10s avant la clôture) :
     On surveille la 3e bougie encore ouverte. Si elle est en train de se
     fermer dans la bonne direction avec une magnitude > p90, on marque
     le signal comme "pre_staged" et on lance la recherche du marché Polymarket.
     Cela nous donne ~5-10 secondes d'avance.

   ÉTAPE 2 — Confirmation (à T+0, bougie officiellement fermée) :
     On vérifie que les 3 bougies FERMÉES confirment le signal.
     Si oui → on place les ordres immédiatement.

   Pourquoi ce double déclenchement ?
     Le marché Polymarket 5m ouvre exactement quand la bougie BTC se ferme.
     Sans pré-staging, on détecterait le signal 2-3 secondes après l'ouverture,
     le prix aurait déjà bougé de 50¢. Avec le pré-staging, on détecte le signal
     pendant la dernière bougie et on place l'ordre dès l'ouverture officielle.

3. ARCHITECTURE DES ORDRES (clé de la stratégie)
   ─────────────────────────────────────────────
   Pour BTC et XRP (stratégie S1 — signal fort) :
     → 1 seul ordre : achat au marché à ~50¢, tenu jusqu'à résolution

   Pour ETH, SOL, DOGE (stratégie S3 — signal moins fiable) :
     → Ordre 1 : achat au MARCHÉ à ~50¢ (exécuté immédiatement)
     → Ordre 2 : achat LIMITE GTC à 30¢ pour le côté opposé (hedge)
                 GTC = Good Till Cancelled = reste dans le carnet jusqu'à exécution
                 S'exécute AUTOMATIQUEMENT quand le prix atteint 30¢ (= notre côté à 70¢)
                 Pas de monitoring, pas de latence, le CLOB gère tout
     → Ordre 3 : vente LIMITE GTC à 95¢ pour le côté acheté
                 S'exécute automatiquement quand le prix atteint 95¢

   Les ordres 2 et 3 sont placés en < 200ms juste après l'ordre 1.
   Une fois placés, ils ne nécessitent AUCUNE action supplémentaire.

4. LATENCE ET TIMING
   ──────────────────
   - WebSocket Binance reçoit la clôture de bougie en ~50ms
   - Recherche du marché Polymarket (API Gamma) : ~100-200ms
   - Placement ordre principal (CLOB) : ~100-200ms
   - Placement ordres limite hedge + vente : ~100ms chacun
   Total : ~500-700ms après la clôture officielle de la bougie

   Le prix Polymarket ne bouge significativement qu'après ~1-2 secondes
   (quand les autres bots détectent aussi le signal). On a une fenêtre
   suffisante pour entrer à ~50¢.

5. ISOLATION DU PORTFOLIO
   ──────────────────────
   Ce bot utilise :
     outputs/phase6/portfolio_5m.json  ← ses propres positions et capital
     logs/pnl_5m.csv                   ← son propre historique P&L

   live_bot.py utilise :
     outputs/phase6/live_portfolio.json
     (ses propres fichiers)

   Les deux bots n'interfèrent pas. Le seul point commun est le wallet
   Polymarket (même adresse). La séparation est logicielle :
   - CAPITAL_5M = 50$ (alloué à ce bot)
   - Le bot vérifie que capital_disponible - RESERVE > 0 avant de trader

6. MODE PAPER TRADING
   ───────────────────
   Par défaut (--paper), le bot :
     - Reçoit tous les signaux en temps réel
     - Trouve les vrais marchés Polymarket
     - Simule les ordres (log dans portfolio_5m.json et pnl_5m.csv)
     - Ne place AUCUN ordre réel

   Pour passer en réel : --live + taper "CONFIRMER"

7. DÉPENDANCE À INSTALLER
   ──────────────────────
   pip install websockets

   Le reste utilise les bibliothèques déjà présentes (requests, pandas, etc.)
"""
