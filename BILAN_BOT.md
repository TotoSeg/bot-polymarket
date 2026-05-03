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

### S3 — "Rien ne va se passer"

| Paramètre | Valeur |
|-----------|--------|
| Filtre prix | YES entre 5 % et 10 % |
| Marchés ciblés | Tous sauf crypto |
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

**Mots-clés détectés :**
`trump, biden, harris, election, congress, senate, president, democrat, republican, white house, governor, parliament, prime minister, chancellor, ceasefire, nato, sanction, coup, invasion, war, treaty, nuclear, troops`

**Un marché politique à YES=7 % déclenche S3 ET SP simultanément.**

---

## 3. Sizing des mises (Kelly)

```
mise = min(capital_total × Kelly × 0.25,  5% du capital,  MAX_BET_USDC)
```

| Paramètre | Valeur | Rôle |
|-----------|--------|------|
| `MAX_BET_USDC` | **200 $** | Plafond absolu par trade (atteint quand capital > 4 000 $) |
| `MAX_BET_PCT` | 5 % | Plafond en % du capital — contrainte réelle sous 4 000 $ |
| `KELLY_FRACTION` | 25 % | Fraction Kelly utilisée (conservateur) |
| `MIN_VOLUME_USD` | 500 $ | Volume minimum du marché |

**Exemples concrets à 285 USDC :**
- S3 YES=7% → ~14 $ par trade
- SP YES=15% → ~11 $ par trade
- SP YES=30% → ~8 $ par trade

Les mises croissent automatiquement avec le capital (effet composé).

---

## 4. Cycle de trading (toutes les heures)

```
run_once()
  ├── 1. Fermer les positions résolues → gains crédités au capital
  ├── 2. Lire le vrai solde USDC → détecter les dépôts externes
  ├── 3. Calculer capital_kelly = dispo + engagé dans les positions
  ├── 4. Scanner ~2 700 marchés actifs (API Gamma Polymarket)
  ├── 5. Filtrer S3 + SP (hors crypto, prix 5-35%)
  ├── 6. Trier par date de résolution la plus proche (effet composé max)
  ├── 7. Pour chaque candidat : Kelly size → liquidity check → ordre NO
  └── 8. Sauvegarder live_portfolio.json + afficher résumé P&L
```

---

## 5. Infrastructure

| Composant | Détail |
|-----------|--------|
| VPS | AWS Lightsail $5/mois, **Irlande (eu-west-1)** ← important, pas EU |
| OS | Ubuntu 22.04 LTS |
| Python | 3.10 (venv dans `~/bot-polymarket/.venv`) |
| RAM | 512 MB + **1 GB swap** (obligatoire) |
| Daemon | systemd, `Restart=always`, `RestartSec=60` |

⚠️ **L'Irlande est la seule région AWS européenne non bloquée par Polymarket.** Ne pas utiliser Frankfurt (Allemagne), Paris (France), Londres (UK) — tous géobloqués.

---

## 6. Pays géobloqués par Polymarket

Polymarket refuse les connexions depuis ces pays (trading impossible même avec VPN depuis ces IP) :

> Australie, Belgique, Biélorussie, Burundi, Cuba, **France**, **Allemagne**, **Royaume-Uni**, **Italie**, **Pays-Bas**, Iran, Irak, Corée du Nord, Libye, Myanmar, Nicaragua, Russie, Somalie, Soudan, Syrie, **États-Unis**, Venezuela, Zimbabwe

**VPN requis pour** : accéder à polymarket.com depuis la France, déposer des fonds, générer les clés API. Choisir un serveur dans un pays non listé (Espagne, Portugal, Canada, Japon, etc.).

---

## 7. GUIDE COMPLET — Configurer le bot from scratch

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

Les clés API permettent au bot de passer des ordres en ton nom.

**D1. Installer les dépendances Python** (sur ton PC local ou le futur VPS) :
```bash
pip install py-clob-client eth-account py-order-utils poly-eip712-structs
```

**D2. Exporter ta clé privée MetaMask** :
- MetaMask → Account Details → Show Private Key → entrer mot de passe
- Copier la clé (64 caractères hex)

**D3. Générer la signature EIP-712** :
```bash
# Sur ton PC, dans le dossier du projet :
BOT_PK=0xTA_CLE_PRIVEE python gen_sig.py
```
Le script affiche une commande `fetch(...)` à copier.

**D4. Exécuter le fetch dans le navigateur** :
1. Activer le VPN (pays non bloqué)
2. Aller sur `polymarket.com` dans Firefox/Chrome
3. Ouvrir la console développeur : **F12** → onglet **Console**
4. Coller et exécuter la commande `fetch(...)` générée à l'étape D3
5. La console retourne :
```json
{"apiKey":"xxxx","secret":"xxxx","passphrase":"xxxx"}
```
**Sauvegarder ces 3 valeurs immédiatement** — elles ne sont affichées qu'une seule fois.

---

### Étape E — Créer le VPS (AWS Lightsail Irlande)

1. Aller sur https://lightsail.aws.amazon.com
2. **Create instance** :
   - Region : **EU (Ireland) eu-west-1** ← obligatoire
   - Platform : Linux/Unix
   - Blueprint : **Ubuntu 22.04 LTS**
   - Plan : **$5/month** (512 MB RAM)
   - Donner un nom à l'instance
3. Télécharger la clé SSH `.pem` (bouton SSH key pairs)
4. Créer l'instance → attendre 1–2 minutes
5. Dans l'onglet **Networking** : vérifier que le port 22 (SSH) est ouvert

---

### Étape F — Déployer le bot sur le VPS

**F1. Se connecter en SSH** (depuis terminal ou PowerShell) :
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
cd ~/bot-polymarket
mkdir -p outputs/phase6 logs
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
MAX_BET_USDC=200
MIN_VOLUME_USD=500
```
Sauvegarder : **Ctrl+O** → **Entrée** → **Ctrl+X**

**F6. Créer le portfolio initial** :
```bash
cat > ~/bot-polymarket/outputs/phase6/live_portfolio.json << 'EOF'
{
  "capital_initial": 0.0,
  "capital_disponible": MONTANT_DEPOSE,
  "total_depose": MONTANT_DEPOSE,
  "positions_ouvertes": {},
  "trades_clos": []
}
EOF
```
⚠️ Remplacer `MONTANT_DEPOSE` par le montant exact déposé sur Polymarket (ex: `285.0`)

> **Pourquoi mettre le montant manuellement ?** Polymarket utilise un système pUSD interne (proxy wallet). La lecture automatique du solde retourne 0. Le bot utilise `capital_disponible` du JSON pour calculer les mises. À chaque nouveau dépôt, mettre à jour manuellement ce chiffre.

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

Attendre 60 minutes (ou relancer `sudo systemctl restart polymarket-bot` pour déclencher immédiatement), puis :
```bash
journalctl -u polymarket-bot -n 100 --no-pager
```

Lignes à chercher :
- `SUCCESS | [ENTREE]` → ordre placé ✅
- `ERROR 403 geoblock` → VPS dans un pays bloqué ❌ (changer de région)
- `Candidats trouvés : X signaux` → bot fonctionne, cherche des opportunités ✅

---

## 8. Surveillance quotidienne

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

# Mise à jour code
cd ~/bot-polymarket && git pull origin phase/5-paper && sudo systemctl restart polymarket-bot

# Redémarrage
sudo systemctl restart polymarket-bot
```

---

## 9. Ajouter des fonds

1. Transférer des USDC supplémentaires depuis Binance → MetaMask (Polygon)
2. Se connecter sur polymarket.com (avec VPN si depuis France) → Deposit
3. **Mettre à jour manuellement** `capital_disponible` et `total_depose` dans le portfolio :
```bash
# Lire le portfolio actuel
cat ~/bot-polymarket/outputs/phase6/live_portfolio.json

# Mettre à jour (exemple : ajout de 200 USDC, total 485)
# Modifier capital_disponible et total_depose dans le fichier
nano ~/bot-polymarket/outputs/phase6/live_portfolio.json
```

---

## 10. Résumé P&L (affiché à chaque cycle)

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

## 11. Transférer le bot à quelqu'un avec son propre wallet

Le bot est conçu pour fonctionner avec n'importe quel wallet. La nouvelle personne doit :

1. **Suivre le Guide complet** (sections A à G ci-dessus) avec son propre wallet MetaMask
2. **Cloner le repo GitHub** : `https://github.com/TotoSeg/bot-polymarket` — code 100% public, aucun secret dedans
3. **Générer ses propres clés API** Polymarket (étape D) avec sa clé privée
4. **Créer son propre VPS** en Irlande
5. **NE PAS utiliser** les clés API ou la clé privée d'un autre wallet

Chaque wallet est indépendant. Il n'y a rien à "transférer" du wallet actuel vers un nouveau — les secrets ne se partagent pas.

---

## 12. Fichiers critiques sur le VPS

| Fichier | Rôle |
|---------|------|
| `~/bot-polymarket/src/phase6_bot/.env` | Clé privée + clés API (**JAMAIS sur GitHub**) |
| `~/bot-polymarket/outputs/phase6/live_portfolio.json` | Portefeuille (trades, P&L, capital) |
| `/etc/systemd/system/polymarket-bot.service` | Service daemon (relance auto) |
| `~/bot-polymarket/logs/` | Logs journaliers |
| `/swapfile` | Swap 1 GB (stabilité RAM) |

---

## 13. Supprimer toutes les traces (PC de développement)

```
C:\Users\ThomasSegond\Desktop\Claude Code\Bot Polymarket\    ← dossier projet
C:\Users\ThomasSegond\.claude\projects\c--Users-...         ← mémoire Claude
C:\Users\ThomasSegond\key.pem                               ← clé SSH
```
```powershell
Remove-Item (Get-PSReadlineOption).HistorySavePath           # historique PowerShell
```
⚠️ Supprimer la clé SSH = perte d'accès SSH au VPS. Créer d'abord une nouvelle clé si nécessaire.

---

## 14. Questions fréquentes

**Q : Pourquoi le solde USDC affiche 0 dans les logs ?**
R : Polymarket utilise un système pUSD interne. La lecture automatique du solde ne fonctionné pas avec ce système. Le bot utilise `capital_disponible` du fichier JSON — à mettre à jour manuellement à chaque dépôt.

**Q : Le bot peut perdre de l'argent ?**
R : Oui. Les win rates (92–99%) sont des moyennes sur 2 ans. Sur 10 trades, 1–2 pertes sont possibles. Kelly limite chaque mise à 5% du capital.

**Q : VPN toujours nécessaire ?**
R : Uniquement pour accéder à polymarket.com depuis la France (dépôt, retrait, génération de clés API). Le bot lui-même tourne sur un VPS irlandais — pas de VPN nécessaire sur le VPS.

**Q : Les clés API expirent ?**
R : Non. Si régénérées, les anciennes sont révoquées. Mettre à jour `.env` et redémarrer le service.

**Q : Le bot tourne si mon PC est éteint ?**
R : Oui — il tourne sur le VPS AWS 24h/24, totalement indépendant.

**Q : Comment voir mes positions en direct ?**
R : Sur polymarket.com avec ton wallet connecté (positions + P&L flottant en temps réel).
