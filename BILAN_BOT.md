# Polymarket Bot — Bilan complet

## 1. Ce que fait le bot en 2 phrases

Le bot scanne en continu les marchés de prédiction Polymarket, identifie ceux où le prix YES est anormalement bas (5–35 %), parie sur NO (l'issue contraire), et laisse la résolution faire le bénéfice. Il utilise deux stratégies à fort win rate issues d'un backtesting sur 1,1 milliard de trades historiques (2024–2025).

---

## 2. Les deux stratégies

### S3 — "Rien ne va se passer"
| Paramètre | Valeur |
|-----------|--------|
| Filtre prix | YES entre 5 % et 10 % |
| Marchés ciblés | Tous sauf crypto |
| Win rate historique | 97,7 % à 99,9 % selon le volume |
| ROI backtest 2024–25 | +1 495 % |
| Positions max simultanées | 15 |

**Logique** : un marché à YES=5-10% estime qu'un événement a peu de chances de se produire. Historiquement, ces marchés résolvent NO dans 97-99% des cas. Le bot achète NO et encaisse quand le marché expire.

**Win rate dynamique selon le volume :**
- Volume > 20 000 $ → prior 99,9 %
- Volume 5 000–20 000 $ → prior 98,1 %
- Volume 1 000–5 000 $ → prior 97,7 %
- Volume < 1 000 $ → prior 99,2 %

**Bonus automatiques** : +1,5 pp si marché politique, +0,5 pp si tech, +1,5 pp si durée 30–90 jours, +0,5 pp si YES entre 6–7 % (sweetspot).

---

### SP — "Politique / géopolitique"
| Paramètre | Valeur |
|-----------|--------|
| Filtre prix | YES entre 5 % et 35 % |
| Marchés ciblés | Politique US, géopolitique, élections, guerres, diplomatie |
| Win rate historique | 92,2 % à 99,9 % selon le prix d'entrée |
| ROI backtest 2024–25 | +1 522 % |
| Positions max simultanées | 10 |

**Logique** : les marchés politiques sur-estiment systématiquement les probabilités d'événements extrêmes. Plus le prix est bas, plus le win rate est élevé.

**Win rate par tranche de prix :**
- YES 5–10 % → prior 99,9 %
- YES 10–20 % → prior 99,2 %
- YES 20–35 % → prior 92,2 %

**Un marché politique à YES=7 % déclenche S3 ET SP simultanément** — deux positions indépendantes.

---

### Mots-clés de détection SP
Politique US : `trump, biden, harris, election, congress, senate, president, democrat, republican, white house, governor, midterm`

Géopolitique : `parliament, prime minister, chancellor, ceasefire, nato, sanction, coup, invasion, election, war, treaty, nuclear, troops`

---

## 3. Sizing des mises (Kelly criterion)

Le bot calcule la mise de chaque trade par la **formule de Kelly fractionnée** :

```
f* = (odds × win_rate - (1 - win_rate)) / odds
mise = capital_total × f* × 0.25      ← fraction Kelly (25%)
mise = min(mise, 5% du capital, MAX_BET_USDC)
```

**Exemples concrets avec 250 USDC :**
- S3 (WR=99,9%, YES=7%) → Kelly brut ~93% → après plafonnement → ~12,50 $
- SP 10-20% (WR=99,2%, YES=15%) → ~11 $
- SP 20-35% (WR=92,2%, YES=30%) → ~8 $

**Paramètres de sizing :**
| Paramètre | Valeur | Rôle |
|-----------|--------|------|
| `MAX_BET_USDC` | 25 $ | Plafond absolu par trade |
| `MAX_BET_PCT` | 5 % | Plafond en % du capital total |
| `KELLY_FRACTION` | 25 % | Fraction du Kelly théorique utilisée |
| `MIN_VOLUME_USD` | 500 $ | Volume minimum du marché |

---

## 4. Priorisation des marchés

Quand plusieurs signaux sont détectés, le bot les trie par :
1. **Date de résolution la plus proche en premier** — pour encaisser les gains plus vite et les réinvestir (effet composé accéléré)
2. **Score décroissant** en cas d'ex-æquo (score 0–10 basé sur volume, catégorie, durée)

---

## 5. Gestion du capital (automatique)

### Synchro automatique avec le wallet
À chaque cycle, le bot lit le **vrai solde USDC** du wallet Polygon via l'API. Plus besoin de renseigner `INITIAL_CAPITAL_USDC` manuellement.

### Détection automatique des dépôts
Si tu déposes des USDC supplémentaires sur le wallet, le bot détecte l'écart au prochain cycle et cumule le montant dans `total_depose`. Le ROI affiché est toujours calculé sur la **somme totale versée**, pas seulement le capital initial.

Exemple dans les logs :
```
Dépôt détecté : +100.00 USDC (total versé : 350.00 USDC)
```

### Capital Kelly = capital total courant
La base de calcul Kelly = capital disponible + capital engagé dans les positions ouvertes. Ainsi les mises croissent automatiquement avec les profits (effet composé réel).

---

## 6. Cycle complet (toutes les heures)

```
run_once()
  ├── 1. Fermer les positions résolues → mise + profit reviennent au capital
  ├── 2. Lire le vrai solde USDC → détecter les dépôts externes
  ├── 3. Calculer capital_kelly = dispo + engagé
  ├── 4. Scanner ~2700 marchés actifs (API Gamma)
  ├── 5. Filtrer S3 + SP (hors crypto, prix 5-35%)
  ├── 6. Trier par end_date ASC, puis score DESC
  ├── 7. Pour chaque candidat :
  │      Kelly size → vérif liquidité → ordre NO via CLOB
  └── 8. Sauvegarder live_portfolio.json + afficher résumé P&L
```

---

## 7. Architecture : qui tourne où

```
┌─────────────────────────────────────────┐     ┌──────────────────────────────┐
│  PC local (Claude Code / dev)           │     │  VPS AWS Lightsail           │
│  c:\...\Bot Polymarket\                 │     │  ubuntu@3.71.112.113         │
│                                         │     │                              │
│  • Développement, backtests             │     │  • Bot tourne 24/7           │
│  • Analyse DuckDB (107 GB parquet)      │ git │  • ~/bot-polymarket/         │
│  • gen_sig.py (génération clés API)     │────▶│  • systemd daemon            │
│  • Pas de clés privées commitées        │     │  • .env avec vrais secrets   │
└─────────────────────────────────────────┘     └──────────────────────────────┘
```

### Fichiers critiques sur le VPS

| Fichier | Rôle |
|---------|------|
| `~/bot-polymarket/src/phase6_bot/.env` | Clé privée + clés API (JAMAIS sur GitHub) |
| `~/bot-polymarket/outputs/phase6/live_portfolio.json` | Portefeuille live (trades, P&L, total versé) |
| `/etc/systemd/system/polymarket-bot.service` | Service systemd (relance auto après crash) |
| `~/bot-polymarket/logs/` | Logs journaliers |
| `/swapfile` | Swap 1 GB (stabilité mémoire) |

### Modules Python

```
src/
├── phase5_paper/
│   ├── polymarket_client.py   ← API Gamma : marchés actifs + résolutions
│   ├── strategy_signals.py    ← Détection S3 et SP, scores dynamiques
│   └── paper_portfolio.py     ← Portefeuille, Kelly sizing, P&L, ROI
└── phase6_bot/
    ├── live_bot.py            ← Orchestrateur principal, synchro wallet
    ├── order_executor.py      ← Ordres réels CLOB (FOK), vérif liquidité
    └── .env                   ← Secrets (gitignorés)
```

---

## 8. Suivre le bot

### Connexion SSH
```bash
ssh -i "C:\Users\ThomasSegond\Desktop\Claude Code\Bot Polymarket\clé SSH\LightsailDefaultKey-eu-central-1.pem" ubuntu@3.71.112.113
```

### Commandes utiles (depuis le VPS)
```bash
# Statut du service
sudo systemctl status polymarket-bot

# Logs en temps réel
journalctl -u polymarket-bot -f

# Bilan P&L complet
cd ~/bot-polymarket && source .venv/bin/activate
python src/phase6_bot/live_bot.py --status

# Arrêt / redémarrage
sudo systemctl stop polymarket-bot
sudo systemctl restart polymarket-bot
```

### Mise à jour du code (repo GitHub public)
```bash
cd ~/bot-polymarket && git pull origin phase/5-paper
sudo systemctl restart polymarket-bot
```

### Résumé P&L affiché à chaque cycle
```
  Total versé         :     250.00$    ← somme de tous tes dépôts
  Capital total       :     287.50$    ← dispo + engagé dans positions
  Capital disponible  :     237.50$    ← USDC libres dans le wallet
  Capital engagé      :      50.00$    ← misé dans 4 positions ouvertes
  P&L réalisé         :     +37.50$    ← gains nets des trades clos
  ROI (sur versements):     +15.00%
  Trades clos         :          12
  Win rate            :      91.7%
```

---

## 9. Déploiement et infrastructure

| Composant | Détail |
|-----------|--------|
| VPS | AWS Lightsail $5/mois, Frankfurt (eu-central-1) |
| OS | Ubuntu 22.04 LTS |
| Python | 3.10 (venv dans `~/bot-polymarket/.venv`) |
| RAM | 512 MB + 1 GB swap |
| Daemon | systemd, `Restart=always`, `RestartSec=60` |
| Réseau | IPv4 prioritaire (fix Cloudflare) |
| Logs | `/home/ubuntu/bot-polymarket/logs/YYYY-MM-DD_live_bot.log` |

---

## 10. Effacer les traces de ce PC

Les clés sont uniquement sur le VPS. Pour supprimer proprement ce PC :

**10a. Supprimer le dossier projet**
```
C:\Users\ThomasSegond\Desktop\Claude Code\Bot Polymarket\
```

**10b. Supprimer la mémoire Claude**
```
C:\Users\ThomasSegond\.claude\projects\c--Users-ThomasSegond-Desktop-Claude-Code-Bot-Polymarket\
```

**10c. Supprimer la clé SSH**
```
C:\Users\ThomasSegond\key.pem
C:\Users\ThomasSegond\Desktop\Claude Code\Bot Polymarket\clé SSH\LightsailDefaultKey-eu-central-1.pem
```
⚠️ Sans cette clé tu perds l'accès SSH au VPS.

**10d. Vider l'historique PowerShell**
```powershell
Remove-Item (Get-PSReadlineOption).HistorySavePath
```

---

## 11. Transférer le bot à quelqu'un d'autre

**Étape 1 — Donner l'accès SSH**
```bash
# Sur le VPS, ajouter la clé publique de la nouvelle personne
echo "NOUVELLE_CLE_PUBLIQUE" >> ~/.ssh/authorized_keys
```
Ou créer un snapshot de l'instance dans la console Lightsail.

**Étape 2 — Transmettre les secrets**
```bash
cat ~/bot-polymarket/src/phase6_bot/.env
```
À transmettre de façon sécurisée :
- `POLYMARKET_PRIVATE_KEY` ← donne accès aux fonds, à traiter comme un mot de passe bancaire
- `POLYMARKET_API_KEY` / `API_SECRET` / `API_PASSPHRASE`

**Étape 3 — Transmettre ce document + l'IP du VPS (3.71.112.113)**

---

## 12. Questions fréquentes

**Q : Le bot peut perdre de l'argent ?**
R : Oui. Les win rates sont des moyennes sur 2 ans de données. Sur 10 trades, 1-2 pertes sont possibles. Kelly limite le risque par trade à 5 % du capital.

**Q : Je dépose des USDC — est-ce que le bot s'en aperçoit ?**
R : Oui, automatiquement au prochain cycle (max 1 heure). Il détecte l'augmentation du solde wallet et adapte les mises.

**Q : Où voir mes positions en direct ?**
R : Sur polymarket.com avec ton wallet connecté (positions + P&L flottant). Pour le bilan comptable complet (ROI, win rate) : `python live_bot.py --status` sur le VPS.

**Q : Le bot tourne même si mon PC est éteint ?**
R : Oui. Il tourne sur le VPS AWS 24h/24, indépendamment de ton PC.

**Q : Que faire si le bot plante ?**
R : systemd le redémarre automatiquement après 60 secondes. Pour voir l'erreur : `journalctl -u polymarket-bot -n 50`.

**Q : Les clés API Polymarket expirent ?**
R : Non. Si tu les régénères, les anciennes sont révoquées — mettre à jour `.env` et redémarrer.

**Q : Comment mettre à jour le code ?**
R : Sur le VPS : `cd ~/bot-polymarket && git pull origin phase/5-paper && sudo systemctl restart polymarket-bot`
