#!/usr/bin/env python3
"""
audit.py — Scan en LECTURE SEULE de répertoires photo/vidéo.

Ne déplace, ne renomme, ne supprime RIEN. Produit un rapport CSV.

Pour chaque fichier trouvé :
  - calcule un hash SHA-256 (détection de doublons stricts)
  - extrait la date de prise de vue : EXIF (via exiftool) en priorité,
    sinon tentative d'extraction depuis le nom du fichier (formats
    appareil/téléphone/WhatsApp courants), sinon aucune date
  - calcule le chemin/nom "cible" attendu selon la convention
    AAAA/AAAAMM/AAAAMMJJ/AAAAMMJJ-HHMMSS.ext
  - détermine un statut : ok / doublon / sans_exif / mal_nomme

Usage :
    python audit.py --scan /data/photos_et_videos /data/a_trier \
                     --output /data/app_data/rapports/rapport_audit.csv

Cache :
    Un cache SQLite (--cache-db, défaut /data/app_data/hash_cache.db,
    sur le volume persistant) retient hash et date de chaque fichier
    déjà traité, indexés par (chemin, taille, date de modification). Les
    fichiers inchangés depuis le dernier scan ne sont pas relus ni
    re-hashés. Ce cache est partagé avec ingest.py (voir photo_common.py).

Logging :
    Logs affichés en console (utile en exécution interactive via
    docker exec). DEBUG=1 en variable d'environnement active les logs de
    niveau DEBUG.

Prérequis :
    - Python 3.8+
    - exiftool installé sur le système (recommandé). Sans exiftool, le
      script tente d'extraire la date depuis le nom du fichier ; si ça
      échoue aussi, le fichier reste sans date (statut sans_exif),
      jamais de date approximative silencieuse via mtime.
"""

import argparse
import csv
from datetime import datetime
from pathlib import Path

from photo_common import (
    ALL_EXTENSIONS,
    compute_sha256,
    exiftool_available,
    expected_relative_path,
    get_best_date,
    get_cached_entry,
    get_logger,
    open_cache_db,
    store_cache_entry,
)

logger = get_logger("gestion_photo.audit")


def scan_directories(scan_dirs, exiftool_ok, cache_conn):
    """
    Parcourt les répertoires donnés, retourne une liste de dicts décrivant
    chaque fichier trouvé. Réutilise le cache pour les fichiers inchangés.
    """
    entries = []
    total_files = 0
    cache_hits = 0
    cache_misses = 0

    for scan_dir in scan_dirs:
        scan_path = Path(scan_dir)
        if not scan_path.exists():
            logger.warning("Répertoire introuvable, ignoré : %s", scan_dir)
            continue

        for filepath in scan_path.rglob("*"):
            if not filepath.is_file():
                continue
            if filepath.suffix.lower() not in ALL_EXTENSIONS:
                continue

            total_files += 1
            path_str = str(filepath)

            try:
                stat = filepath.stat()
                size = stat.st_size
                mtime = stat.st_mtime
            except OSError as e:
                logger.warning("Impossible d'accéder à %s : %s", filepath, e)
                continue

            cached = get_cached_entry(cache_conn, path_str, size, mtime)

            if cached is not None:
                cache_hits += 1
                file_hash, exif_date_iso, date_source = cached
                date_obj = datetime.fromisoformat(exif_date_iso) if exif_date_iso else None
            else:
                cache_misses += 1
                date_obj, date_source = get_best_date(filepath, exiftool_ok)
                file_hash = compute_sha256(filepath)

                store_cache_entry(
                    cache_conn, path_str, size, mtime, file_hash,
                    date_obj.isoformat() if date_obj else None, date_source,
                )

            logger.debug("Fichier [%d] : %s", total_files, filepath)
            if total_files % 50 == 0:
                logger.info(
                    "... %d fichiers traités (cache: %d réutilisés, %d recalculés)",
                    total_files, cache_hits, cache_misses,
                )

            expected_rel = None
            if date_obj is not None:
                expected_rel = expected_relative_path(date_obj, filepath.suffix)

            entries.append({
                "chemin": path_str,
                "taille_octets": size,
                "date_utilisee": date_obj.isoformat() if date_obj else None,
                "source_date": date_source if date_obj else "aucune",
                "hash_sha256": file_hash,
                "chemin_cible_attendu": expected_rel,
            })

        # Commit périodique par répertoire scanné pour ne pas perdre le
        # travail déjà fait en cas d'interruption sur une longue collection.
        cache_conn.commit()

    logger.info(
        "Scan terminé — %d fichier(s) parcouru(s) (cache: %d réutilisés, %d recalculés)",
        total_files, cache_hits, cache_misses,
    )
    return entries


def annotate_status(entries):
    """
    Ajoute un champ 'statut' à chaque entrée :
      - doublon        : hash déjà vu ailleurs dans le scan
      - sans_exif       : aucune date EXIF trouvée (fallback utilisé ou aucune date)
      - mal_nomme       : le chemin ne correspond pas au chemin cible attendu
      - ok              : rien à signaler
    Un fichier peut cumuler plusieurs statuts (séparés par '+').
    """
    hash_to_paths = {}
    for e in entries:
        if e["hash_sha256"]:
            hash_to_paths.setdefault(e["hash_sha256"], []).append(e["chemin"])

    for e in entries:
        statuts = []

        if e["hash_sha256"] and len(hash_to_paths[e["hash_sha256"]]) > 1:
            statuts.append("doublon")

        if e["source_date"] != "exif":
            statuts.append("sans_exif")

        if e["chemin_cible_attendu"]:
            chemin_actuel = Path(e["chemin"])
            # On compare juste la fin du chemin (nom + 3 niveaux de dossiers)
            attendu_parts = Path(e["chemin_cible_attendu"]).parts
            actuel_parts = chemin_actuel.parts[-len(attendu_parts):]
            if tuple(actuel_parts) != attendu_parts:
                statuts.append("mal_nomme")

        e["statut"] = "+".join(statuts) if statuts else "ok"

        # Pratique pour le tri dans le CSV : liste des autres fichiers
        # partageant le même hash (vide si pas de doublon)
        if e["hash_sha256"]:
            autres = [p for p in hash_to_paths[e["hash_sha256"]] if p != e["chemin"]]
            e["doublons_avec"] = " | ".join(autres)
        else:
            e["doublons_avec"] = ""

    return entries


def write_csv_report(entries, output_path):
    fieldnames = [
        "chemin", "statut", "taille_octets", "date_utilisee", "source_date",
        "chemin_cible_attendu", "hash_sha256", "doublons_avec",
    ]
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for e in entries:
            writer.writerow(e)


def print_summary(entries):
    total = len(entries)
    doublons = sum(1 for e in entries if "doublon" in e["statut"])
    sans_exif = sum(1 for e in entries if "sans_exif" in e["statut"])
    mal_nommes = sum(1 for e in entries if "mal_nomme" in e["statut"])
    ok = sum(1 for e in entries if e["statut"] == "ok")

    print("\n--- Résumé ---")
    print(f"Total fichiers        : {total}")
    print(f"OK (rien à signaler)  : {ok}")
    print(f"Doublons détectés     : {doublons}")
    print(f"Sans date EXIF        : {sans_exif}")
    print(f"Mal nommés/mal placés : {mal_nommes}")


def main():
    parser = argparse.ArgumentParser(
        description="Audit en lecture seule des répertoires photo/vidéo."
    )
    parser.add_argument(
        "--scan", nargs="+", required=True,
        help="Un ou plusieurs répertoires à scanner (récursivement).",
    )
    parser.add_argument(
        "--output", default="/data/app_data/rapports/rapport_audit.csv",
        help="Chemin du fichier CSV de sortie (défaut: /data/app_data/rapports/rapport_audit.csv).",
    )
    parser.add_argument(
        "--cache-db", default="/data/app_data/hash_cache.db",
        help=(
            "Chemin de la base SQLite de cache des hashs/dates EXIF "
            "(défaut: /data/app_data/hash_cache.db, sur le volume "
            "persistant). Les fichiers inchangés depuis le dernier scan "
            "ne sont pas relus."
        ),
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Ignore et ne met pas à jour le cache : tout est recalculé.",
    )
    args = parser.parse_args()

    logger.info("Audit lancé — scan: %s | output: %s", ", ".join(args.scan), args.output)

    exiftool_ok = exiftool_available()
    if not exiftool_ok:
        logger.warning(
            "exiftool n'est pas trouvé sur ce système. "
            "Le script tentera d'extraire la date depuis le nom du fichier. "
            "Pour installer exiftool : "
            "Debian/Ubuntu : sudo apt install libimage-exiftool-perl | https://exiftool.org/"
        )

    cache_db_path = ":memory:" if args.no_cache else args.cache_db
    cache_conn = open_cache_db(cache_db_path)
    if args.no_cache:
        logger.info("--no-cache activé : cache ignoré pour cette exécution.")
    else:
        logger.info("Cache utilisé : %s", args.cache_db)

    logger.info("Scan de : %s", ", ".join(args.scan))
    entries = scan_directories(args.scan, exiftool_ok, cache_conn)
    cache_conn.close()

    entries = annotate_status(entries)
    write_csv_report(entries, args.output)
    print_summary(entries)
    logger.info("Rapport écrit dans : %s", args.output)
    logger.info("Audit terminé — %d entrée(s) exportée(es)", len(entries))


if __name__ == "__main__":
    main()
