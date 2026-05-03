# Polymarket Bot — Bilan pédagogique

## 1. Ce que fait le bot en 2 phrases

Le bot scanne en continu les marchés de prédiction Polymarket, identifie ceux où le prix YES est anormalement bas (5–35 %), parie sur NO (l'issue contraire), et laisse la résolution faire le bénéfice. Il utilise deux stratégies à fort win rate issues d'un backtesting sur 1,1 milliard de trades historiques.

---

## 2. Les deux stratégies

### S3 — "Ça ne va pas se passer"
- **Filtre** : prix YES entre 5 % et 10 %, marchés hors crypto
- **Logique** : si le marché cote YES à 5-10 %, il pense que l'événement a 5-10 % de chances. Historiquement, 99,7 % du temps, ces marchés résolvent NO → le bot gagne.
- **Mise** : Kelly fractionnaire, plafonné à 25 USDC/trade

### SP — "Politique/géopolitique"
- **Filtre** : prix YES entre 5 % et 35 %, marchés politiques ou géopolitiques
- **Logique** : les marchés politiques sur-estiment systématiquement les probabilités d'événements extrêmes. WR historique : 94,9 %.
- **Mise** : même sizing que S3

### Tri des candidats
Les marchés sont priorisés par **date de résolution la plus proche** (pas par score). L'objectif est de récupérer rapidement les gains pour les réinvestir (effet composé accéléré).

---

## 3. Architecture : qui tourne où

```
┌─────────────────────────────────────────┐     ┌──────────────────────────────┐
│  PC local (Claude Code / dev)           │     │  VPS AWS Lightsail           │
│  c:\...\Bot Polymarket\                 │     │  ubuntu@3.71.112.113         │
│                                         │     │                              │
│  • Développement, backtests             │     │  • Bot tourne 24/7           │
│  • Analyse DuckDB (107 GB parquet)      │ SCP │  • ~/bot-polymarket/         │
│  • gen_sig.py (génération clés API)     │────▶│  • systemd daemon            │
│  • Pas de clés privées commitées        │     │  • .env avec vrais secrets   │
└─────────────────────────────────────────┘     └──────────────────────────────┘
```

### Fichiers critiques sur le VPS (ne pas supprimer)

| Fichier | Rôle |
|---------|------|
| `~/bot-polymarket/src/phase6_bot/.env` | Clé privée + clés API (JAMAIS sur GitHub) |
| `~/bot-polymarket/outputs/phase6/live_portfolio.json` | Portefeuille live (trades, P&L) |
| `/etc/systemd/system/polymarket-bot.service` | Service systemd (relance auto) |
| `~/bot-polymarket/logs/` | Logs journaliers |

### Flux d'un cycle (toutes les heures)

```
run_once()
  ├── 1. Fermer les positions dont le marché est résolu
  ├── 2. Scanner ~2700 marchés actifs (API Gamma)
  ├── 3. Filtrer S3 + SP → trier par end_date ASC
  ├── 4. Pour chaque candidat : Kelly size → liquidity check → ordre NO
  └── 5. Sauvegarder live_portfolio.json + afficher résumé
```

---

## 4. Modules Python et leur rôle

```
src/
├── phase5_paper/
│   ├── polymarket_client.py   ← API Gamma : récupère les marchés en temps réel
│   ├── strategy_signals.py    ← Détecte S3 et SP sur chaque marché
│   └── paper_portfolio.py     ← Gestion du portefeuille + sizing Kelly
└── phase6_bot/
    ├── live_bot.py            ← Point d'entrée principal (le bot)
    ├── order_executor.py      ← Passe les ordres réels via CLOB Polymarket
    └── .env                   ← Secrets (non commité)
```

---

## 5. Paramètres de trading (actuels)

| Paramètre | Valeur | Rôle |
|-----------|--------|------|
| `INITIAL_CAPITAL_USDC` | 250 | Capital de départ |
| `MAX_BET_USDC` | 25 | Mise max par trade |
| `MIN_VOLUME_USD` | 500 | Volume min du marché pour entrer |
| `MAX_BET_PCT` | 5 % | Plafond Kelly en % du capital |
| Scan interval | 60 min | Fréquence du cycle |

---

## 6. Comment surveiller le bot depuis n'importe où

### Se connecter au VPS
```bash
# Depuis le PC local
ssh -i "C:\Users\ThomasSegond\Desktop\Claude Code\Bot Polymarket\clé SSH\LightsailDefaultKey-eu-central-1.pem" ubuntu@3.71.112.113
```

### Vérifier que le bot tourne
```bash
sudo systemctl status polymarket-bot
```

### Voir les logs en temps réel
```bash
bash ~/bot-polymarket/infra/monitor.sh
# ou directement :
journalctl -u polymarket-bot -f
```

### Voir le portefeuille
```bash
cd ~/bot-polymarket
source .venv/bin/activate
python src/phase6_bot/live_bot.py --status
```

### Arrêter / redémarrer le bot
```bash
sudo systemctl stop polymarket-bot    # arrêt
sudo systemctl start polymarket-bot   # démarrage
sudo systemctl restart polymarket-bot # redémarrage
```

---

## 7. Effacer toutes les traces de ce PC (PC Claude Code)

Les éléments sensibles ne sont **pas sur ce PC** (les clés sont uniquement sur le VPS dans `.env`). Mais pour supprimer proprement :

### 7a. Supprimer le dossier projet
```
C:\Users\ThomasSegond\Desktop\Claude Code\Bot Polymarket\
```
(Supprimer entièrement ce dossier)

### 7b. Supprimer la mémoire Claude
```
C:\Users\ThomasSegond\.claude\projects\c--Users-ThomasSegond-Desktop-Claude-Code-Bot-Polymarket\
```

### 7c. Supprimer la clé SSH Lightsail
```
C:\Users\ThomasSegond\Desktop\Claude Code\Bot Polymarket\clé SSH\LightsailDefaultKey-eu-central-1.pem
```
⚠️ Si vous supprimez la clé SSH sans en créer une nouvelle, vous perdez l'accès au VPS.

### 7d. Vider l'historique du terminal (PowerShell)
```powershell
Remove-Item (Get-PSReadlineOption).HistorySavePath
```

### 7e. Ce qui reste en dehors de ce PC
- VPS AWS Lightsail (3.71.112.113) : tourne de façon autonome
- Wallet Polygon : clé privée dans le .env du VPS uniquement
- Clés API Polymarket : dans le .env du VPS uniquement
- GitHub (TotoSeg/bot-polymarket) : code source, sans secrets

---

## 8. Transférer le bot à quelqu'un d'autre

### Étape 1 : Transférer le VPS
Option A — Donner l'accès SSH :
1. Créer une nouvelle paire de clés sur AWS Lightsail (Console → Networking → SSH Keys)
2. Ajouter la clé publique sur le VPS : `echo "NOUVELLE_CLE_PUBLIQUE" >> ~/.ssh/authorized_keys`
3. Envoyer le fichier `.pem` à la nouvelle personne

Option B — Transférer l'instance AWS :
- La facturation AWS Lightsail est liée à un compte Amazon
- Créer un snapshot de l'instance, partager avec le compte destination

### Étape 2 : Transférer les secrets
Sur le VPS, afficher les clés :
```bash
cat ~/bot-polymarket/src/phase6_bot/.env
```
Transmettre ces 4 éléments de façon sécurisée :
- `POLYMARKET_PRIVATE_KEY` — clé du wallet (⚠️ donne accès aux fonds !)
- `POLYMARKET_API_KEY`
- `POLYMARKET_API_SECRET`
- `POLYMARKET_API_PASSPHRASE`

### Étape 3 : Transférer le code source
Si le repo GitHub est privé, donner accès à TotoSeg/bot-polymarket dans les settings GitHub.

### Étape 4 : Informer la nouvelle personne
- L'adresse du wallet Polygon (pour y déposer/retirer des USDC)
- L'adresse du VPS : 3.71.112.113
- Ce document BILAN_BOT.md

---

## 9. Questions fréquentes

**Q : Le bot peut perdre de l'argent ?**
R : Oui. Les win rates (99,7 % S3, 94,9 % SP) sont des moyennes sur 2 ans. Sur 10 trades, il peut y avoir 1-2 pertes. Le sizing Kelly limite le risque par trade.

**Q : Où voir les profits/pertes ?**
R : Dans `outputs/phase6/live_portfolio.json` sur le VPS, ou via `--status`.

**Q : Les clés API expirent ?**
R : Non, mais si vous régénérez des clés, les anciennes sont révoquées. Mettre à jour `.env` et redémarrer le service.

**Q : Que faire si le bot plante ?**
R : `systemd` le redémarre automatiquement après 60 secondes. Vérifier `journalctl -u polymarket-bot -n 50` pour voir l'erreur.

**Q : Comment augmenter le capital ?**
R : Déposer des USDC supplémentaires sur le wallet Polygon, puis mettre à jour `INITIAL_CAPITAL_USDC` dans `.env` et redémarrer.
