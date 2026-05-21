# Polymarket Bot S3+SP — Documentation complète

> **Code source** : https://github.com/TotoSeg/bot-polymarket (branche `phase/5-paper`)  
> Ce document permet à n'importe qui de configurer et faire tourner le bot en totale autonomie.

---

## 1. Ce que fait le bot

Le bot scanne en continu les marchés de prédiction Polymarket, identifie ceux où le prix YES est anormalement bas (5–35 %), parie sur NO (l'issue contraire), et laisse la résolution faire le bénéfice. Il tourne 24h/24 sur un VPS, indépendamment de tout PC.

**Backtesting sur 1,1 milliard de trades (2024–2025) :**
- Stratégie S3+SP combinée, mise composée, plafond 200$ : **$1 000 → $236 000 en 2 ans (+23 518 %)**

---

## 2. Les deux stratégies

### S3 — "Rien ne va se passer" ← prioritaire

| Paramètre | Valeur |
|-----------|--------|
| Filtre prix | YES entre 5 % et 10 % |
| Marchés ciblés | Tous sauf crypto et sport |
| Win rate historique | 97,7 % à 99,9 % selon le volume |
| ROI backtest 2024–25 | +1 495 % |

**Win rate dynamique selon le volume :**
- Volume > 20 000 $ → 99,9 %
- Volume 5 000–20 000 $ → 98,1 %
- Volume 1 000–5 000 $ → 97,7 %
- Volume < 1 000 $ → 99,2 %

**Bonus** : +1,5 pp si marché politique, +0,5 pp si tech, +1,5 pp si durée 30–90 jours, +0,5 pp si YES 6–7 % (sweetspot).

---

### SP — "Politique / géopolitique"

| Paramètre | Valeur |
|-----------|--------|
| Filtre prix | YES entre 5 % et 35 % |
| Marchés ciblés | Politique US, géopolitique, élections, guerres, diplomatie |
| Win rate historique | 92,2 % à 99,9 % selon le prix d'entrée |
| ROI backtest 2024–25 | +1 522 % |

**Win rate par tranche :**
- YES 5–10 % → 99,9 %
- YES 10–20 % → 99,2 %
- YES 20–35 % → 92,2 %

**Règle de priorité : S3 prime sur SP.** Si un marché déclenche les deux, seul S3 est pris. SP s'applique uniquement aux marchés dont le prix YES dépasse 10 % (hors portée de S3). Une seule position par marché.

---

## 3. Règles de sélection des marchés

| Règle | Critère |
|-------|---------|
| Prix YES | Entre 5 % et 35 % |
| Exclusions | Crypto, sport (filtres textuels sur la question) |
| Gain attendu minimum | EV ≥ 5 % — `win_rate × (yes/no) × 0,98 − (1−win_rate) ≥ 0,05` |
| Fenêtre de résolution | Résolution dans les **12 jours** maximum |
| Volume minimum | 500 $ |
| Unicité | Une seule position par marché (S3 prioritaire sur SP) |

**Ordre de priorité des catégories** (trié avant la date de résolution) :
1. Politique (congress, senate, president, white house…)
2. Géopolitique (nato, war, ceasefire, nuclear, invasion…)
3. Iran (iran, tehran, ayatollah, irgc, jcpoa…)
4. Élection (election, vote, ballot, primary, runoff…)
5. Culture (music, movie, oscar, grammy, netflix…)
6. Autre

---

## 4. Sizing des mises (Kelly)

```
mise = min(capital_total × Kelly × 0.25,  5% du capital,  MAX_BET_USDC,  capital_disponible)
```

| Paramètre | Valeur | Rôle |
|-----------|--------|------|
| `MAX_BET_USDC` | **200 $** | Plafond absolu par trade |
| `MAX_BET_PCT` | 5 % | Plafond en % du capital total |
| `KELLY_FRACTION` | 25 % | Fraction Kelly (conservateur) |
| `MIN_VOLUME_USD` | 500 $ | Volume minimum du marché |
| Minimum CLOB | 1 $ | En dessous → position non prise |

**Comportement clé** : si le capital disponible est inférieur à la taille Kelly, le bot mise tout le disponible (pas de skip). Skip uniquement si le résultat < 1 $ (minimum CLOB).

**Exemples concrets à 285 USDC :**
- S3 YES=7% → ~14 $ par trade
- SP YES=15% → ~11 $ par trade
- SP YES=30% → ~8 $ par trade

Les mises croissent automatiquement avec le capital (effet composé).

---

## 5. Cycle de trading (toutes les heures)

```
run_once()
  ├── 1. Clôture anticipée : YES ≤ 1% → vente tokens NO si liquidité OK
  ├── 2. Fermer les positions résolues → gains crédités au capital
  ├── 3. Lire le vrai solde USDC → détecter les dépôts externes
  ├── 4. Scanner ~2 800 marchés actifs (API Gamma Polymarket)
  ├── 5. Filtrer : prix 5-35%, hors crypto/sport, EV≥5%, résolution ≤12j
  ├── 6. Trier : catégorie → date de résolution → score
  ├── 7. Pour chaque candidat : Kelly size → liquidity check → ordre NO
  └── 8. Sauvegarder live_portfolio.json + afficher résumé P&L
```

**Entre chaque cycle :** reporting Notion à 30 min (mise à jour automatique du tableau de bord).

---

## 6. Clôture anticipée

Le bot peut clore une position **avant résolution** si :
- Le prix YES tombe à ≤ 1 % (certitude NO à 99 %)
- ET le carnet d'ordres a assez de bids pour vendre sans P&L négatif

Si la liquidité est insuffisante → le bot attend la résolution naturelle.

---

## 7. Mode cleanup

```bash
python src/phase6_bot/live_bot.py --cleanup
```

Ferme toutes les positions ouvertes qui satisfont **au moins une** condition :
- EV < 5 % (calculé sur le prix d'entrée)
- Résolution après le 31/05/2026
- Marché en retard (endDate dépassée mais toujours ouvert)

Tente une vente CLOB au meilleur prix disponible. Si aucun acheteur → affiche un message pour fermeture manuelle sur polymarket.com.

---

## 8. Reporting Notion

Tableau de bord automatique mis à jour toutes les 30 minutes.

**Colonnes à créer dans la base Notion (noms exacts) :**

| Colonne | Type Notion |
|---------|-------------|
| `Titre` | Title |
| `Résolution` | Date ← colonne de tri |
| `Taille ($)` | Number |
| `Mise ($)` | Number |
| `Gain espéré (%)` | Number |
| `Stratégie` | Select |
| `YES entrée` | Number |
| `market_id` | Text |

**Configuration Notion :**
1. https://www.notion.so/my-integrations → New integration → copier le token (`ntn_...`)
2. Créer la base avec les colonnes ci-dessus
3. Connecter l'intégration : `...` → Connections → Add connections
4. Copier l'ID de la base depuis l'URL (32 caractères avant le `?`)
5. Ajouter dans `.env` :
```
NOTION_TOKEN=ntn_XXXXX
NOTION_DATABASE_ID=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```
6. Configurer le tri par `Résolution` ascending dans la vue Notion (une seule fois)

---

## 9. Infrastructure

| Composant | Détail |
|-----------|--------|
| VPS | AWS Lightsail $5/mois, **Irlande (eu-west-1)** ← important, pas EU |
| OS | Ubuntu 22.04 LTS |
| Python | 3.10 (venv dans `~/bot-polymarket/.venv`) |
| RAM | 512 MB + **1 GB swap** (obligatoire) |
| Daemon | systemd, `Restart=always`, `RestartSec=60` |

⚠️ **L'Irlande est la seule région AWS européenne non bloquée par Polymarket.** Ne pas utiliser Frankfurt, Paris, Londres — tous géobloqués.

---

## 10. Pays géobloqués par Polymarket

> Australie, Belgique, Biélorussie, Burundi, Cuba, **France**, **Allemagne**, **Royaume-Uni**, **Italie**, **Pays-Bas**, Iran, Irak, Corée du Nord, Libye, Myanmar, Nicaragua, Russie, Somalie, Soudan, Syrie, **États-Unis**, Venezuela, Zimbabwe

**VPN requis pour** : accéder à polymarket.com depuis la France, déposer des fonds, générer les clés API. Choisir un serveur dans un pays non listé (Espagne, Portugal, Canada, Japon, etc.).

---

## 11. GUIDE COMPLET — Configurer le bot from scratch

### Étape A — Créer un wallet MetaMask

1. Installer l'extension MetaMask sur Chrome/Firefox : https://metamask.io
2. Créer un nouveau wallet → sauvegarder la phrase de récupération (12 mots) en lieu sûr
3. Ajouter le réseau **Polygon** : Settings → Networks → Add Network → rechercher "Polygon"
4. Noter l'adresse du wallet (commence par `0x...`, visible en haut de MetaMask)

---

### Étape B — Acheter et transférer des USDC

1. Sur Binance (ou autre exchange), acheter des **USDC**
2. Retirer vers MetaMask :
   - Réseau : **Polygon** (pas Ethereum — frais élevés)
   - Adresse : ton adresse MetaMask
   - Montant minimum recommandé : 100 USDC pour tester, 250–500 USDC pour commencer sérieusement
3. Attendre 2–5 minutes que les USDC apparaissent dans MetaMask (onglet Polygon)

---

### Étape C — Créer un compte Polymarket et déposer

⚠️ **VPN obligatoire depuis la France** (choisir un serveur en Espagne, Portugal, Canada...)

1. Activer le VPN sur ton **téléphone** (plus simple pour MetaMask mobile)
2. Ouvrir l'application MetaMask mobile → onglet **Navigateur** (icône en bas)
3. Aller sur `polymarket.com`
4. **Connect Wallet** → MetaMask → autoriser la connexion
5. Cliquer sur ton profil → **Deposit**
6. Entrer le montant USDC à déposer
7. Valider les **deux transactions** dans MetaMask (approve + deposit)
8. Vérifier que le solde apparaît sur ton profil Polymarket

> **Note technique** : Polymarket utilise un système pUSD (proxy wallet interne). L'adresse affichée sur Polymarket.com est différente de ton adresse MetaMask — c'est normal.

---

### Étape D — Générer les clés API Polymarket

**D1. Installer les dépendances Python** :
```bash
pip install py-clob-client-v2 eth-account
```

**D2. Exporter ta clé privée MetaMask** :
- MetaMask → Account Details → Show Private Key → entrer mot de passe
- Copier la clé (64 caractères hex)

**D3. Générer les clés API depuis le VPS** :
```bash
cd ~/bot-polymarket && source .venv/bin/activate
python src/phase6_bot/live_bot.py --create-keys
```

---

### Étape E — Créer le VPS (AWS Lightsail Irlande)

1. Aller sur https://lightsail.aws.amazon.com
2. **Create instance** :
   - Region : **EU (Ireland) eu-west-1** ← obligatoire
   - Platform : Linux/Unix
   - Blueprint : **Ubuntu 22.04 LTS**
   - Plan : **$5/month** (512 MB RAM)
3. Télécharger la clé SSH `.pem`
4. Dans l'onglet **Networking** : vérifier que le port 22 (SSH) est ouvert

---

### Étape F — Déployer le bot sur le VPS

**F1. Se connecter en SSH** :
```bash
ssh -i /chemin/vers/cle.pem ubuntu@IP_DU_VPS
```

**F2. Installer les prérequis** :
```bash
sudo apt-get update -q && sudo apt-get install -y python3.10-venv python3-pip git
```

**F3. Cloner le code** :
```bash
git clone -b phase/5-paper https://github.com/TotoSeg/bot-polymarket.git ~/bot-polymarket
cd ~/bot-polymarket && mkdir -p outputs/phase6 logs
```

**F4. Créer l'environnement Python** :
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip -q && pip install -r requirements-bot.txt -q
```

**F5. Créer le fichier `.env`** :
```bash
nano ~/bot-polymarket/src/phase6_bot/.env
```
Coller ce contenu (remplacer avec tes vraies valeurs) :
```
POLYMARKET_PRIVATE_KEY=0xTA_CLE_PRIVEE_64_CHARS
POLYMARKET_API_KEY=ta_api_key
POLYMARKET_API_SECRET=ton_api_secret
POLYMARKET_API_PASSPHRASE=ta_passphrase
POLYMARKET_PROXY_WALLET=0xTON_ADRESSE_PROXY
MAX_BET_USDC=200
MIN_VOLUME_USD=500
NOTION_TOKEN=ntn_XXXXX
NOTION_DATABASE_ID=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```
Sauvegarder : **Ctrl+O** → **Entrée** → **Ctrl+X**

**F6. Créer le portfolio initial** :
```bash
cat > ~/bot-polymarket/outputs/phase6/live_portfolio.json << 'EOF'
{
  "capital_initial": MONTANT_DEPOSE,
  "capital_disponible": MONTANT_DEPOSE,
  "total_depose": MONTANT_DEPOSE,
  "positions_ouvertes": {},
  "trades_clos": []
}
EOF
```
⚠️ Remplacer `MONTANT_DEPOSE` par le montant exact déposé (ex: `285.0`)

**F7. Installer le service systemd et le swap** :
```bash
sudo cp ~/bot-polymarket/infra/polymarket-bot.service /etc/systemd/system/
sudo sed -i "s|/home/ubuntu|$HOME|g" /etc/systemd/system/polymarket-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-bot
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

**F8. Vérifier que tout tourne** :
```bash
sudo systemctl status polymarket-bot --no-pager
```
→ doit afficher `active (running)`

---

### Étape G — Vérifier le premier cycle

Le bot attend 1 heure avant le premier cycle automatique. Pour déclencher immédiatement un cycle manuel :
```bash
cd ~/bot-polymarket && source .venv/bin/activate
python src/phase6_bot/live_bot.py
```

Vérifier les logs :
```bash
journalctl -u polymarket-bot -n 50 --no-pager
```

Lignes à chercher :
- `[ENTREE]` → ordre placé ✅
- `Candidats : X` → bot fonctionne ✅
- `Notion mis à jour` → reporting Notion actif ✅
- `ERROR 403 geoblock` → VPS dans un pays bloqué ❌

---

## 12. Surveillance quotidienne

```bash
# Connexion SSH
ssh -i /chemin/vers/cle.pem ubuntu@IP_VPS

# Statut service
sudo systemctl status polymarket-bot

# Logs temps réel
journalctl -u polymarket-bot -f

# Bilan P&L
cd ~/bot-polymarket && source .venv/bin/activate
python src/phase6_bot/live_bot.py --status

# Mise à jour code + redémarrage
cd ~/bot-polymarket && git pull origin phase/5-paper && sudo systemctl restart polymarket-bot

# Cycle immédiat (sans attendre l'heure)
python src/phase6_bot/live_bot.py

# Cleanup des positions hors critères
python src/phase6_bot/live_bot.py --cleanup

# Analyse des marchés disponibles par fenêtre de temps
python src/phase6_bot/analyze_windows.py
```

---

## 13. Ajouter des fonds

1. Transférer des USDC depuis Binance → MetaMask (Polygon)
2. Se connecter sur polymarket.com (avec VPN depuis France) → Deposit
3. Le bot détecte automatiquement le dépôt via la synchro du solde wallet au prochain cycle

---

## 14. Résumé P&L (affiché à chaque cycle)

```
  Total versé         :     285.00$    ← somme de tous les dépôts
  Capital total       :     312.50$    ← dispo + positions ouvertes
  Capital disponible  :     250.00$    ← USDC libres pour trader
  Capital engagé      :      62.50$    ← misé dans 5 positions
  P&L réalisé         :     +27.50$    ← gains nets trades clos
  ROI (sur versements):      +9.65%
  Trades clos         :          18
  Win rate            :      94.4%
```

---

## 15. Fichiers critiques sur le VPS

| Fichier | Rôle |
|---------|------|
| `~/bot-polymarket/src/phase6_bot/.env` | Clé privée + clés API + Notion (**JAMAIS sur GitHub**) |
| `~/bot-polymarket/outputs/phase6/live_portfolio.json` | Portefeuille (trades, P&L, capital) |
| `~/bot-polymarket/outputs/phase6/notion_page_ids.json` | Cache Notion (market_id → page_id) |
| `/etc/systemd/system/polymarket-bot.service` | Service daemon (relance auto) |
| `~/bot-polymarket/logs/` | Logs journaliers |
| `/swapfile` | Swap 1 GB (stabilité RAM) |

---

## 16. Transférer le bot à quelqu'un avec son propre wallet

1. **Suivre le Guide complet** (sections A à G) avec son propre wallet MetaMask
2. **Cloner le repo GitHub** : `https://github.com/TotoSeg/bot-polymarket` — code 100% public
3. **Générer ses propres clés API** Polymarket avec sa clé privée
4. **Créer son propre VPS** en Irlande
5. **NE PAS utiliser** les clés API ou la clé privée d'un autre wallet

---

## 17. Questions fréquentes

**Q : Le bot prend-il plusieurs positions sur le même marché ?**
R : Non. Une seule position par marché. Si S3 et SP se déclenchent tous les deux, seul S3 est pris.

**Q : Pourquoi le bot n'ouvre pas de nouvelles positions ?**
R : Soit le capital disponible est < 1$ (minimum CLOB), soit aucun marché ne satisfait les critères dans la fenêtre de 12 jours. Lancer `python src/phase6_bot/analyze_windows.py` pour diagnostiquer.

**Q : Les fonds arrivent-ils automatiquement après résolution d'un marché ?**
R : Oui. Polymarket crédite automatiquement les tokens gagnants. Le bot met à jour sa comptabilité au cycle suivant.

**Q : Pourquoi le solde USDC affiche parfois 0 dans les logs ?**
R : Polymarket utilise un proxy wallet pUSD. En cas d'échec de lecture, le bot continue avec le solde connu du portfolio JSON.

**Q : Le bot peut perdre de l'argent ?**
R : Oui. Les win rates (92–99%) sont des moyennes sur 2 ans. Sur 10 trades, 1–2 pertes sont possibles. Kelly limite chaque mise à 5% du capital.

**Q : VPN toujours nécessaire ?**
R : Uniquement pour accéder à polymarket.com depuis la France (dépôt, retrait, génération de clés API). Le bot tourne sur un VPS irlandais — pas de VPN sur le VPS.

**Q : Les clés API expirent ?**
R : Non. Si régénérées, les anciennes sont révoquées. Mettre à jour `.env` et redémarrer le service.

**Q : Le bot tourne si mon PC est éteint ?**
R : Oui — il tourne sur le VPS AWS 24h/24, totalement indépendant.
