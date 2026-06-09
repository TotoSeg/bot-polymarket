# polymarket_common/ — Module partagé entre les deux bots

> **Usage Claude Code** : ce fichier décrit le module `polymarket_common/` partagé entre `bot_directional/` et `bot_lp/`. Créer ce module EN PREMIER, avant de toucher aux deux bots. Toute modification d'une fonction commune (auth, utils, types) se fait ICI UNIQUEMENT et se répercute automatiquement sur les deux bots.

---

## Structure

```
vps/
├── polymarket_common/
│   ├── __init__.py
│   ├── client.py        ← instanciation ClobClient (EOA, signature_type=0)
│   ├── gamma.py         ← appels Gamma API (public, sans auth)
│   ├── types.py         ← dataclasses partagées (Position, Order, MarketInfo)
│   ├── logger.py        ← configuration logging commune
│   └── allowances.py    ← approbations one-time des contrats Polymarket
│
├── bot_directional/     ← importe depuis polymarket_common
│   └── main.py
│
└── bot_lp/              ← importe depuis polymarket_common
    └── main.py
```

---

## client.py — Connexion CLOB pour wallet MetaMask EOA

### Contexte critique : deux SDKs coexistent en 2026

- `py-clob-client` (v0.34.6, archivé mai 2026) — SDK historique, toujours fonctionnel pour les wallets EOA MetaMask avec `signature_type=0`. **C'est celui à utiliser pour un nouveau wallet MetaMask.**
- `py-clob-client-v2` — SDK post-migration CLOB v2 (avril 2026), conçu pour les deposit wallets (email/Magic). **Bug actif non résolu (mai 2026) : `create_or_derive_api_key()` lie la clé à l'EOA et non au deposit wallet pour `signature_type=3`.** Ne pas utiliser pour un wallet MetaMask EOA classique.

**→ Pour un nouveau wallet MetaMask : utiliser `py-clob-client` avec `signature_type=0`.**

### Procédure de setup d'un nouveau wallet (à faire une seule fois manuellement)

```
ÉTAPE 1 — Créer le wallet MetaMask
  → Ouvrir MetaMask → Create new wallet
  → Sauvegarder la seed phrase hors ligne
  → Basculer sur le réseau Polygon (Chain ID 137)
  → Exporter la clé privée : Settings > Account Details > Export Private Key
  → Stocker dans .env sur le VPS : LP_PRIVATE_KEY=0x...

ÉTAPE 2 — Approvisionner en POL (gas)
  → POL requis pour les transactions on-chain (split/merge CTF, allowances)
  → Montant recommandé : 2–5 $ de POL (suffit pour des centaines de tx)
  → Méthode la moins chère : acheter POL sur un exchange (Binance, Kraken)
    et retirer directement sur Polygon (pas Ethereum mainnet)
  → Vérifier réception : polygonscan.com → coller l'adresse du wallet

ÉTAPE 3 — Approvisionner en pUSD
  → Voir section "Transfert de fonds" ci-dessous

ÉTAPE 4 — Générer les credentials CLOB API (script one-time)
  → Voir script setup_credentials.py ci-dessous

ÉTAPE 5 — Approuver les contrats Polymarket
  → Voir allowances.py ci-dessous
  → À faire une seule fois par wallet
```

### setup_credentials.py — Script one-time (à lancer une fois par wallet)

```python
"""
Script one-time : génère et sauvegarde les credentials CLOB API
pour un wallet MetaMask EOA (signature_type=0).
Lancer une seule fois, sauvegarder le résultat dans .env
"""
import os
from py_clob_client.client import ClobClient

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.environ["LP_PRIVATE_KEY"]  # clé privée du nouveau wallet LP

# Instancier avec signature_type=0 (EOA MetaMask standard)
client = ClobClient(
    HOST,
    key=PRIVATE_KEY,
    chain_id=CHAIN_ID,
    signature_type=0,  # EOA — pas de funder address pour MetaMask direct
)

# Générer ou retrouver les credentials existants
creds = client.create_or_derive_api_creds()

print("=== SAUVEGARDER CES VALEURS DANS .env ===")
print(f"LP_POLY_API_KEY={creds.api_key}")
print(f"LP_POLY_API_SECRET={creds.api_secret}")
print(f"LP_POLY_API_PASSPHRASE={creds.api_passphrase}")
print(f"LP_POLY_ADDRESS={client.get_address()}")
print("==========================================")
```

### client.py — Module partagé

```python
"""
polymarket_common/client.py
Instanciation du ClobClient. Importé par bot_directional et bot_lp.
Chaque bot passe ses propres credentials — un client par wallet.
"""
import os
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137


def build_client(
    private_key: str,
    api_key: str,
    api_secret: str,
    api_passphrase: str,
) -> ClobClient:
    """
    Construit un ClobClient authentifié pour un wallet EOA MetaMask.
    signature_type=0 : standard EIP-712, pas de proxy/funder.
    """
    creds = ApiCreds(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
    )
    client = ClobClient(
        HOST,
        key=private_key,
        chain_id=CHAIN_ID,
        creds=creds,
        signature_type=0,
    )
    return client


def build_directional_client() -> ClobClient:
    """Client pour le bot directionnel — lit les variables DIRECTIONAL_* du .env"""
    return build_client(
        private_key=os.environ["DIRECTIONAL_PRIVATE_KEY"],
        api_key=os.environ["DIRECTIONAL_POLY_API_KEY"],
        api_secret=os.environ["DIRECTIONAL_POLY_API_SECRET"],
        api_passphrase=os.environ["DIRECTIONAL_POLY_API_PASSPHRASE"],
    )


def build_lp_client() -> ClobClient:
    """Client pour le bot LP — lit les variables LP_* du .env"""
    return build_client(
        private_key=os.environ["LP_PRIVATE_KEY"],
        api_key=os.environ["LP_POLY_API_KEY"],
        api_secret=os.environ["LP_POLY_API_SECRET"],
        api_passphrase=os.environ["LP_POLY_API_PASSPHRASE"],
    )
```

### .env — Variables d'environnement (template)

```bash
# Bot directionnel — wallet existant
DIRECTIONAL_PRIVATE_KEY=0x...
DIRECTIONAL_POLY_API_KEY=...
DIRECTIONAL_POLY_API_SECRET=...
DIRECTIONAL_POLY_API_PASSPHRASE=...
DIRECTIONAL_POLY_ADDRESS=0x...

# Bot LP — nouveau wallet MetaMask
LP_PRIVATE_KEY=0x...
LP_POLY_API_KEY=...          # généré par setup_credentials.py
LP_POLY_API_SECRET=...
LP_POLY_API_PASSPHRASE=...
LP_POLY_ADDRESS=0x...
```

---

## allowances.py — Approbations one-time (EOA MetaMask uniquement)

Les wallets MetaMask EOA doivent approuver les contrats Polymarket une fois avant de trader. Les wallets proxy (Gnosis Safe) n'ont pas besoin de cette étape.

```python
"""
polymarket_common/allowances.py
Approuve les contrats Polymarket pour un wallet EOA.
À appeler une seule fois après setup_credentials.py.
Ne rien changer ici — la logique est dans py_clob_client.
"""
from py_clob_client.client import ClobClient


def set_allowances(client: ClobClient) -> None:
    """
    Approuve USDC + tokens conditionnels pour les contrats exchange.
    Coût : ~0.05 $ de gas Polygon. Une seule fois par wallet.
    """
    # Approbation USDC/pUSD pour CTF Exchange
    resp = client.set_allowance(asset_type="COLLATERAL")
    print(f"Allowance COLLATERAL : {resp}")

    # Approbation tokens conditionnels (YES/NO) pour CTF Exchange
    resp = client.set_allowance(asset_type="CONDITIONAL")
    print(f"Allowance CONDITIONAL : {resp}")

    print("Allowances configurées. Ne relancer cette fonction qu'en cas de reset.")


if __name__ == "__main__":
    # Usage : python -m polymarket_common.allowances lp
    import sys
    from polymarket_common.client import build_lp_client, build_directional_client

    target = sys.argv[1] if len(sys.argv) > 1 else "lp"
    client = build_lp_client() if target == "lp" else build_directional_client()
    set_allowances(client)
```

---

## gamma.py — Appels Gamma API partagés

```python
"""
polymarket_common/gamma.py
Toutes les fonctions de lecture Gamma API (public, sans auth).
Partagé entre scanner LP et toute lecture de marchés dans le directionnel.
"""
import requests
from datetime import datetime, timezone
from typing import Optional

GAMMA_BASE = "https://gamma-api.polymarket.com"

CATEGORY_TAG_IDS = {
    "geopolitics": 100265,
    "politics": 2,
    "finance": 120,
    "crypto": 21,
    "sports": 100639,
    "tech": 1401,
    "culture": 596,
}


def get_active_markets(
    tag_id: Optional[int] = None,
    order_by: str = "rewardEpoch",
    limit: int = 100,
) -> list[dict]:
    """Retourne les marchés actifs, triés par ordre décroissant de rewardEpoch par défaut."""
    params = {
        "active": "true",
        "closed": "false",
        "order": order_by,
        "ascending": "false",
        "limit": limit,
    }
    if tag_id:
        params["tag_id"] = tag_id
        params["related_tags"] = "true"

    resp = requests.get(f"{GAMMA_BASE}/markets", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_market(condition_id: str) -> dict:
    """Retourne le détail d'un marché par condition_id."""
    resp = requests.get(f"{GAMMA_BASE}/markets/{condition_id}", timeout=10)
    resp.raise_for_status()
    return resp.json()


def days_to_end(market: dict) -> float:
    """Calcule le nombre de jours restants avant résolution."""
    end = datetime.fromisoformat(market["endDate"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    return (end - now).total_seconds() / 86400


def is_fee_free(market: dict) -> bool:
    """True si le marché est en catégorie fee-free (géopolitique/world events)."""
    return not market.get("feesEnabled", False)
```

---

## types.py — Dataclasses partagées

```python
"""
polymarket_common/types.py
Types partagés entre bot_directional et bot_lp.
Ne pas modifier sans impacter les deux bots.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class MarketInfo:
    condition_id: str
    slug: str
    question: str
    end_date: datetime
    midpoint: float
    spread: float
    reward_epoch: float          # pool journalier ($)
    rewards_min_size: float
    rewards_max_spread: float
    volume_24hr: float
    liquidity: float
    yes_token_id: str
    no_token_id: str
    fees_enabled: bool


@dataclass
class LPPosition:
    condition_id: str
    yes_token_id: str
    no_token_id: str
    bid_order_id: Optional[str]
    ask_order_id: Optional[str]
    bid_price: float
    ask_price: float
    size_per_side: float
    entry_reward_nette_estimee: float   # baseline pour calcul divergence
    entry_time: datetime
    competitiveness_last_check: Optional[datetime] = None


@dataclass
class DirectionalPosition:
    condition_id: str
    token_id: str
    side: str                    # "YES" or "NO"
    entry_price: float
    size: float
    order_id: Optional[str]
    entry_time: datetime
```

---

## logger.py — Configuration logging commune

```python
"""
polymarket_common/logger.py
Configuration du logger partagé. Chaque bot utilise un namespace distinct
([LP] ou [DIR]) mais écrit dans les mêmes handlers.
"""
import logging
import sys
from pathlib import Path


def setup_logger(
    name: str,
    log_file: str = "/var/log/polymarket/bot.log",
    level: int = logging.INFO,
) -> logging.Logger:
    """
    Configure un logger avec handler fichier + stdout.
    Usage :
        from polymarket_common.logger import setup_logger
        logger = setup_logger("lp.scanner")   # préfixe [LP]
        logger = setup_logger("dir.main")     # préfixe [DIR]
    """
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)-20s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    logger = logging.getLogger(name)
    logger.setLevel(level)

    if not logger.handlers:
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    return logger
```

---

## Transfert de fonds entre wallets Polygon

### Contexte

Les deux wallets (directionnel et LP) sont sur Polygon. Le transfert se fait directement sur Polygon, sans passer par Ethereum mainnet.

**Trois cas de figure :**

### Cas 1 — Le wallet directionnel a déjà du pUSD/USDC sur Polygon → transfert direct

pUSD est un ERC-20 sur Polygon. On le transfère comme n'importe quel token.

```
1. Ouvrir MetaMask sur le wallet DIRECTIONAL
2. Basculer sur le réseau Polygon (Chain ID 137)
3. Assets → "Import token" → coller l'adresse du contrat pUSD :
   0x4bB6b2DF79d7E39dE06cA09A6Ff65C93Bd63D66  (vérifier sur docs.polymarket.com)
4. Send → coller l'adresse du wallet LP → entrer le montant
5. Gas : quelques centimes de POL
6. Confirmer la transaction

Vérification : polygonscan.com → adresse wallet LP → onglet "Token Transfers"
```

### Cas 2 — Approvisionner depuis un exchange (méthode la moins chère)

La méthode la moins chère est d'acheter de l'USDC sur Coinbase, Kraken, ou n'importe quel exchange majeur qui supporte les retraits Polygon, puis de retirer directement vers le wallet MetaMask sur le réseau Polygon.

```
1. Sur l'exchange : Withdraw → USDC → réseau Polygon (pas Ethereum !)
2. Adresse de destination : adresse du wallet LP MetaMask
3. Coût typique : 0,50–2,00 $ de frais de retrait exchange
4. Réception : quelques minutes

Une fois l'USDC reçu sur Polygon, Polymarket le convertit automatiquement
en pUSD lors du premier dépôt via l'interface polymarket.com.
```

### Cas 3 — Pas d'USDC sur Polygon : bridger depuis une autre chain

L'interface de Circle (CCTP) est le bridge canonique. Wormhole, LI.FI, Jumper et Squid exposent tous CCTP comme option de routing. Le flux est identique sur tous : connecter un wallet contenant de l'USDC sur la chain source, sélectionner Polygon comme destination, approuver l'USDC pour le contrat CCTP, confirmer le burn, attendre l'attestation Circle, et récupérer sur Polygon.

```
Bridge recommandé : app.li.fi ou jumper.exchange
Chain source → Polygon, token USDC
Délai V2 Fast Transfer : 8–20 secondes
```

### Rappel gas POL pour le nouveau wallet LP

POL est requis pour payer le gas sur Polygon. Environ 1–3 $ de POL suffit pour des centaines de transactions. Sans POL, les transactions échouent même avec du pUSD.

```
Envoyer ~2 $ de POL depuis le wallet directionnel vers le wallet LP
(ou acheter sur un exchange et retirer sur Polygon)
AVANT de déposer le pUSD — sinon les allowances ne peuvent pas être approuvées.
```

---

## Ordre d'installation sur le VPS

```bash
# 1. Créer la structure de répertoires
mkdir -p /opt/polymarket/{polymarket_common,bot_directional,bot_lp}
cd /opt/polymarket

# 2. Installer les dépendances partagées
pip install py-clob-client==0.34.6  # version stable EOA, pas v2
pip install requests python-dotenv

# 3. Créer le .env
cp .env.template .env
# Remplir les variables DIRECTIONAL_* avec les credentials existants
# Remplir les variables LP_* après avoir lancé setup_credentials.py

# 4. Lancer setup_credentials.py pour le nouveau wallet LP
python -m polymarket_common.setup_credentials
# → copier les valeurs affichées dans .env

# 5. Lancer les allowances pour le wallet LP
python -m polymarket_common.allowances lp

# 6. Migrer le bot directionnel existant pour importer depuis polymarket_common
# → remplacer les imports d'auth par : from polymarket_common.client import build_directional_client

# 7. Créer bot_lp/ en suivant polymarket_lp_bot_context.md
```

---

## Règle de maintenance

Toute modification qui touche à l'authentification, aux URLs d'API, aux types de données, ou au format des logs doit être faite dans `polymarket_common/` uniquement. Ne jamais dupliquer ces fonctions dans `bot_directional/` ou `bot_lp/`.
