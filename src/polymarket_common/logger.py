"""
polymarket_common/logger.py
============================
Configuration de logging partagée.

Le bot directionnel existant (live_bot.py) utilise loguru, pas le logging
stdlib. Pour ne pas changer le format des logs existants, ce module reproduit
exactement la configuration de live_bot.py::setup() — même format, même
rotation journalière par fichier — mais avec un nom de fichier paramétrable
pour que bot_lp écrive dans son propre fichier.

Usage :
    from polymarket_common.logger import setup_logger
    logger = setup_logger("live_bot")   # bot directionnel -> logs/AAAA-MM-JJ_live_bot.log
    logger = setup_logger("lp")         # bot LP            -> logs/AAAA-MM-JJ_lp.log

loguru utilise un sink global unique par processus : appeler setup_logger()
une seule fois au démarrage de chaque bot (les deux bots tournent dans des
processus séparés, donc pas de conflit entre eux).
"""

import sys
from pathlib import Path
from datetime import datetime

from loguru import logger


def setup_logger(name: str, log_dir: str = "logs", level: str = "INFO"):
    """
    Configure loguru avec :
      - un handler stdout colorisé, niveau `level` (INFO par défaut)
      - un handler fichier `{log_dir}/{AAAA-MM-JJ}_{name}.log` (tous niveaux)

    Format identique à celui de live_bot.py pour ne pas casser le parsing
    des logs existants.
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    logger.remove()
    logger.add(
        sys.stdout, colorize=True, level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
    )
    logger.add(
        log_path / f"{datetime.now().strftime('%Y-%m-%d')}_{name}.log",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
    )
    return logger
