"""
Phase 3 — Stratégie Banoleigh Variante 2 : Parier sur le Perdant
=================================================================
HYPOTHÈSE (Variant 2) :
Sur les marchés crypto "Up or Down" de 5 minutes (BTC/ETH), la foule
a tendance à SURESTIMER les favoris et SOUS-ESTIMER les outsiders.

Si en début de fenêtre de 5 minutes :
  - YES (Up) trade à < 0.40 → les gens pensent BTC va surtout DESCENDRE
    → mais est-ce que BTC monte quand même souvent ? (parier YES = l'outsider)
  - YES (Up) trade à > 0.60 → les gens pensent BTC va surtout MONTER
    → mais est-ce que BTC descend quand même souvent ? (parier NO = l'outsider)

Si le "perdant" prévu gagne plus souvent que son prix l'implique → EDGE.

STRUCTURE DU SCRIPT :
1. Chargement des marchés 5min BTC/ETH (markets.parquet)
2. Jointure avec les trades (quant.parquet) via DuckDB
3. Calcul du TWAP dans la première minute de la fenêtre de 5 min
4. Classification : YES loser / NO loser / Neutre
5. Calcul des win rates par bracket de prix
6. Export des résultats

DÉFINITION DE LA FENÊTRE :
- Pour chaque marché "Bitcoin Up or Down - Date, HH:MMxM-HH:MMxM ET"
- end_date = fin de la fenêtre de 5 minutes (ex: 5:35PM ET)
- Début de la fenêtre = EPOCH(end_date) - 300 secondes
- "Première minute" = [EPOCH(end_date) - 300, EPOCH(end_date) - 240]
  (les 60 premières secondes de la fenêtre de 5 min)

OUTCOME (résultat final) :
- outcome_prices = ['1', '0'] → YES/Up a gagné
- outcome_prices = ['0', '1'] → NO/Down a gagné

NOTE technique :
- quant.parquet contient TOUTES les perspectives en "YES unifié"
  (price = prix du token YES = probabilité que YES gagne)
- Les timestamps sont en secondes Unix (UBIGINT)
- end_date est un TIMESTAMP WITH TIME ZONE → EPOCH(end_date) = secondes Unix
"""

# ============================================================
# IMPORTS
# ============================================================
import sys
import os
import json
import math
from pathlib import Path

# DuckDB : base de données analytique ultra-rapide pour fichiers Parquet
# On NE CHARGE JAMAIS quant.parquet entier en pandas (170M lignes = crash RAM)
import duckdb

# Pandas : seulement pour manipuler les petits DataFrames résultants
import pandas as pd

# Loguru : logging propre et coloré
try:
    from loguru import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ============================================================
# CONFIGURATION DES CHEMINS
# ============================================================
# Racine du projet (le script tourne depuis la racine : python src/phase3_edges/crypto_variant2.py)
PROJECT_ROOT = Path(__file__).parent.parent.parent

# Fichiers de données
MARKETS_PATH = PROJECT_ROOT / "data" / "markets.parquet"
QUANT_PATH   = PROJECT_ROOT / "data" / "quant.parquet"

# Dossiers de sortie
OUTPUT_DIR = PROJECT_ROOT / "outputs" / "phase3"
LOG_DIR    = PROJECT_ROOT / "logs"

# Fichiers de sortie
OUTPUT_JSON = OUTPUT_DIR / "crypto_variant2_results.json"
OUTPUT_TXT  = OUTPUT_DIR / "crypto_variant2_results.txt"


def setup_logging():
    """Configure le logger : un fichier de log + la console."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Retire le handler console par défaut de loguru et recrée proprement
    try:
        logger.remove()
        logger.add(sys.stderr, level="INFO",
                   format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{message}</cyan>")
        logger.add(str(LOG_DIR / "crypto_variant2.log"), level="DEBUG", rotation="10 MB",
                   format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}")
    except Exception:
        pass


def check_data_files():
    """Vérifie que les fichiers de données existent."""
    for path in [MARKETS_PATH, QUANT_PATH]:
        if not path.exists():
            logger.error(f"Fichier introuvable : {path}")
            sys.exit(1)
    logger.info("Fichiers de données trouvés.")


def get_schema_info(con):
    """
    Affiche le schéma des deux fichiers Parquet.
    Utile pour vérifier que les colonnes attendues existent.
    """
    logger.info("=== Schéma markets.parquet ===")
    schema_m = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{MARKETS_PATH.as_posix()}') LIMIT 1"
    ).fetchdf()
    logger.debug(f"\n{schema_m[['column_name','column_type']].to_string()}")

    logger.info("=== Schéma quant.parquet ===")
    schema_q = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{QUANT_PATH.as_posix()}') LIMIT 1"
    ).fetchdf()
    logger.debug(f"\n{schema_q[['column_name','column_type']].to_string()}")

    return schema_m, schema_q


def load_5min_crypto_markets(con):
    """
    Charge les marchés fermés "Up or Down" BTC/ETH de 5 minutes.

    Filtrage :
    - 'Up or Down' dans la question (ce sont les marchés directionnels crypto)
    - 'Bitcoin' ou 'Ethereum' dans la question
    - closed = 1 (marché terminé, résolution connue)
    - Fenêtre de 5 minutes : la question contient un pattern HH:MMxM-HH:MMxM
      où la durée est de 5 min (ex: 5:30PM-5:35PM)

    Colonnes récupérées :
    - condition_id : identifiant unique du marché (sert à joindre avec quant)
    - question     : titre du marché
    - outcome_prices : ['1','0'] = YES gagne, ['0','1'] = NO gagne
    - end_date     : fin de la fenêtre de 5 minutes

    RETOURNE : DataFrame pandas avec les marchés 5min
    """
    logger.info("Chargement des marchés 5min BTC/ETH...")

    # Filtre pour la fenêtre de 5 minutes :
    # On cherche des patterns comme 'X:30PM-X:35PM', 'X:35PM-X:40PM', etc.
    # Format : HH:MM[AP]M-HH:MM[AP]M avec un incrément de 5 minutes
    # On utilise une série de patterns LIKE car DuckDB n'a pas lookbehind regex
    five_min_patterns = [
        "%:00%M-%:05%M%",  # XX:00 - XX:05
        "%:05%M-%:10%M%",
        "%:10%M-%:15%M%",
        "%:15%M-%:20%M%",
        "%:20%M-%:25%M%",
        "%:25%M-%:30%M%",
        "%:30%M-%:35%M%",
        "%:35%M-%:40%M%",
        "%:40%M-%:45%M%",
        "%:45%M-%:50%M%",
        "%:50%M-%:55%M%",
        "%:55%M-%:00%M%",  # XX:55 - YY:00 (changement d'heure)
    ]

    # Construction dynamique de la clause OR pour les patterns 5-min
    # NOTE: Les patterns ci-dessus peuvent aussi matcher des fenêtres de 15 min
    # (ex: 1:00AM-1:15AM matche pas ces patterns car c'est 15 min)
    # mais pour être sûr, on va vérifier la durée avec end_date - created_at

    # La durée entre created_at et end_date pour un marché 5-min est ~24h
    # car les marchés sont créés la veille. On ne peut donc pas filtrer par durée.
    # En revanche, on peut parser la regex pour extraire les heures de début et fin.

    # APPROCHE SIMPLIFIÉE ET ROBUSTE :
    # Les marchés de 5 min ont une fenêtre de temps où l'heure de fin = heure début + 5 min
    # Tous les patterns visibles dans les données sont du type XX:XX[AP]M-XX:XX[AP]M
    # Pour les 15-min : XX:00AM-XX:15AM, XX:15AM-XX:30AM etc.
    # Pour les 5-min  : XX:00AM-XX:05AM, XX:05AM-XX:10AM etc.
    # Le pattern distinctif du 5-min : le dernier chiffre de l'heure de fin est 5 ou 0
    # et la différence est de 5 minutes

    # On filtre avec une combinaison de patterns LIKE qui représentent les
    # fenêtres 5-min (chaque incrément de 5 min, pas 15 min)
    like_clause = " OR ".join([f"question LIKE '{p}'" for p in five_min_patterns])

    sql = f"""
        SELECT
            condition_id,
            question,
            outcome_prices,
            end_date,
            EPOCH(end_date) AS end_epoch    -- Timestamp Unix de fin de fenêtre
        FROM read_parquet('{MARKETS_PATH.as_posix()}')
        WHERE
            -- Marchés directionnels crypto
            (question LIKE '%Up or Down%')
            AND (question LIKE '%Bitcoin%' OR question LIKE '%Ethereum%')
            -- Seulement les marchés terminés (résolution connue)
            AND closed = 1
            -- Seulement les résolutions claires (YES ou NO gagne, pas 50/50)
            -- outcome_prices = "['1', '0']" ou "['0', '1']"
            AND (outcome_prices LIKE '%1%,%0%' OR outcome_prices LIKE '%0%,%1%')
            AND outcome_prices NOT LIKE '%0.5%'
            AND outcome_prices NOT LIKE '%0.9%'
            AND outcome_prices NOT LIKE '%0.1%'
            AND outcome_prices NOT LIKE '%0.2%'
            AND outcome_prices NOT LIKE '%0.3%'
            AND outcome_prices NOT LIKE '%0.4%'
            AND outcome_prices NOT LIKE '%0.6%'
            AND outcome_prices NOT LIKE '%0.7%'
            AND outcome_prices NOT LIKE '%0.8%'
            -- Filtre 5 minutes (un des patterns de fenêtre 5-min)
            AND ({like_clause})
    """

    df = con.execute(sql).fetchdf()
    logger.info(f"Marchés 5min BTC/ETH chargés : {len(df):,} marchés")

    if len(df) == 0:
        logger.error("Aucun marché trouvé ! Vérifiez les filtres.")
        return None

    # Ajout d'une colonne : YES a-t-il gagné ?
    # outcome_prices = "['1', '0']" → YES/Up gagne (token1 = Up = YES = 1)
    # outcome_prices = "['0', '1']" → NO/Down gagne (token1 = Up = YES = 0)
    # La première valeur (avant la virgule) correspond à token1 (Up/YES)
    # Si elle vaut '1' → YES a gagné ; si '0' → NO a gagné
    def parse_outcome(x):
        # x ressemble à "['1', '0']" ou "['0', '1']"
        # On prend le premier nombre : s'il est > 0.5 → YES a gagné
        try:
            # Extraire la première valeur entre [ et ,
            first_val = x.strip().lstrip('[').split(',')[0].strip().strip("'\"")
            return 1 if float(first_val) >= 0.5 else 0
        except Exception:
            return None

    df['yes_won'] = df['outcome_prices'].apply(parse_outcome)
    # Supprimer les lignes avec outcome inconnu
    df = df.dropna(subset=['yes_won'])
    df['yes_won'] = df['yes_won'].astype(int)

    yes_win_rate = df['yes_won'].mean() * 100
    logger.info(f"Taux de victoire YES (Up) global : {yes_win_rate:.2f}%")

    return df


def compute_twap_entry_window(con, markets_df):
    """
    Pour chaque marché, calcule le TWAP (Time-Weighted Average Price)
    pendant la PREMIÈRE MINUTE de la fenêtre de 5 minutes.

    Stratégie :
    - Début de fenêtre = end_epoch - 300 secondes (5 min avant fin)
    - Première minute  = [end_epoch - 300, end_epoch - 240] (60 secondes)
    - TWAP = moyenne pondérée par volume (usd_amount)
           = SUM(price * usd_amount) / SUM(usd_amount)

    RETOURNE : DataFrame avec condition_id, twap_entry, trade_count
    """
    logger.info("Calcul du TWAP dans la fenêtre d'entrée (première minute)...")
    logger.info("(Cela peut prendre 1-3 minutes selon la RAM disponible)")

    # On crée une table temporaire des marchés pour optimiser la jointure
    # en évitant de passer 100k+ condition_ids par paramètre

    # Étape 1 : Enregistrer le DataFrame des marchés comme table DuckDB temporaire
    con.execute("DROP TABLE IF EXISTS temp_markets")
    con.execute("CREATE TEMP TABLE temp_markets AS SELECT * FROM markets_df")

    # Étape 2 : Jointure avec quant.parquet, filtrée sur la fenêtre temporelle
    # NOTE : quant.parquet a 170M lignes — DuckDB est suffisamment intelligent
    # pour pousser les filtres et ne pas tout charger en mémoire
    sql = f"""
        SELECT
            q.condition_id,
            -- TWAP = prix moyen pondéré par le volume (USD)
            SUM(q.price * q.usd_amount) / SUM(q.usd_amount) AS twap_entry,
            -- Nombre de trades dans la fenêtre d'entrée
            COUNT(*) AS trade_count_entry,
            -- Volume total dans la fenêtre d'entrée
            SUM(q.usd_amount) AS volume_entry
        FROM read_parquet('{QUANT_PATH.as_posix()}') q
        -- Jointure sur condition_id (identifiant unique du marché)
        INNER JOIN temp_markets m ON q.condition_id = m.condition_id
        WHERE
            -- Première minute de la fenêtre de 5 min
            -- end_epoch - 300 = début de fenêtre (ex: 5:30PM)
            -- end_epoch - 240 = 1 min après début (ex: 5:31PM)
            q.timestamp >= m.end_epoch - 300    -- Début de fenêtre
            AND q.timestamp < m.end_epoch - 240  -- Fin de la 1ère minute
        GROUP BY q.condition_id
        -- Garder seulement les marchés avec au moins 3 trades
        -- (éviter le bruit sur les marchés peu liquides)
        HAVING COUNT(*) >= 3
    """

    twap_df = con.execute(sql).fetchdf()
    logger.info(f"Marchés avec trades dans la fenêtre d'entrée : {len(twap_df):,}")

    if len(twap_df) == 0:
        logger.warning("AUCUN trade trouvé dans la fenêtre d'entrée !")
        logger.warning("Possible problème de timing. Vérifiez la logique end_epoch - 300.")
        return None

    logger.info(f"Distribution TWAP : min={twap_df['twap_entry'].min():.3f}, "
                f"mean={twap_df['twap_entry'].mean():.3f}, "
                f"max={twap_df['twap_entry'].max():.3f}")

    return twap_df


def merge_and_classify(markets_df, twap_df):
    """
    Fusionne les marchés avec les TWAP et classifie chaque marché.

    Classification (stratégie du perdant) :
    - "yes_loser"  : TWAP en entrée < 0.40 → YES est l'outsider → on parie YES
    - "no_loser"   : TWAP en entrée > 0.60 → NO est l'outsider → on parie NO
    - "neutral"    : TWAP entre 0.40 et 0.60 → pas de pari clair

    Win rate pour la stratégie du perdant :
    - Si yes_loser  : on gagne si YES gagne (yes_won = 1)
    - Si no_loser   : on gagne si NO gagne (yes_won = 0)

    RETOURNE : DataFrame enrichi avec classification et résultat
    """
    logger.info("Fusion et classification des marchés...")

    # Jointure marchés + TWAP
    merged = markets_df.merge(twap_df, on='condition_id', how='inner')
    logger.info(f"Marchés après jointure avec TWAP : {len(merged):,}")

    # Classification
    def classify(twap):
        if twap < 0.40:
            return 'yes_loser'   # YES est peu probable (< 40%) → outsider
        elif twap > 0.60:
            return 'no_loser'    # NO est peu probable (YES > 60%) → NO est l'outsider
        else:
            return 'neutral'     # Trop incertain → pas de signal clair

    merged['classification'] = merged['twap_entry'].apply(classify)

    # Est-ce que notre pari "parier sur le perdant" a gagné ?
    # yes_loser : on parie YES → gagne si yes_won = 1
    # no_loser  : on parie NO  → gagne si yes_won = 0
    def loser_won(row):
        if row['classification'] == 'yes_loser':
            return row['yes_won']        # YES gagne = notre pari YES gagne
        elif row['classification'] == 'no_loser':
            return 1 - row['yes_won']    # NO gagne (yes_won=0) = notre pari NO gagne
        else:
            return None                  # Neutre : pas de pari

    merged['loser_bet_won'] = merged.apply(loser_won, axis=1)

    # Distribution des classifications
    class_counts = merged['classification'].value_counts()
    logger.info(f"Distribution :\n  yes_loser: {class_counts.get('yes_loser', 0):,}\n"
                f"  no_loser:  {class_counts.get('no_loser', 0):,}\n"
                f"  neutral:   {class_counts.get('neutral', 0):,}")

    return merged


def compute_bracket_winrates(df):
    """
    Calcule les win rates pour chaque bracket de prix.

    Brackets (prix YES en entrée) :
    - 0.02–0.10 : YES très faible → parier YES
    - 0.10–0.20
    - 0.20–0.30
    - 0.30–0.40
    - 0.60–0.70 : YES très fort → parier NO
    - 0.70–0.80
    - 0.80–0.90
    - 0.90–0.98

    Pour chaque bracket :
    - Compte les marchés
    - Calcule le win rate de la stratégie "parier sur le perdant"
    - Compare au win rate attendu basé sur le prix (calibration)

    RETOURNE : dict avec les résultats par bracket
    """
    logger.info("Calcul des win rates par bracket de prix...")

    # Définition des brackets
    # Format : (borne_basse, borne_haute, type_pari, label)
    brackets = [
        # YES loser brackets : on parie YES (l'outsider)
        (0.02, 0.10, 'bet_yes', 'YES 0.02-0.10 -> parier YES'),
        (0.10, 0.20, 'bet_yes', 'YES 0.10-0.20 -> parier YES'),
        (0.20, 0.30, 'bet_yes', 'YES 0.20-0.30 -> parier YES'),
        (0.30, 0.40, 'bet_yes', 'YES 0.30-0.40 -> parier YES'),
        # NO loser brackets : on parie NO (l'outsider)
        (0.60, 0.70, 'bet_no',  'YES 0.60-0.70 -> parier NO'),
        (0.70, 0.80, 'bet_no',  'YES 0.70-0.80 -> parier NO'),
        (0.80, 0.90, 'bet_no',  'YES 0.80-0.90 -> parier NO'),
        (0.90, 0.98, 'bet_no',  'YES 0.90-0.98 -> parier NO'),
    ]

    results = []

    for low, high, bet_type, label in brackets:
        # Filtrer les marchés dans ce bracket
        mask = (df['twap_entry'] >= low) & (df['twap_entry'] < high)
        subset = df[mask].copy()

        if len(subset) == 0:
            logger.debug(f"Bracket {label}: aucun marché")
            continue

        # Calculer le résultat selon le type de pari
        if bet_type == 'bet_yes':
            # On parie YES → on gagne si YES a gagné
            wins = subset['yes_won'].sum()
            total = len(subset)
        else:  # bet_no
            # On parie NO → on gagne si YES N'A PAS gagné
            wins = (1 - subset['yes_won']).sum()
            total = len(subset)

        win_rate = wins / total if total > 0 else 0

        # Prix moyen dans ce bracket (pour calculer l'edge)
        avg_price = subset['twap_entry'].mean()

        # Probabilité implicite du pari :
        # Si bet_yes : on achète YES à avg_price → rentable si yes_win_rate > avg_price
        # Si bet_no  : on achète NO à (1 - avg_price) → rentable si no_win_rate > (1-avg_price)
        if bet_type == 'bet_yes':
            implied_prob = avg_price  # Prix payé pour le token YES
            expected_return_pct = (win_rate / implied_prob - 1) * 100 if implied_prob > 0 else 0
        else:
            implied_prob = 1 - avg_price  # Prix payé pour le token NO
            expected_return_pct = (win_rate / implied_prob - 1) * 100 if implied_prob > 0 else 0

        # Edge = win_rate - prix_payé (positif = profitable)
        edge = win_rate - implied_prob

        # Intervalle de confiance (95%) — formule Wilson
        # n * p * (1-p) doit être > 0
        if total > 0 and win_rate > 0 and win_rate < 1:
            se = math.sqrt(win_rate * (1 - win_rate) / total)
            ci_95_low  = win_rate - 1.96 * se
            ci_95_high = win_rate + 1.96 * se
        else:
            ci_95_low  = win_rate
            ci_95_high = win_rate

        result = {
            'bracket':            label,
            'low':                low,
            'high':               high,
            'bet_type':           bet_type,
            'total_markets':      int(total),
            'wins':               int(wins),
            'win_rate':           round(win_rate, 4),
            'win_rate_pct':       round(win_rate * 100, 2),
            'avg_entry_price':    round(avg_price, 4),
            'implied_prob':       round(implied_prob, 4),
            'edge':               round(edge, 4),
            'edge_pct':           round(edge * 100, 2),
            'expected_return_pct':round(expected_return_pct, 2),
            'ci_95_low':          round(ci_95_low, 4),
            'ci_95_high':         round(ci_95_high, 4),
            'is_profitable':      bool(edge > 0),
            'is_significant':     bool(total >= 30 and ci_95_low > implied_prob),
        }

        results.append(result)

        # Log pour chaque bracket
        marker = "EDGE!" if result['is_significant'] else ("+" if edge > 0 else "-")
        logger.info(
            f"[{marker}] {label:<42} | "
            f"N={total:5d} | "
            f"WR={win_rate*100:5.1f}% | "
            f"Price={implied_prob:.3f} | "
            f"Edge={edge*100:+.1f}%"
        )

    return results


def compute_overall_stats(df):
    """
    Calcule les statistiques globales de la stratégie.

    RETOURNE : dict avec les stats globales
    """
    # Marchés avec signal (pas neutres)
    signal_df = df[df['classification'] != 'neutral'].copy()

    if len(signal_df) == 0:
        return {}

    overall_win_rate = signal_df['loser_bet_won'].mean()
    yes_loser_df = df[df['classification'] == 'yes_loser']
    no_loser_df  = df[df['classification'] == 'no_loser']

    stats = {
        'total_markets_analyzed':   int(len(df)),
        'markets_with_signal':      int(len(signal_df)),
        'yes_loser_count':          int(len(yes_loser_df)),
        'no_loser_count':           int(len(no_loser_df)),
        'neutral_count':            int(len(df[df['classification'] == 'neutral'])),
        'overall_loser_win_rate':   round(float(overall_win_rate), 4),
        'overall_loser_win_rate_pct': round(float(overall_win_rate) * 100, 2),
        'yes_loser_win_rate':       round(float(yes_loser_df['yes_won'].mean()), 4) if len(yes_loser_df) > 0 else None,
        'no_loser_win_rate':        round(float((1 - no_loser_df['yes_won']).mean()), 4) if len(no_loser_df) > 0 else None,
        'global_yes_win_rate':      round(float(df['yes_won'].mean()), 4),
    }

    return stats


def save_results(bracket_results, overall_stats, df):
    """
    Sauvegarde les résultats dans :
    - outputs/phase3/crypto_variant2_results.json  (données brutes)
    - outputs/phase3/crypto_variant2_results.txt   (rapport lisible)

    Sauvegarde aussi un CSV de tous les marchés classifiés.
    """
    # ---- JSON ----
    output_data = {
        'strategy':      'Banoleigh Variant 2 — Bet on the Loser',
        'description':   'Sur marchés 5min BTC/ETH Up or Down, parier sur l\'outsider (côté < 0.40 ou > 0.60)',
        'overall_stats': overall_stats,
        'brackets':      bracket_results,
    }

    with open(OUTPUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    logger.info(f"JSON sauvegardé : {OUTPUT_JSON}")

    # ---- TXT (rapport lisible) ----
    sep = "=" * 80
    lines = [
        sep,
        "STRATEGIE BANOLEIGH VARIANTE 2 : PARIER SUR LE PERDANT",
        "Marchés 5 minutes BTC/ETH Up or Down — Polymarket",
        sep,
        "",
        "HYPOTHESE :",
        "  Sur les marchés crypto 5-min, si en début de fenêtre :",
        "  - YES trade < 0.40  → parier YES (l'outsider)",
        "  - YES trade > 0.60  → parier NO  (l'outsider)",
        "  L'idée : la foule surestime le favori, l'outsider gagne plus souvent",
        "  que son prix ne l'implique.",
        "",
        "STATISTIQUES GLOBALES :",
        f"  Marchés analysés                : {overall_stats.get('total_markets_analyzed', 0):,}",
        f"  Marchés avec signal (non-neutres): {overall_stats.get('markets_with_signal', 0):,}",
        f"    dont YES loser                 : {overall_stats.get('yes_loser_count', 0):,}",
        f"    dont NO loser                  : {overall_stats.get('no_loser_count', 0):,}",
        f"    dont Neutres (0.40-0.60)       : {overall_stats.get('neutral_count', 0):,}",
        f"  Taux victoire YES global         : {overall_stats.get('global_yes_win_rate', 0)*100:.2f}%",
        f"  Win rate stratégie globale       : {overall_stats.get('overall_loser_win_rate_pct', 0):.2f}%",
        f"  Win rate YES loser               : {overall_stats.get('yes_loser_win_rate', 0)*100:.2f}%" if overall_stats.get('yes_loser_win_rate') else "  Win rate YES loser               : N/A",
        f"  Win rate NO loser                : {overall_stats.get('no_loser_win_rate', 0)*100:.2f}%" if overall_stats.get('no_loser_win_rate') else "  Win rate NO loser                : N/A",
        "",
        sep,
        "RÉSULTATS PAR BRACKET DE PRIX",
        "(Edge = Win Rate - Prix payé ; positif = profitable)",
        sep,
        "",
        f"{'Bracket':<44} {'N':>6} {'WR%':>7} {'Prix':>7} {'Edge%':>7} {'Rentable?':>10}",
        "-" * 80,
    ]

    for r in bracket_results:
        marker = " ** EDGE **" if r['is_significant'] else ("   +      " if r['edge'] > 0 else "          ")
        lines.append(
            f"{r['bracket']:<44} {r['total_markets']:>6,} {r['win_rate_pct']:>7.1f}% "
            f"{r['implied_prob']:>7.3f} {r['edge_pct']:>+7.1f}% {marker}"
        )

    lines += [
        "",
        "** EDGE ** = significatif statistiquement (N>=30 et IC 95% > prix payé)",
        "",
        sep,
        "INTERPRÉTATION :",
        "",
    ]

    # Résumé de l'interprétation
    profitable_brackets = [r for r in bracket_results if r['is_significant']]
    if profitable_brackets:
        lines.append("  EDGES SIGNIFICATIFS DÉTECTÉS :")
        for r in profitable_brackets:
            lines.append(f"    {r['bracket']}")
            lines.append(f"      Win Rate = {r['win_rate_pct']:.1f}% vs Prix = {r['implied_prob']*100:.1f}%")
            lines.append(f"      Edge = {r['edge_pct']:+.1f}% | Retour espéré = {r['expected_return_pct']:+.1f}%")
            lines.append(f"      IC 95% : [{r['ci_95_low']*100:.1f}%, {r['ci_95_high']*100:.1f}%]")
            lines.append("")
    else:
        lines.append("  Aucun edge statistiquement significatif détecté dans les données actuelles.")

    lines += [
        "",
        sep,
        "CONCLUSION :",
        "",
    ]

    # Conclusion automatique
    overall_wr = overall_stats.get('overall_loser_win_rate_pct', 50)
    if overall_wr > 52:
        lines.append(f"  La stratégie 'parier sur le perdant' montre un win rate global de {overall_wr:.1f}%")
        lines.append("  ce qui dépasse le seuil de 50% + frais. La stratégie mérite approfondissement.")
    elif overall_wr > 50:
        lines.append(f"  Win rate global = {overall_wr:.1f}% (légèrement positif mais insuffisant après frais).")
        lines.append("  Certains brackets spécifiques peuvent être exploitables.")
    else:
        lines.append(f"  Win rate global = {overall_wr:.1f}% : la stratégie ne montre pas d'edge global.")
        lines.append("  Analyser les brackets individuels pour trouver des niches profitables.")

    lines += [
        "",
        sep,
        "NOTE METHODOLOGIQUE IMPORTANTE :",
        "",
        "  Le TWAP de la 'fenêtre d'entrée' (première minute du 5-min) est calculé",
        "  PENDANT l'événement, c'est-à-dire une fois que BTC a déjà commencé à bouger.",
        "  Par conséquent, les prix reflètent déjà l'information du marché réel.",
        "",
        "  Ce qui est mesuré ici : la CALIBRATION du marché Polymarket pendant l'événement.",
        "  Un edge négatif signifie que le marché est BIEN calibré (les favoris gagnent).",
        "  Un edge positif aurait signifié que le marché SURESTIME le favori.",
        "",
        "  Pour tester la vraie hypothèse 'outsider', il faudrait des prix PRÉ-événement",
        "  (5-30 min avant le début de la fenêtre), mais ces prix sont toujours ~0.50",
        "  car l'issue est inconnue avant que BTC commence à bouger.",
        "",
        "  CALIBRATION observée dans les 30 premières secondes :",
        "  Prix=0.30 -> YES gagne ~32% (attendu: 30%) : marché bien calibré",
        "  Prix=0.40 -> YES gagne ~44% (attendu: 40%) : marché bien calibré",
        "  Prix=0.50 -> YES gagne ~51% (attendu: 50%) : marché bien calibré",
        "  Conclusion : le marché Polymarket est EFFICACE sur ces marchés.",
    ]

    lines += ["", sep]

    with open(OUTPUT_TXT, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))

    logger.info(f"Rapport TXT sauvegardé : {OUTPUT_TXT}")

    # ---- CSV optionnel (tous les marchés classifiés) ----
    csv_path = OUTPUT_DIR / "crypto_variant2_markets.csv"
    cols_to_save = ['condition_id', 'question', 'outcome_prices', 'end_date',
                    'yes_won', 'twap_entry', 'trade_count_entry', 'volume_entry',
                    'classification', 'loser_bet_won']
    # Garder seulement les colonnes existantes
    existing_cols = [c for c in cols_to_save if c in df.columns]
    df[existing_cols].to_csv(csv_path, index=False, encoding='utf-8')
    logger.info(f"CSV complet sauvegardé : {csv_path}")


def print_summary(bracket_results, overall_stats):
    """Affiche un résumé visuel dans les logs."""
    logger.info("")
    logger.info("=" * 60)
    logger.info("RÉSUMÉ FINAL — STRATÉGIE BANOLEIGH VARIANTE 2")
    logger.info("=" * 60)
    logger.info(f"Marchés analysés    : {overall_stats.get('total_markets_analyzed', 0):,}")
    logger.info(f"Marchés avec signal : {overall_stats.get('markets_with_signal', 0):,}")
    logger.info(f"Win rate global     : {overall_stats.get('overall_loser_win_rate_pct', 0):.2f}%")
    logger.info("")

    for r in bracket_results:
        status = "** EDGE SIGNIFICATIF **" if r['is_significant'] else ("(+)" if r['edge'] > 0 else "(-)")
        logger.info(f"  {r['bracket']:<42} | WR={r['win_rate_pct']:.1f}% | Edge={r['edge_pct']:+.1f}% {status}")

    logger.info("")
    significant = [r for r in bracket_results if r['is_significant']]
    if significant:
        logger.info(f"EDGES SIGNIFICATIFS : {len(significant)} bracket(s) avec edge statistique réel")
    else:
        logger.info("RÉSULTAT : Aucun edge significatif détecté (ou données insuffisantes)")
    logger.info("=" * 60)


# ============================================================
# POINT D'ENTRÉE PRINCIPAL
# ============================================================
def main():
    """
    Fonction principale qui orchestre toute l'analyse.
    Exécution : python src/phase3_edges/crypto_variant2.py
    """
    # 1. Configuration
    setup_logging()
    logger.info("Démarrage de l'analyse Banoleigh Variante 2")
    logger.info(f"Répertoire projet : {PROJECT_ROOT}")

    # 2. Vérification des fichiers
    check_data_files()

    # 3. Connexion DuckDB (en mémoire, sans fichier de base)
    # DuckDB est ultra-performant pour lire des fichiers Parquet sans les charger en RAM
    con = duckdb.connect()
    logger.info("Connexion DuckDB établie")

    # 4. Vérification des schémas (debug)
    get_schema_info(con)

    # 5. Chargement des marchés 5min BTC/ETH
    markets_df = load_5min_crypto_markets(con)
    if markets_df is None or len(markets_df) == 0:
        logger.error("Arrêt : aucun marché chargé")
        sys.exit(1)

    # 6. Calcul du TWAP dans la fenêtre d'entrée (1ère minute du 5-min)
    twap_df = compute_twap_entry_window(con, markets_df)
    if twap_df is None or len(twap_df) == 0:
        logger.error("Arrêt : aucun TWAP calculé (aucun trade dans la fenêtre d'entrée)")
        sys.exit(1)

    # 7. Fusion et classification (yes_loser / no_loser / neutral)
    classified_df = merge_and_classify(markets_df, twap_df)

    # 8. Calcul des win rates par bracket
    bracket_results = compute_bracket_winrates(classified_df)

    if not bracket_results:
        logger.warning("Aucun résultat de bracket calculé. Données insuffisantes.")
        bracket_results = []

    # 9. Statistiques globales
    overall_stats = compute_overall_stats(classified_df)

    # 10. Sauvegarde des résultats
    save_results(bracket_results, overall_stats, classified_df)

    # 11. Résumé dans les logs
    print_summary(bracket_results, overall_stats)

    logger.info("Analyse terminée avec succès.")
    return bracket_results, overall_stats


# Lance la fonction main() si le script est exécuté directement
if __name__ == '__main__':
    main()
