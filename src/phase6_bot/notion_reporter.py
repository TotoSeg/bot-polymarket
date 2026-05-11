"""
Phase 6 – Reporting automatique Notion
=======================================
Met à jour une base de données Notion avec les positions ouvertes du bot.
Appelé toutes les 30 minutes par live_bot.py en mode --loop.

Configuration requise dans .env :
    NOTION_TOKEN       : clé d'intégration Notion (secret_xxx)
    NOTION_DATABASE_ID : ID de la base de données Notion cible

Structure de la base Notion (à créer manuellement) :
    Titre        → Title      (question du marché)
    Taille ($)   → Number     (mise en USDC)
    Gain espéré  → Number     (gain attendu en $ si résolution favorable)
    Résolution   → Date       (date de résolution du marché)
    Stratégie    → Select     (S3 ou SP)
    YES entrée   → Number     (prix YES au moment de l'entrée)
    market_id    → Rich text  (identifiant interne, pour upsert)

Le reporter lit live_portfolio.json, synchronise la base Notion :
  - Crée les pages manquantes
  - Met à jour les pages existantes (via market_id)
  - Archive les pages dont les positions sont fermées
"""

import os
import json
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from loguru import logger

# Frais Polymarket (pour calculer le gain net espéré)
POLYMARKET_FEE = 0.02

NOTION_API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Fichier local qui mémorise les market_id → page_id Notion
# Évite de requêter toute la DB à chaque run (rate limit)
_CACHE_FILE = Path(__file__).resolve().parents[2] / "outputs" / "phase6" / "notion_page_ids.json"


def _headers() -> dict:
    token = os.environ.get("NOTION_TOKEN", "")
    if not token:
        raise EnvironmentError("NOTION_TOKEN manquant dans .env")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def _db_id() -> str:
    db = os.environ.get("NOTION_DATABASE_ID", "")
    if not db:
        raise EnvironmentError("NOTION_DATABASE_ID manquant dans .env")
    # Normaliser : retirer les tirets si l'ID est au format UUID sans tirets
    return db.replace("-", "")


# ── Cache local page_id ──────────────────────────────────────────────────────

def _load_cache() -> dict:
    """Charge le mapping market_id → notion_page_id depuis le cache local."""
    if _CACHE_FILE.exists():
        try:
            return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict):
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


# ── Calcul du gain espéré ────────────────────────────────────────────────────

def _expected_gain_usd(pos: dict) -> float:
    """
    Gain espéré en $ si la position se résout favorablement (NO gagne).
    = mise × win_rate × (yes_price / no_price) × (1 − frais) − mise × (1 − win_rate)
    """
    bet       = pos.get("bet_amount", 0.0)
    win_rate  = pos.get("win_rate_prior", 0.97)
    yes_price = pos.get("entry_price_yes", 0.07)
    no_price  = 1.0 - yes_price
    if no_price <= 0 or bet <= 0:
        return 0.0
    gain_if_win = (yes_price / no_price) * (1.0 - POLYMARKET_FEE)
    ev_pct      = win_rate * gain_if_win - (1.0 - win_rate) * 1.0
    return round(bet * ev_pct, 2)


# ── CRUD Notion ───────────────────────────────────────────────────────────────

def _build_page_properties(pos: dict, market_id: str) -> dict:
    """Construit le dict de propriétés Notion pour une position."""
    question         = pos.get("question", "")[:100]
    bet_amount       = pos.get("bet_amount", 0.0)
    gain_espere      = _expected_gain_usd(pos)
    strategy         = pos.get("strategy", "")
    yes_price        = pos.get("entry_price_yes", 0.0)
    resolution_date  = pos.get("resolution_date", "")   # format "YYYY-MM-DD"

    props = {
        "Titre": {
            "title": [{"type": "text", "text": {"content": question}}]
        },
        "Taille ($)": {
            "number": round(bet_amount, 2)
        },
        "Gain espéré ($)": {
            "number": gain_espere
        },
        "Résolution": {
            # Notion attend un objet date avec "start" en ISO 8601
            "date": {"start": resolution_date} if resolution_date else None
        },
        "Stratégie": {
            "select": {"name": strategy}
        },
        "YES entrée": {
            "number": round(yes_price, 4)
        },
        "market_id": {
            "rich_text": [{"type": "text", "text": {"content": market_id}}]
        },
    }
    # Notion rejette les propriétés date avec value None → les retirer si absentes
    if not resolution_date:
        del props["Résolution"]
    return props


def _create_page(market_id: str, pos: dict) -> Optional[str]:
    """Crée une nouvelle page dans la base Notion. Retourne le page_id."""
    db = _db_id()
    props = _build_page_properties(pos, market_id)
    payload = {
        "parent": {"database_id": db},
        "properties": props,
    }
    try:
        r = requests.post(f"{NOTION_API_URL}/pages", headers=_headers(),
                          json=payload, timeout=10)
        r.raise_for_status()
        page_id = r.json().get("id", "")
        logger.debug(f"  Notion page créée : {market_id[:12]}... → {page_id[:8]}...")
        return page_id
    except Exception as e:
        logger.warning(f"  Notion create échoué ({market_id[:12]}...) : {e}")
        return None


def _update_page(page_id: str, market_id: str, pos: dict):
    """Met à jour une page Notion existante."""
    props = _build_page_properties(pos, market_id)
    payload = {"properties": props}
    try:
        r = requests.patch(f"{NOTION_API_URL}/pages/{page_id}", headers=_headers(),
                           json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        logger.warning(f"  Notion update échoué (page {page_id[:8]}...) : {e}")


def _archive_page(page_id: str):
    """Archive (supprime logiquement) une page Notion."""
    try:
        r = requests.patch(f"{NOTION_API_URL}/pages/{page_id}", headers=_headers(),
                           json={"archived": True}, timeout=10)
        r.raise_for_status()
    except Exception as e:
        logger.warning(f"  Notion archive échoué (page {page_id[:8]}...) : {e}")


def _add_resolution_date_to_portfolio(portfolio: dict):
    """
    Tente d'ajouter la date de résolution aux positions si elle manque.
    (Enrichissement optionnel – ne bloque pas si l'API est lente.)
    """
    # Import ici pour éviter les dépendances circulaires
    try:
        from src.phase5_paper.polymarket_client import get_market
        from src.phase6_bot.live_bot import parse_end_date
    except ImportError:
        return

    for mid, pos in portfolio.get("positions_ouvertes", {}).items():
        if "resolution_date" not in pos:
            try:
                market = get_market(mid)
                if market:
                    end_dt = parse_end_date(market)
                    if end_dt.year != 9999:
                        pos["resolution_date"] = end_dt.strftime("%Y-%m-%d")
            except Exception:
                pass


# ── Fonction principale ───────────────────────────────────────────────────────

def update_notion_report(portfolio: dict):
    """
    Synchronise la base Notion avec les positions ouvertes du portfolio.

    Algorithme :
      1. Charger le cache local market_id → page_id
      2. Pour chaque position ouverte : créer ou mettre à jour la page
      3. Archiver les pages dont la position est fermée
      4. Sauvegarder le cache mis à jour

    Les pages sont créées avec les propriétés définies dans _build_page_properties().
    Le tri par date de résolution doit être configuré dans la vue Notion (une seule fois).
    """
    # Vérifier que les variables d'env sont présentes
    if not os.environ.get("NOTION_TOKEN") or not os.environ.get("NOTION_DATABASE_ID"):
        logger.debug("NOTION_TOKEN ou NOTION_DATABASE_ID absent – reporting Notion désactivé")
        return

    try:
        _add_resolution_date_to_portfolio(portfolio)

        cache        = _load_cache()
        open_ids     = set(portfolio.get("positions_ouvertes", {}).keys())
        cached_ids   = set(cache.keys())

        nb_created = 0
        nb_updated = 0
        nb_archived = 0

        # Créer ou mettre à jour les positions ouvertes
        for mid, pos in portfolio.get("positions_ouvertes", {}).items():
            # Ajouter la date de résolution dans les propriétés si disponible
            if mid in cache:
                _update_page(cache[mid], mid, pos)
                nb_updated += 1
            else:
                page_id = _create_page(mid, pos)
                if page_id:
                    cache[mid] = page_id
                    nb_created += 1

        # Archiver les pages dont la position est maintenant fermée
        for mid in cached_ids - open_ids:
            _archive_page(cache[mid])
            del cache[mid]
            nb_archived += 1

        _save_cache(cache)
        logger.info(f"Notion mis à jour : {nb_created} créées, {nb_updated} mises à jour, "
                    f"{nb_archived} archivées")

    except EnvironmentError as e:
        logger.debug(f"Notion non configuré : {e}")
    except Exception as e:
        logger.warning(f"Erreur reporting Notion : {e}")
