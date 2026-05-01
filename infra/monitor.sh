#!/usr/bin/env bash
# monitor.sh — Vérification rapide du bot Polymarket
# Usage : bash ~/bot-polymarket/infra/monitor.sh

BOT_DIR="$HOME/bot-polymarket"

echo "============================================="
echo "  MONITORING POLYMARKET BOT"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================="

# Statut du service
echo ""
echo "--- SERVICE ---"
sudo systemctl status polymarket-bot --no-pager -l 2>/dev/null || \
    echo "Service non installé (lancer manuellement avec --loop)"

# 30 dernières lignes de logs
echo ""
echo "--- DERNIERS LOGS ---"
sudo journalctl -u polymarket-bot -n 30 --no-pager 2>/dev/null || \
    tail -n 30 "$BOT_DIR/logs/"*_live_bot.log 2>/dev/null || \
    echo "Aucun log trouvé"

# Résumé du portefeuille
echo ""
echo "--- PORTEFEUILLE ---"
cd "$BOT_DIR"
source .venv/bin/activate 2>/dev/null
python src/phase6_bot/live_bot.py --status 2>/dev/null || \
    echo "Impossible d'afficher le portefeuille (venv non activé ?)"

echo ""
echo "============================================="
