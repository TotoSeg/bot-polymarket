# Polymarket LP Farming Bot — Context de projet complet

> **Usage** : ce fichier est le contexte complet pour Claude Code. Lire intégralement avant de coder. Contient : mécanique du système de rewards, APIs disponibles, stratégie validée, architecture du bot, gestion des fills, logique de sortie permanente et priorisation des catégories de marchés.

---

## 1. Mécanique fondamentale

### Ce qu'est Polymarket

Marché de prédiction on-chain sur Polygon. Chaque marché est binaire (YES/NO). Les prix sont des probabilités en dollars (0,00–1,00 $). Settlement en pUSD (ERC-20 backed 1:1 USDC).

Infrastructure : off-chain CLOB (Central Limit Order Book) pour le matching, settlement on-chain via CTF (Conditional Token Framework). Deux APIs principales : **Gamma API** (lecture publique, métadonnées) et **CLOB API** (trading, authentifié).

### Les 3 sources de revenus LP (cumulables)

| Source | Condition | Montant | Paiement |
|--------|-----------|---------|----------|
| **Liquidity Rewards** | Ordres resting dans le carnet près du midpoint | Part pro-rata du pool journalier | Minuit UTC quotidien en pUSD |
| **Maker Rebates** | Ordres exécutés (fills) | 20–50 % des taker fees selon catégorie | Quotidien en pUSD |
| **Holding Rewards** | Détenir une position dans marchés éligibles | 4 % APY annualisé | Quotidien en pUSD |

Minimum de paiement liquidity rewards : **1 $ par marché par jour**. En dessous : rien n'est versé.

### Formule de scoring des Liquidity Rewards

```
S(v, s) = ((v - s) / v)² × b
```

- `v` = `rewards_max_spread` du marché (en cents, ex: 5)
- `s` = distance de l'ordre au midpoint (en cents)
- `b` = taille de l'ordre en $
- Courbe **quadratique** : se rapprocher d'1 cent multiplie le score bien plus que linéairement
- Snapshots aléatoires **chaque minute** (1 440/jour)
- Score accumulé → part proportionnelle du pool journalier

**Two-sided vs single-sided :**
- Two-sided (BID + ASK simultanément) : score = `Q_min` × 2 (minimum des deux côtés)
- Single-sided avec midpoint ∈ [0.10, 0.90] : score ÷ 3
- Single-sided avec midpoint hors [0.10, 0.90] : score = **0**
- → Toujours poster des deux côtés

### Mécanique split/merge (sans frais)

- **Split** : 1 pUSD → 1 YES token + 1 NO token (call `splitPosition()` sur CTF)
- **Merge** : 1 YES + 1 NO → 1 pUSD (call `mergePositions()` sur CTF)
- Frais : zéro trading fee, uniquement gas Polygon (< 0,01 $)
- Impossible après résolution du marché (utiliser `redeem` à la place)
- Usage LP : split pour obtenir les deux côtés à poster, merge quand BID+ASK tous deux fillés

---

## 2. APIs disponibles

### Gamma API (public, sans auth)

Base URL : `https://gamma-api.polymarket.com`

Endpoints utiles :

```
GET /markets?active=true&closed=false&limit=100
  → Liste tous les marchés actifs
  → Champs clés : rewardEpoch, rewardsMinSize, rewardsMaxSpread,
    spread, bestAsk, bestBid, liquidity, volume_24hr, endDate,
    midpoint, conditionId, slug

GET /markets?active=true&closed=false&order=rewardEpoch&ascending=false
  → Triés par pool de rewards décroissant

GET /markets/{slug}
  → Détail d'un marché spécifique
```

Payload market (champs LP critiques) :
```json
{
  "conditionId": "0x...",
  "slug": "will-x-happen",
  "endDate": "2026-12-31T00:00:00Z",
  "rewardEpoch": 150.0,
  "rewardsMinSize": 100,
  "rewardsMaxSpread": 5.0,
  "spread": 0.02,
  "bestBid": 0.49,
  "bestAsk": 0.51,
  "liquidity": 45000,
  "volume_24hr": 12000,
  "tokens": [
    {"token_id": "...", "outcome": "YES", "price": 0.50},
    {"token_id": "...", "outcome": "NO",  "price": 0.50}
  ]
}
```

### CLOB API (authentifié pour trading/rewards)

Base URL : `https://clob.polymarket.com`

**Auth headers requis** : `POLY_ADDRESS`, `POLY_API_KEY`, `POLY_PASSPHRASE`, `POLY_SIGNATURE`, `POLY_TIMESTAMP`

SDK officiel Python (post CLOB v2, avril 2026) : `py-clob-client-v2`
SDK communautaire complet : `pip install polymarket-apis` (v2.1.0, mai 2026)

Endpoints rewards (CLOB) :

```
GET  /rewards/markets                     → Public. Config rewards sans market_competitiveness
GET  /rewards/markets/{condition_id}      → Public. Raw rewards pour 1 marché
GET  /rewards/user/markets                → AUTH. Marchés + market_competitiveness + earnings
GET  /rewards/earnings/date               → AUTH. Tes earnings par date
GET  /rewards/earnings/total              → AUTH. Tes earnings totaux
GET  /rewards/percentages                 → AUTH. Ton % réel du pool par marché (ta part live)
```

**Champ clé** : `market_competitiveness` (0.0 → 1.0) dans `/rewards/user/markets`
- Exposé uniquement en authentifié
- C'est exactement ce que l'UI Polymarket affiche dans l'onglet Rewards
- 0 = pas de concurrents, 1 = carnet saturé

Endpoints orderbook (CLOB, public) :

```
GET  /book?token_id={token_id}            → Orderbook complet
GET  /midpoint?token_id={token_id}        → Midpoint temps réel
GET  /spread?token_id={token_id}          → Spread actuel
GET  /price-history?token_id={token_id}&interval=1h  → Historique prix
```

Endpoints ordres (CLOB, authentifié) :

```
POST /order                               → Placer un ordre
DEL  /order/{order_id}                   → Annuler un ordre
DEL  /orders                             → Annuler tous les ordres
DEL  /orders?market={condition_id}       → Annuler ordres d'un marché
GET  /orders?market={condition_id}       → Mes ordres actifs
GET  /trades?market={condition_id}       → Mes trades
```

WebSocket (temps réel) : `wss://ws-subscriptions-clob.polymarket.com/ws/`
- Channel `market` : updates orderbook
- Channel `user` : fills, order updates

---

## 3. Stratégie validée

### Principe

Ne pas prendre de risque directionnel. Gagner des rewards indépendamment du résultat du marché. Le P&L vient des rewards + spread capturé sur fills symétriques, pas de la direction du prix.

### Score de sélection des marchés

```python
score = (rewardEpoch / (competitiveness_proxy + 1)) * duration_factor * liquidity_factor
```

Où :
- `rewardEpoch` = pool journalier en $
- `competitiveness_proxy` = `market_competitiveness` si auth dispo, sinon estimation via carnet
- `duration_factor = min(days_to_end / 30, 1.0)` → pénalise marchés proches de résolution
- `liquidity_factor = 1.0` si spread > rewardsMaxSpread × 1.5 (carnet peu dense), sinon 0.7

**La métrique clé** : `reward_nette_estimée = rewardEpoch / (nb_compétiteurs + 1)`. Un pool de 50 $/j avec 3 concurrents (12,5 $/j) bat un pool de 500 $/j avec 40 concurrents (12,2 $/j).

### Filtres d'entrée (hard filters — élimination automatique)

```python
HARD_FILTERS = {
    "rewardEpoch > 0",                    # marché avec rewards actives
    "days_to_end > 30",                   # pas de résolution imminente — couvre aussi les catalyseurs type annonces Fed
    "0.15 <= midpoint <= 0.85",           # midpoint dans zone éligible two-sided
    "reward_nette_estimee >= 1.0",        # au-dessus du seuil de paiement
    "rewardsMinSize is not None",         # paramètres connus
}
```

### Filtres de qualité (soft filters — pondération du score)

```python
SOFT_FILTERS = {
    "spread > rewardsMaxSpread * 1.5",    # carnet peu compétitif
    "price_volatility_24h < 0.10",        # marché stable (max(price) - min(price) sur 24h)
    "volume_24hr > 1000",                 # marché avec activité minimale
    # Note : pas de détection de catalyseurs — couvert par endDate > 30j + stop-loss 3 cents
}
```

### Sizing des positions

```python
SIZING_RULES = {
    "max_per_market": capital_total * 0.20,          # 20% du capital max par marché
    "target_per_side": rewardsMinSize * 3,            # zone optimale 2-5x minSize
    "min_per_side": rewardsMinSize * 1.2,             # marge sécurité sur seuil
    "max_markets_simultaneous": 5,                    # diversification min
    "order_placement": {
        "BID": midpoint - spread_target,
        "ASK": midpoint + spread_target,
        "spread_target": max(1, rewardsMaxSpread * 0.3),  # 30% du spread max = bon équilibre score/risque
    }
}
```

Au-delà de `rewardsMinSize × 10` : rendement marginal faible (score linéaire, risque adverse fill proportionnel).

---

## 4. Architecture du bot

### Boucle principale

```
SCAN (toutes les 10 min)
  → Gamma API : /markets?active=true&closed=false&order=rewardEpoch
  → Hard filter → Soft filter → Ranking par score
  → Ignorer les condition_id en blacklist
  → Top-K marchés (K = budget / position_size)

POSITION (par marché sélectionné)
  → Enregistrer reward_nette_estimee au moment de l'entrée
  → Split pUSD → YES + NO tokens
  → Poster BID à midpoint - spread_target
  → Poster ASK à midpoint + spread_target
  → Check immédiat compétitivité (voir section 6)

MONITOR (WebSocket continu)
  → Écouter moves du midpoint
  → Si |mid_move| > 1 cent : requoter (annuler + replacer)
  → Si fill : déclencher logique de gestion (voir section 5)

MONITOR COMPÉTITIVITÉ (toutes les heures, sur tous les marchés actifs)
  → GET /rewards/user/markets → lire market_competitiveness
  → Calculer reward_nette_reelle = rewardEpoch * (1 - market_competitiveness)
  → Si reward_nette_reelle / reward_nette_estimee < 0.70 : sortie propre (voir section 6)

MONITOR HARD FILTERS (toutes les heures, sur tous les marchés actifs)
  → Recalculer days_to_end, midpoint, reward_nette
  → Si un hard filter est violé : sortie propre (voir section 6)

REWARDS (passif)
  → Minuit UTC : versement automatique pUSD
  → Logger /rewards/earnings/date pour tracking
```

### Stack technique recommandé

```python
# Dépendances
pip install polymarket-apis  # v2.1.0 — wrapper officieux complet CLOB + Gamma
pip install web3             # interactions on-chain CTF pour split/merge
pip install websockets       # WebSocket pour monitoring temps réel
pip install asyncio          # boucle async

# Structure fichiers
bot/
├── config.py          # paramètres (clés API, seuils, sizing)
├── scanner.py         # scan Gamma + filtrage + ranking + blacklist
├── positions.py       # gestion split/merge/posting d'ordres
├── monitor.py         # WebSocket + logique requoting
├── fills.py           # gestion des fills (arbre de décision)
├── exits.py           # logique de sortie propre (compétitivité + hard filters)
├── rewards.py         # tracking rewards via CLOB API
└── main.py            # orchestration principale
```

---

## 5. Gestion des fills

### Arbre de décision post-fill

```
Fill détecté (BID ou ASK exécuté)
│
├── Calculer mid_move = |midpoint_actuel - midpoint_au_moment_du_fill|
│
├── mid_move < 1 cent
│   └── ACTION : attendre. Fill symétrique probable (marché stable). Garder l'autre ordre en place.
│
├── 1 cent ≤ mid_move ≤ 3 cents
│   └── ACTION : requoter l'autre côté au nouveau midpoint. Garder la position fillée.
│
├── mid_move > 3 cents  [STOP-LOSS]
│   └── ACTION : déclencher séquence de sortie propre (voir ci-dessous)
│
└── YES + NO en inventaire (fill symétrique complet)
    └── ACTION : merge immédiat → 1 $ pUSD récupéré + spread capturé → replacer ordres
```

### Séquence de sortie propre (réutilisée partout)

Cette séquence est appelée dans trois contextes distincts :
1. Stop-loss fill (mid_move > 3 cents)
2. Divergence de compétitivité > 30 % (voir section 6)
3. Violation d'un hard filter sur marché actif (voir section 6)

```python
def exit_market_cleanly(condition_id):
    # Étape 1 : annuler tous les ordres resting
    cancel_all_orders(condition_id)

    # Étape 2 : attendre 5s pour fills résiduels en transit
    sleep(5)

    # Étape 3 : lire l'inventaire final
    yes_balance = get_token_balance(yes_token_id)
    no_balance  = get_token_balance(no_token_id)

    # Étape 4 : merger les paires disponibles
    mergeable = min(yes_balance, no_balance)
    if mergeable > 0:
        merge_positions(condition_id, mergeable)  # → pUSD récupéré

    # Étape 5 : liquider l'excédent via ordre limite au midpoint actuel
    remaining_yes = yes_balance - mergeable
    remaining_no  = no_balance  - mergeable
    if remaining_yes > 0:
        place_limit_order(side="ASK", token=yes_token_id,
                          price=midpoint_current, size=remaining_yes)
    if remaining_no > 0:
        place_limit_order(side="ASK", token=no_token_id,
                          price=1 - midpoint_current, size=remaining_no)

    # Étape 6 : blacklister le marché
    blacklist_market(condition_id, hours=24)
```

### Calcul du P&L sur un cycle complet

```
BID fillé à 0.49 $ (50 shares = 25 $)
ASK fillé à 0.51 $ (50 shares = 25.50 $)
→ Merge : 50 YES + 50 NO → 50 pUSD
→ P&L brut = 50.00 - 49.00 = 1.00 $ (spread capturé)
→ Gas merge = ~0.001 $ (négligeable)
→ P&L net = +1.00 $ + rewards du jour
```

### Cas particulier : fill unilatéral sur marché en mouvement

```python
# Ne pas merger si position directionnelle est profitable
if (fill_side == "BID") and (midpoint_current > fill_price * 1.05):
    # YES tokens ont pris +5% → vendre au marché plutôt que merger
    action = "SELL_LIMIT"  # ordre limite au nouveau mid, pas taker
elif (fill_side == "ASK") and (midpoint_current < fill_price * 0.95):
    # NO tokens ont pris de la valeur → vendre NO au marché
    action = "SELL_LIMIT"
else:
    action = "WAIT_OR_REQUOTE"
```

---

## 6. Logique de sortie permanente (compétitivité + hard filters)

### Principe

Les conditions de sortie s'appliquent **en continu sur tous les marchés actifs**, pas seulement à l'entrée. Un marché peut devenir non éligible après plusieurs jours de position.

### Sortie sur divergence de compétitivité

```python
# À l'entrée sur un marché : enregistrer la baseline
position_entry[condition_id] = {
    "reward_nette_estimee": rewardEpoch / (estimated_competitors + 1),
    "entry_time": now(),
}

# Monitoring toutes les heures
def check_competitiveness(condition_id):
    data = get("/rewards/user/markets")[condition_id]
    competitiveness = data["market_competitiveness"]  # 0.0 → 1.0
    reward_nette_reelle = data["rewardEpoch"] * (1 - competitiveness)
    reward_nette_estimee = position_entry[condition_id]["reward_nette_estimee"]

    degradation_ratio = reward_nette_reelle / reward_nette_estimee
    if degradation_ratio < 0.70:  # seuil : 30% de divergence
        log(f"[EXIT] Compétitivité dégradée sur {condition_id}: "
            f"{degradation_ratio:.0%} de l'espéré")
        exit_market_cleanly(condition_id)
```

**Seuil 0.70** : sortie si la reward nette réelle est inférieure à 70 % de l'estimée à l'entrée. Calibrable via `COMPETITIVENESS_EXIT_THRESHOLD` dans config.py.

**Check immédiat à l'entrée** : appeler `check_competitiveness()` quelques secondes après avoir posté les premiers ordres, avant tout risque d'autofill significatif sur un marché stable.

### Sortie sur violation de hard filter

```python
# Monitoring toutes les heures (même boucle que compétitivité)
def check_hard_filters(condition_id):
    market = get_market(condition_id)  # Gamma API

    violations = []
    if market["days_to_end"] <= 30:
        violations.append("days_to_end")
    if not (0.15 <= market["midpoint"] <= 0.85):
        violations.append("midpoint_out_of_range")
    if market["rewardEpoch"] == 0:
        violations.append("reward_epoch_zero")

    # reward_nette recalculée avec compétitivité live
    reward_nette = market["rewardEpoch"] * (1 - get_competitiveness(condition_id))
    if reward_nette < 1.0:
        violations.append("below_payout_threshold")

    if violations:
        log(f"[EXIT] Hard filter violé sur {condition_id}: {violations}")
        exit_market_cleanly(condition_id)  # tout ou rien, pas de sortie progressive
```

### Comportement de la blacklist

```python
BLACKLIST_RULES = {
    "duration_hours": 24,           # durée de blacklist par défaut
    "reason_exit_competitiveness": 24,
    "reason_exit_hard_filter": 48,  # plus long si hard filter (marché structurellement dégradé)
    "reason_stop_loss": 12,         # plus court si simple stop-loss (peut se rétablir)
}
```

---

## 7. Paramètres de configuration recommandés (point de départ)

```python
CONFIG = {
    # Capital
    "total_capital_usd": 1000,
    "max_markets_simultaneous": 5,
    "max_pct_per_market": 0.20,

    # Seuils de sélection (hard filters)
    "min_reward_epoch_usd": 20,           # pool journalier minimum
    "min_reward_nette_usd": 1.0,          # reward nette estimée minimum
    "min_days_to_end": 30,                # couvre aussi les catalyseurs type annonces Fed
    "midpoint_range": (0.15, 0.85),
    "max_volatility_24h": 0.10,           # 10 cents max de movement sur 24h

    # Sizing ordres
    "spread_target_pct_of_max": 0.30,     # placer à 30% du rewardsMaxSpread
    "size_multiplier_vs_min": 3.0,        # taille cible = minSize × 3

    # Gestion des fills
    "requote_threshold_cents": 1.0,       # requoter si mid bouge > 1 cent
    "stop_loss_threshold_cents": 3.0,     # stop-loss si mid bouge > 3 cents

    # Logique de sortie permanente
    "competitiveness_exit_threshold": 0.70,  # sortir si reward réelle < 70% de l'estimée
    "competitiveness_check_seconds": 3600,   # vérifier toutes les heures
    "hard_filter_check_seconds": 3600,       # même fréquence

    # Blacklist
    "blacklist_hours_competitiveness": 24,
    "blacklist_hours_hard_filter": 48,
    "blacklist_hours_stop_loss": 12,

    # Priorisation catégories (tag_id Gamma API)
    "category_priority": [
        {"tag_id": 100265, "label": "geopolitics", "fee_free": True},   # priorité 1
        {"tag_id": 2,      "label": "politics",    "fee_free": False},  # priorité 2
        {"tag_id": 120,    "label": "finance",     "fee_free": False},  # priorité 3
        # sports (100639) et crypto (21) exclus par défaut
    ],

    # Monitoring
    "scan_interval_seconds": 600,
}
```

---

## 8. Priorisation des catégories de marchés

### Tag IDs Gamma API (source : dépôt officiel Polymarket)

```python
CATEGORY_TAG_IDS = {
    "politics":    2,
    "finance":     120,
    "crypto":      21,
    "sports":      100639,
    "tech":        1401,
    "culture":     596,
    "geopolitics": 100265,
}

# Filtrage dans le scanner
url = f"https://gamma-api.polymarket.com/events?tag_id={tag_id}&related_tags=true&closed=false&order=rewardEpoch&ascending=false"
```

### Détection fee-free programmatique (source : docs.polymarket.com/trading)

```python
# Sur l'objet market retourné par Gamma ou CLOB
is_fee_free = not market.get("feesEnabled", False)
# feesEnabled = False → géopolitique/world events → fee-free
# feesEnabled = True  → toutes autres catégories → taker fees actives
```

### Tableau de priorisation

| Catégorie | tag_id | feesEnabled | Maker rebates | Durée typique | Volatilité | Priorité LP |
|-----------|--------|-------------|---------------|---------------|------------|-------------|
| Geopolitics | 100265 | False | Non (fee-free) | Mois–années | Très faible | **1 — base du portefeuille** |
| Politics | 2 | True | 25 % des fees | Mois–années | Faible | **2 — long-dated** |
| Finance | 120 | True | 50 % des fees | Semaines–mois | Modérée | **3 — rebates élevés** |
| Sports (pre-game) | 100639 | True | 25 % des fees | Jours | Modérée | 4 — si pool/compétition ratio bon |
| Sports (live) | 100639 | True | 25 % des fees | Heures | Très élevée | **Exclure** |
| Crypto 15 min | 21 | True | 20 % des fees | Minutes | Extrême | **Exclure** |

### Pourquoi pas de détection de catalyseurs économiques

Le filtre `days_to_end > 30` exclut mécaniquement les marchés de type "décision Fed du 12 juin" (endDate proche). Les marchés long-dated restants (ex: "Fed rate below 3% by end 2026") sont protégés par le stop-loss 3 cents et le filtre volatilité 24h. Détection par mots-clés ou calendrier économique externe : feature v2 optionnelle, pas nécessaire en v1.

---

---

## 9. Points d'attention critiques

### CLOB v2 (migration avril 2026)

Polymarket a migré vers CLOB v2 le 28 avril 2026. **Les anciens SDKs ne fonctionnent plus.**
- Utiliser `py-clob-client-v2` ou `polymarket-apis >= 2.1.0`
- Nouveau token de collateral : pUSD (pas USDC directement)
- Nouveaux contrats Exchange : CTF Exchange V2 + Neg Risk CTF Exchange V2
- Migration one-time requise pour les wallets existants

### Adverse selection — risque principal

Les ordres les plus proches du midpoint (meilleur score) sont aussi les premiers fillés quand le marché bouge. Il est impossible de maximiser le score ET minimiser l'adverse selection simultanément. Le `spread_target_pct_of_max` à 0.30 est un compromis empirique validé.

Marchés à éviter absolument :
- `endDate` < 30 jours (filtre hard, couvre aussi les catalyseurs imminents)
- Volatilité 24h > 10 cents
- Marchés sports en live (in-play)
- Crypto 15 min

### Seuil de paiement minimum

Si ta part journalière du pool d'un marché est < 1 $, **rien n'est versé**. Surveiller `/rewards/percentages` pour détecter les marchés où tu es trop dilué.

### market_competitiveness — interprétation

Le champ est disponible uniquement via `GET /rewards/user/markets` (authentifié). Il est calculé relativement à ta position dans le carnet, pas en absolu. Utiliser comme signal de monitoring post-entrée plutôt que de pré-sélection (pour la pré-sélection, utiliser la profondeur du carnet comme proxy public).

### Gas et frais

- Placer/annuler des ordres CLOB : **gasless** (meta-transactions, Polymarket paie)
- Split/Merge CTF : gas Polygon requis, < 0,01 $ par opération
- Maker fees : **zéro** sur tous les marchés
- Taker fees : 0,75–1,80 $ per 100 shares selon catégorie (à éviter — rester maker)
- Géopolitique/world events : zéro fees

---

## 10. Ressources et références

| Resource | URL | Usage |
|----------|-----|-------|
| Docs officielles | `https://docs.polymarket.com` | Référence API principale |
| API Reference complète | `https://docs.polymarket.com/api-reference/introduction` | Tous les endpoints |
| Rewards doc | `https://docs.polymarket.com/market-makers/liquidity-rewards` | Formule scoring |
| Merge doc | `https://docs.polymarket.com/trading/ctf/merge` | Mécanique merge |
| SDK Python | `https://pypi.org/project/polymarket-apis/` | `pip install polymarket-apis` |
| Bot open-source ref | `https://github.com/terrytrl100/polymarket-automated-mm` | Implémentation existante à forker |
| Bot original | `https://news.polymarket.com/p/automated-market-making-on-polymarket` | @defiance_cr |

---

## 11. Ce qui n'est PAS dans ce contexte (à rechercher si besoin)

- Authentification CLOB v2 : génération POLY_SIGNATURE (HMAC-SHA256 avec clé privée wallet)
- Gestion des Neg Risk markets (marchés multi-outcomes comme sport leagues)
- Stratégie cross-platform arbitrage avec Kalshi
- Logique airdrop POLY token (spéculatif, non confirmé)
- Gestion des 15-minute crypto markets (frais taker jusqu'à 1,80 % — différent des marchés standard)
- Détection de catalyseurs économiques via calendrier externe (feature v2 optionnelle — non nécessaire en v1)
