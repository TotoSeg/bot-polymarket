"""
polymarket_common/types.py
============================
Dataclasses partagées entre bot_directional et bot_lp.
Ne pas modifier sans impacter les deux bots.

DirectionalPosition reflète le format RÉEL des positions du bot existant
(src/phase5_paper/paper_portfolio.py::add_position), où l'état est aujourd'hui
stocké en dict JSON brut, pas en dataclass. Cette dataclass documente ce
format pour référence/typage futur — le bot directionnel continue d'utiliser
le dict JSON tel quel (cf. étape 1 : ne pas changer le format de l'état).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class MarketInfo:
    condition_id:       str
    slug:               str
    question:           str
    end_date:           datetime
    midpoint:           float
    spread:             float
    reward_epoch:       float          # pool journalier ($)
    rewards_min_size:   float
    rewards_max_spread: float
    volume_24hr:        float
    liquidity:          float
    yes_token_id:       str
    no_token_id:        str
    fees_enabled:       bool


@dataclass
class LPPosition:
    condition_id:                str
    yes_token_id:                str
    no_token_id:                 str
    bid_order_id:                Optional[str]
    ask_order_id:                Optional[str]
    bid_price:                   float
    ask_price:                   float
    size_per_side:                float
    entry_reward_nette_estimee:  float   # baseline pour calcul divergence
    entry_time:                  datetime
    competitiveness_last_check:  Optional[datetime] = None


@dataclass
class DirectionalPosition:
    """
    Reflète une entrée de portfolio["positions_ouvertes"] telle que produite
    par paper_portfolio.add_position(). Champs identiques, mêmes noms.
    """
    market_id:        str
    condition_id:     str
    question:         str
    strategy:         str             # "S3", "SP", "SY", ...
    direction:        str             # "NO" ou "YES"
    win_rate_prior:   float
    entry_price_yes:  float
    bet_amount:       float
    entry_date:       str
    reason:           str
    resolution_date:  Optional[str] = None
