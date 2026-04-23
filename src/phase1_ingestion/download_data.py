"""
Phase 1 — Téléchargement des données depuis HuggingFace
========================================================
Ce script télécharge les fichiers Parquet depuis un dataset HuggingFace
et les place dans le dossier data/ du projet.

Usage :
    python src/phase1_ingestion/download_data.py
    python src/phase1_ingestion/download_data.py --file quant.parquet
"""

import argparse
import sys
import os
from pathlib import Path
from datetime import datetime

# Forcer UTF-8 sur Windows pour éviter les erreurs d'encodage dans le terminal
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Loguru : logger moderne et coloré ────────────────────────────────────────
from loguru import logger

# ── HuggingFace Hub : client pour télécharger les datasets ───────────────────
from huggingface_hub import hf_hub_download, HfApi
from huggingface_hub.utils import EntryNotFoundError, RepositoryNotFoundError

# =============================================================================
# CONFIGURATION — À MODIFIER SELON VOTRE DATASET HUGGINGFACE
# =============================================================================

# Identifiant du dataset HuggingFace (format : "organisation/nom-du-dataset")
# Exemple : "Polymarket/polymarket-data" ou "moncompte/mon-dataset"
HF_DATASET_ID = "SII-WANGZJ/Polymarket_data"  # Dataset vérifié — contient markets/quant/users.parquet

# Type de dépôt HuggingFace : "dataset" ou "model"
HF_REPO_TYPE = "dataset"

# Fichiers à télécharger (dans l'ordre de priorité)
FILES_TO_DOWNLOAD = [
    "markets.parquet",   # Petit fichier — téléchargé en premier pour valider
    "quant.parquet",     # ~107 GB — téléchargement long
    "users.parquet",     # Phase avancée
]

# =============================================================================

# Dossier racine du projet (deux niveaux au-dessus de ce fichier)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"


def setup_logger():
    """Configure le logger pour écrire à la fois dans la console et dans un fichier."""
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_download_data.log"

    # Supprimer le handler par défaut et en ajouter deux : console + fichier
    logger.remove()
    logger.add(sys.stdout, colorize=True, format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
    logger.add(log_file, format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}")
    logger.info(f"Logs enregistrés dans : {log_file}")


def verify_dataset_exists(dataset_id: str) -> bool:
    """
    Vérifie que le dataset existe sur HuggingFace avant de télécharger.
    Retourne True si le dataset est accessible, False sinon.
    """
    api = HfApi()
    try:
        api.dataset_info(dataset_id)
        logger.success(f"Dataset trouvé sur HuggingFace : {dataset_id}")
        return True
    except RepositoryNotFoundError:
        logger.error(f"Dataset introuvable : '{dataset_id}'")
        logger.info("Vérifiez l'identifiant dans la variable HF_DATASET_ID")
        return False
    except Exception as e:
        logger.warning(f"Impossible de vérifier le dataset : {e}")
        return True  # On tente quand même le téléchargement


def download_file(filename: str, dataset_id: str) -> bool:
    """
    Télécharge un fichier depuis HuggingFace vers data/.
    Retourne True si succès, False si erreur.

    HuggingFace gère automatiquement :
    - La barre de progression
    - La reprise en cas d'interruption (cache local)
    - La vérification d'intégrité (hash SHA256)
    """
    dest_path = DATA_DIR / filename

    # Si le fichier existe déjà, on skip
    if dest_path.exists():
        size_mb = dest_path.stat().st_size / (1024 ** 2)
        logger.info(f"Fichier déjà présent ({size_mb:.1f} MB) : {filename} — skip")
        return True

    logger.info(f"Début du téléchargement : {filename}")
    logger.info(f"Dataset : {dataset_id}")
    logger.info(f"Destination : {dest_path}")

    try:
        # hf_hub_download télécharge dans le cache HuggingFace (~/.cache/huggingface)
        # puis copie vers local_dir si spécifié
        downloaded_path = hf_hub_download(
            repo_id=dataset_id,
            filename=filename,
            repo_type=HF_REPO_TYPE,
            local_dir=DATA_DIR,          # Copier directement dans data/
            local_dir_use_symlinks=False, # Copie réelle, pas de lien symbolique
        )
        size_mb = Path(downloaded_path).stat().st_size / (1024 ** 2)
        logger.success(f"Téléchargement terminé : {filename} ({size_mb:.1f} MB)")
        return True

    except EntryNotFoundError:
        logger.error(f"Fichier '{filename}' non trouvé dans le dataset '{dataset_id}'")
        logger.info("Vérifiez le nom du fichier sur la page HuggingFace du dataset")
        return False

    except KeyboardInterrupt:
        logger.warning("Téléchargement interrompu par l'utilisateur (Ctrl+C)")
        logger.info("Le téléchargement reprendra automatiquement au prochain lancement")
        return False

    except Exception as e:
        logger.error(f"Erreur lors du téléchargement de '{filename}' : {e}")
        return False


def main(files_to_download: list[str]):
    """Point d'entrée principal du script."""
    setup_logger()

    logger.info("=" * 60)
    logger.info("PHASE 1 — Téléchargement des données Polymarket")
    logger.info("=" * 60)

    # Créer le dossier data/ s'il n'existe pas
    DATA_DIR.mkdir(exist_ok=True)
    logger.info(f"Dossier de destination : {DATA_DIR}")

    # Vérifier que le dataset existe
    if not verify_dataset_exists(HF_DATASET_ID):
        logger.error("Arrêt du script — dataset introuvable")
        sys.exit(1)

    # Télécharger chaque fichier demandé
    results = {}
    for filename in files_to_download:
        logger.info(f"\n{'─' * 40}")
        success = download_file(filename, HF_DATASET_ID)
        results[filename] = "[OK]" if success else "[ECHEC]"

    # Résumé final
    logger.info(f"\n{'=' * 60}")
    logger.info("RÉSUMÉ DES TÉLÉCHARGEMENTS")
    logger.info("=" * 60)
    for filename, status in results.items():
        logger.info(f"  {status}  {filename}")

    # Lister les fichiers présents dans data/
    logger.info(f"\nContenu du dossier data/ :")
    for f in sorted(DATA_DIR.glob("*.parquet")):
        size_mb = f.stat().st_size / (1024 ** 2)
        size_str = f"{size_mb/1024:.2f} GB" if size_mb > 1024 else f"{size_mb:.1f} MB"
        logger.info(f"  {f.name:30s} {size_str}")


if __name__ == "__main__":
    # Gestion des arguments en ligne de commande
    parser = argparse.ArgumentParser(description="Télécharge les données Polymarket depuis HuggingFace")
    parser.add_argument(
        "--file",
        type=str,
        default=None,
        help="Nom du fichier à télécharger (ex: markets.parquet). Si absent, télécharge markets.parquet uniquement."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Télécharger tous les fichiers (markets, quant, users)"
    )
    args = parser.parse_args()

    if args.all:
        files = FILES_TO_DOWNLOAD
    elif args.file:
        files = [args.file]
    else:
        # Par défaut : seulement markets.parquet pour commencer
        files = ["markets.parquet"]

    main(files)
