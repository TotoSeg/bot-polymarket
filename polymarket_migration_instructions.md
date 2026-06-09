# Instructions de migration — Architecture deux bots + module commun

> **Usage Claude Code** : lire dans cet ordre OBLIGATOIRE : `polymarket_common_setup.md` → ce fichier → `polymarket_lp_bot_context.md`. Ce fichier a priorité sur le contexte LP pour toutes les décisions d'architecture.

---

## Contexte de départ

- Bot existant : script Python **monolithique**, directionnel (paris sur outcomes)
- Connecté : **CLOB API authentifiée** (wallet MetaMask dédié, VPS Ireland)
- Instrumentation : **logs structurés + gestion d'état + tracking P&L** — à conserver
- Objectif : deux bots **indépendants** (deux wallets, deux processus, deux scripts) partageant `polymarket_common/`

---

## Architecture cible

```
vps/
├── polymarket_common/   ← code partagé (auth, gamma, types, logger)
├── bot_directional/     ← bot existant, refactorisé minimalement
│   ├── main.py
│   └── ...
└── bot_lp/              ← nouveau bot LP autonome
    ├── main.py
    ├── scanner.py
    ├── positions.py
    ├── exits.py
    └── monitor.py
```

Deux services `systemd` distincts. Deux wallets distincts. Capital non partagé.

---

## Principe directeur : ne pas réécrire, extraire

Le bot directionnel existant a une connexion CLOB authentifiée qui fonctionne, une gestion d'état éprouvée, et des logs structurés. Ce sont des actifs.

La migration se fait en deux phases :
1. **Extraire** le code d'auth et les utils du bot directionnel vers `polymarket_common/`
2. **Construire** `bot_lp/` en important depuis `polymarket_common/`

La phase 1 touche au bot directionnel mais uniquement pour extraire du code existant — pas de logique modifiée, pas de comportement changé.

---

## Étape 1 — Audit du code existant (OBLIGATOIRE avant tout)

Avant d'écrire une seule ligne, Claude Code doit lire et documenter :

```
LIRE et résumer dans un commentaire :
1. Auth CLOB
   → Comment sont stockées les clés ? (.env, fichier, hardcodées ?)
   → Quel SDK ? (py-clob-client, polymarket-apis, custom ?)
   → Client instancié globalement ou par appel ?

2. Structure de l'état
   → Format des positions ? (dict, JSON, DB ?)
   → Où est persisté l'état ? (fichier, mémoire seule ?)

3. Système de logs
   → Quel logger ? (logging stdlib, loguru, custom ?)
   → Format et destination des logs ?

4. Boucle principale
   → Async (asyncio) ou synchrone ?
   → Scheduler / interval déjà en place ?
   → WebSocket déjà ouvert ?

5. Fonctions d'ordres
   → Fonctions existantes pour placer / annuler des ordres ?
```

**Répondre à ces 5 points avant de toucher au code.**

---

## Étape 2 — Extraire vers polymarket_common/

Déplacer depuis le bot directionnel existant vers `polymarket_common/` :

### 2a. Auth → polymarket_common/client.py

```python
# Avant (dans bot_directional/main.py) — exemple hypothétique
client = ClobClient(HOST, key=PK, chain_id=137, creds=creds, signature_type=0)

# Après — extraire dans polymarket_common/client.py
# et remplacer dans bot_directional/main.py par :
from polymarket_common.client import build_directional_client
client = build_directional_client()
```

Le code de `client.py` est documenté dans `polymarket_common_setup.md`.

### 2b. Logger → polymarket_common/logger.py

```python
# Dans bot_directional/main.py — remplacer la config logging existante par :
from polymarket_common.logger import setup_logger
logger = setup_logger("dir.main", log_file="/var/log/polymarket/directional.log")
```

Si le bot existant utilise loguru ou un logger custom, adapter `logger.py` en conséquence — ne pas changer le format des logs existants, juste le centraliser.

### 2c. Types → polymarket_common/types.py

Identifier la dataclass ou le dict qui représente une position directionnelle et la migrer dans `types.py` sous `DirectionalPosition`. Voir la définition dans `polymarket_common_setup.md`.

### 2d. Appels Gamma → polymarket_common/gamma.py

Si le bot directionnel fait des appels Gamma API, les migrer dans `gamma.py`. Sinon, ignorer — `gamma.py` sera utilisé uniquement par `bot_lp/`.

---

## Étape 3 — Vérifier que bot_directional fonctionne toujours

Avant de créer `bot_lp/` :

```bash
# Relancer le bot directionnel après migration vers polymarket_common
python bot_directional/main.py

# Vérifier :
# - Les logs s'écrivent au bon endroit avec le bon format
# - L'état se charge et se sauvegarde correctement
# - Un ordre test peut être placé et annulé
# - Comportement identique à avant la migration
```

**Ne pas avancer vers l'étape 4 si le bot directionnel a le moindre comportement modifié.**

---

## Étape 4 — Créer bot_lp/

Implémenter `bot_lp/` en suivant `polymarket_lp_bot_context.md`, dans cet ordre :

```
[1] bot_lp/state.py
    → Structure d'état LP (positions, blacklist, baselines)
    → Persistence indépendante du bot directionnel
    → Format : JSON dans /var/lib/polymarket/lp_state.json

[2] bot_lp/scanner.py
    → Importer depuis polymarket_common.gamma
    → Hard filters + soft filters + scoring
    → Test : afficher top-5 marchés sans placer d'ordres

[3] bot_lp/positions.py
    → Importer depuis polymarket_common.client (build_lp_client)
    → split_position(), place_lp_orders(), cancel_lp_orders()
    → Test en dry-run avant exécution réelle

[4] bot_lp/exits.py
    → exit_market_cleanly() (séquence complète documentée dans lp_bot_context.md)
    → check_competitiveness(), check_hard_filters()

[5] bot_lp/monitor.py
    → WebSocket indépendant (pas partagé avec bot_directional)
    → Requoting + gestion des fills

[6] bot_lp/main.py
    → Orchestration des 4 boucles
    → Logger : setup_logger("lp.main", log_file="/var/log/polymarket/lp.log")
```

---

## Étape 5 — Services systemd

```ini
# /etc/systemd/system/polymarket-directional.service
[Unit]
Description=Polymarket Directional Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/polymarket
EnvironmentFile=/opt/polymarket/.env
ExecStart=/usr/bin/python3 bot_directional/main.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/polymarket-lp.service
[Unit]
Description=Polymarket LP Farming Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/polymarket
EnvironmentFile=/opt/polymarket/.env
ExecStart=/usr/bin/python3 bot_lp/main.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable polymarket-directional polymarket-lp
systemctl start polymarket-directional
systemctl start polymarket-lp

# Vérifier les logs
journalctl -u polymarket-lp -f
tail -f /var/log/polymarket/lp.log
```

---

## Ce que Claude Code NE DOIT PAS faire

```
✗ Modifier la logique directionnelle existante
✗ Changer le format des logs existants
✗ Créer un client CLOB commun partagé entre les deux bots
  (chaque bot a son propre client, son propre wallet)
✗ Partager l'état entre les deux bots
✗ Refactoriser "pendant qu'on y est" — hors scope
✗ Implémenter les étapes 2–5 avant d'avoir audité l'étape 1
✗ Modifier polymarket_common/ sans vérifier l'impact sur bot_directional
```

---

## Checklist de mise en production

```
□ Audit étape 1 documenté
□ polymarket_common/ créé et testé en isolation
□ bot_directional fonctionne identiquement après migration vers polymarket_common
□ Nouveau wallet LP approvisionné (POL + pUSD)
□ setup_credentials.py lancé, credentials LP dans .env
□ allowances.py lancé pour le wallet LP
□ bot_lp/ testé en dry-run (scanner OK, ordres simulés OK)
□ Deux services systemd actifs et en Restart=on-failure
□ Logs distincts : /var/log/polymarket/directional.log et lp.log
```
