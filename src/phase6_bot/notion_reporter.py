"""
Phase 6 – Reporting automatique Notion
=======================================
Met à jour une base de données Notion avec les positions ouvertes du bot.
Appelé toutes les 30 minutes par live_bot.py en mode --loop.

Configuration requise dans .env :
    NOTION_TOKEN       : token d'intégration Notion (ntn_... ou secret_...)
    NOTION_DATABASE_ID : ID de la base de données Notion cible (32 hex chars)

Colonnes Notion à créer (noms exacts) :
    Titre          → Title      (question du marché)
    Mise ($)       → Number     (USDC investi à l'entrée)
    Gain espéré (%)→ Number     (EV en % de la mise)
    Résolution     → Date       (date de clôture du marché — colonne de tri)
    Stratégie      → Select     (S3 ou SP)
    YES entrée     → Number     (prix YES au moment de l'entrée)
    market_id      → Text       (identifiant interne, pour upsert)

Note : "Mise ($)" = USDC investi par le bot. Polymarket affiche le payout
potentiel (tokens × 1$) qui est supérieur à la mise car NO < 1$.
"""

import os
import json
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from loguru import logger

POLYMARKET_FEE = 0.02
NOTION_API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

_CACHE_FILE     = Path(__file__).resolve().parents[2] / "outputs" / "phase6" / "notion_page_ids.json"
_PORTFOLIO_FILE = Path(__file__).resolve().parents[2] / "outputs" / "phase6" / "live_portfolio.json"


# ── Helpers internes ─────────────────────────────────────────────────────────

def _parse_end_date_local(m: dict) -> Optional[str]:
    """Extrait la date de résolution d'un marché et retourne 'YYYY-MM-DD' ou None."""
    raw = m.get("endDate") or m.get("endDateIso") or ""
    if not raw:
        return None
    try:
        raw = raw.rstrip("Z").replace("Z", "+00:00")
        dt  = datetime.fromisoformat(raw + ("T00:00:00" if "T" not in raw else ""))
        if dt.year >= 9999:
            return None
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _expected_gain_pct(pos: dict) -> float:
    """Gain espéré en % de la mise (EV × 100)."""
    wr    = pos.get("win_rate_prior", 0.97)
    yes_p = pos.get("entry_price_yes", 0.07)
    no_p  = 1.0 - yes_p
    if no_p <= 0:
        return 0.0
    gain_win = (yes_p / no_p) * (1.0 - POLYMARKET_FEE)
    ev       = wr * gain_win - (1.0 - wr) * 1.0
    return round(ev * 100, 1)


def _headers() -> dict:
    token = os.environ.get("NOTION_TOKEN", "")
    if not token:
        raise EnvironmentError("NOTION_TOKEN manquant dans .env")
    return {
        "Authorization":  f"Bearer {token}",
        "Content-Type":   "application/json",
        "Notion-Version": NOTION_VERSION,
    }


def _db_id() -> str:
    db = os.environ.get("NOTION_DATABASE_ID", "").replace("-", "")
    if not db:
        raise EnvironmentError("NOTION_DATABASE_ID manquant dans .env")
    return db


# ── Cache page_id ────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    if _CACHE_FILE.exists():
        try:
            return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict):
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


# ── Enrichissement des dates de résolution ───────────────────────────────────

def _enrich_resolution_dates(portfolio: dict) -> bool:
    """
    Tente de remplir `resolution_date` pour les positions qui ne l'ont pas encore.
    Interroge l'API Polymarket et sauvegarde le portfolio enrichi.
    Retourne True si au moins une date a été ajoutée.
    """
    try:
        from src.phase5_paper.polymarket_client import get_market
    except ImportError:
        return False

    changed = False
    for mid, pos in portfolio.get("positions_ouvertes", {}).items():
        if "resolution_date" not in pos:
            try:
                market = get_market(mid)
                if market:
                    date_str = _parse_end_date_local(market)
                    if date_str:
                        pos["resolution_date"] = date_str
                        changed = True
            except Exception:
                pass

    if changed:
        # Persister les dates enrichies dans le fichier portfolio
        try:
            import json as _json
            _PORTFOLIO_FILE.write_text(
                _json.dumps(portfolio, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    return changed


# ── CRUD Notion ───────────────────────────────────────────────────────────────

def _build_page_properties(pos: dict, market_id: str) -> dict:
    question        = pos.get("question", "")[:100]
    bet_amount      = pos.get("bet_amount", 0.0)
    gain_espere_pct = _expected_gain_pct(pos)
    strategy        = pos.get("strategy", "")
    yes_price       = pos.get("entry_price_yes", 0.0)
    resolution_date = pos.get("resolution_date", "")

    # Taille = tokens NO détenus × 1$ face value = ce que Polymarket affiche
    no_price = 1.0 - yes_price
    taille   = round(bet_amount / no_price, 2) if no_price > 0 else 0.0

    props = {
        "Titre": {
            "title": [{"type": "text", "text": {"content": question}}]
        },
        "Taille ($)": {
            "number": taille
        },
        "Mise ($)": {
            "number": round(bet_amount, 2)
        },
        "Gain espéré (%)": {
            "number": gain_espere_pct
        },
        "Stratégie": {
            "select": {"name": strategy} if strategy else None
        },
        "YES entrée": {
            "number": round(yes_price, 4)
        },
        "market_id": {
            "rich_text": [{"type": "text", "text": {"content": market_id}}]
        },
    }

    if resolution_date:
        props["Résolution"] = {"date": {"start": resolution_date}}

    return {k: v for k, v in props.items() if v is not None}


def _create_page(market_id: str, pos: dict) -> Optional[str]:
    payload = {
        "parent":     {"database_id": _db_id()},
        "properties": _build_page_properties(pos, market_id),
    }
    try:
        r = requests.post(f"{NOTION_API_URL}/pages", headers=_headers(),
                          json=payload, timeout=10)
        r.raise_for_status()
        return r.json().get("id", "")
    except Exception as e:
        logger.warning(f"  Notion create échoué ({market_id[:8]}...) : {e}")
        return None


def _update_page(page_id: str, market_id: str, pos: dict):
    payload = {"properties": _build_page_properties(pos, market_id)}
    try:
        r = requests.patch(f"{NOTION_API_URL}/pages/{page_id}", headers=_headers(),
                           json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        logger.warning(f"  Notion update échoué ({page_id[:8]}...) : {e}")


def _archive_page(page_id: str):
    try:
        r = requests.patch(f"{NOTION_API_URL}/pages/{page_id}", headers=_headers(),
                           json={"archived": True}, timeout=10)
        r.raise_for_status()
    except Exception as e:
        logger.warning(f"  Notion archive échoué ({page_id[:8]}...) : {e}")


# ── Fonction principale ───────────────────────────────────────────────────────

def update_notion_report(portfolio: dict):
    """
    Synchronise la base Notion avec les positions ouvertes du portfolio.
    Crée/met à jour les pages existantes, archive les positions fermées.
    """
    if not os.environ.get("NOTION_TOKEN") or not os.environ.get("NOTION_DATABASE_ID"):
        logger.debug("Notion désactivé (NOTION_TOKEN ou NOTION_DATABASE_ID absent)")
        return

    try:
        # Enrichir les dates manquantes (positions ouvertes avant la maj du bot)
        _enrich_resolution_dates(portfolio)

        cache       = _load_cache()
        open_ids    = set(portfolio.get("positions_ouvertes", {}).keys())
        cached_ids  = set(cache.keys())

        nb_created = nb_updated = nb_archived = 0

        for mid, pos in portfolio.get("positions_ouvertes", {}).items():
            if mid in cache:
                _update_page(cache[mid], mid, pos)
                nb_updated += 1
            else:
                page_id = _create_page(mid, pos)
                if page_id:
                    cache[mid] = page_id
                    nb_created += 1

        for mid in cached_ids - open_ids:
            _archive_page(cache.pop(mid))
            nb_archived += 1

        _save_cache(cache)
        logger.info(f"Notion mis à jour : {nb_created} créées, {nb_updated} mises à jour, "
                    f"{nb_archived} archivées")

    except EnvironmentError as e:
        logger.debug(f"Notion non configuré : {e}")
    except Exception as e:
        logger.warning(f"Erreur reporting Notion : {e}")
