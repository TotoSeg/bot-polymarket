#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Déploiement automatique du Polymarket Bot sur VPS Ubuntu 22.04
# =============================================================================
# Usage :
#   chmod +x deploy.sh && ./deploy.sh
#
# Ce script :
#   1. Installe Python 3.12 et git
#   2. Clone le repo GitHub (ou met à jour si déjà cloné)
#   3. Crée l'environnement virtuel et installe les dépendances
#   4. Initialise les répertoires et le portefeuille vide
#   5. Crée un .env template si absent
# =============================================================================
set -e  # Arrêter si une commande échoue

# ── CONFIGURATION — à adapter avant de lancer ────────────────────────────────
REPO_URL="https://github.com/TON_COMPTE/bot-polymarket.git"  # REMPLACER
BRANCH="phase/1-ingestion"
BOT_DIR="$HOME/bot-polymarket"
# ─────────────────────────────────────────────────────────────────────────────

echo "============================================="
echo "  Déploiement Polymarket Bot S3+SP"
echo "============================================="
echo ""

# 1. Mise à jour système et installation des prérequis
echo "[1/6] Installation des prérequis système..."
sudo apt-get update -q
sudo apt-get install -y -q software-properties-common git curl

# 2. Python 3.12
echo "[2/6] Installation de Python 3.12..."
sudo add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null
sudo apt-get install -y -q python3.12 python3.12-venv python3.12-dev
python3.12 --version

# 3. Cloner ou mettre à jour le repo
echo "[3/6] Récupération du code..."
if [ -d "$BOT_DIR/.git" ]; then
    echo "  -> Mise à jour du repo existant..."
    cd "$BOT_DIR"
    git fetch origin
    git checkout "$BRANCH"
    git pull origin "$BRANCH"
else
    echo "  -> Clonage du repo..."
    git clone -b "$BRANCH" "$REPO_URL" "$BOT_DIR"
    cd "$BOT_DIR"
fi

# 4. Environnement virtuel Python
echo "[4/6] Création de l'environnement virtuel..."
if [ ! -d "$BOT_DIR/.venv" ]; then
    python3.12 -m venv "$BOT_DIR/.venv"
fi
source "$BOT_DIR/.venv/bin/activate"
pip install --upgrade pip -q
pip install -r requirements-bot.txt -q
echo "  -> Dépendances installées"

# 5. Répertoires et fichiers initiaux
echo "[5/6] Initialisation des répertoires..."
mkdir -p "$BOT_DIR/outputs/phase6"
mkdir -p "$BOT_DIR/logs"

# Portfolio vide si absent
if [ ! -f "$BOT_DIR/outputs/phase6/live_portfolio.json" ]; then
    cat > "$BOT_DIR/outputs/phase6/live_portfolio.json" << 'EOF'
{
  "capital_initial": 500.0,
  "capital_disponible": 500.0,
  "positions_ouvertes": {},
  "trades_clos": []
}
EOF
    echo "  -> Portfolio initialisé (500 USDC)"
fi

# 6. Fichier .env si absent
echo "[6/6] Configuration .env..."
ENV_FILE="$BOT_DIR/src/phase6_bot/.env"
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" << 'EOF'
# Clé privée du wallet Ethereum/Polygon
POLYMARKET_PRIVATE_KEY=0x_VOTRE_CLE_PRIVEE_ICI

# Clés API Polymarket CLOB (générées avec --create-keys)
POLYMARKET_API_KEY=
POLYMARKET_API_SECRET=
POLYMARKET_API_PASSPHRASE=

# Paramètres du bot
INITIAL_CAPITAL_USDC=500
MAX_BET_USDC=25
MIN_VOLUME_USD=500
EOF
    echo "  -> .env créé — REMPLIR avec les vraies clés avant de lancer !"
else
    echo "  -> .env déjà présent, conservé tel quel"
fi

# Installer le service systemd
echo ""
echo "Installation du service systemd..."
sudo cp "$BOT_DIR/infra/polymarket-bot.service" /etc/systemd/system/
sudo sed -i "s|/home/ubuntu|$HOME|g" /etc/systemd/system/polymarket-bot.service
sudo systemctl daemon-reload
echo "  -> Service installé (pas encore démarré)"

# ── Résumé final ──────────────────────────────────────────────────────────────
echo ""
echo "============================================="
echo "  Déploiement terminé !"
echo "============================================="
echo ""
echo "PROCHAINES ETAPES MANUELLES :"
echo ""
echo "  1. Renseigner la clé privée dans .env :"
echo "     nano $ENV_FILE"
echo ""
echo "  2. Générer les clés API Polymarket :"
echo "     cd $BOT_DIR"
echo "     source .venv/bin/activate"
echo "     python src/phase6_bot/live_bot.py --create-keys"
echo "     -> Copier les 3 clés dans .env"
echo ""
echo "  3. Tester sans ordre réel :"
echo "     python src/phase6_bot/live_bot.py --dry-run"
echo ""
echo "  4. Lancer le bot en daemon :"
echo "     sudo systemctl enable --now polymarket-bot"
echo "     sudo systemctl status polymarket-bot"
echo ""
echo "  5. Surveiller les logs :"
echo "     bash $BOT_DIR/infra/monitor.sh"
echo ""
