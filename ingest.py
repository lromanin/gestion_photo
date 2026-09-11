#!/usr/bin/env python3
"""
ingest.py — Traite les fichiers de _a_trier/ et les range dans la collection.

Contrairement à audit.py, ce script DÉPLACE des fichiers. Il ne supprime
en revanche jamais rien de lui-même : les doublons détectés sont mis en
quarantaine (jamais effacés), pour validation manuelle ultérieure.

Pour chaque fichier trouvé dans --source :
  1. calcule un hash SHA-256
  2. si ce hash existe déjà dans la collection (--dest) ou a déjà été vu
     plus tôt dans ce même lot : le fichier est un DOUBLON → déplacé vers
     --quarantine, jamais vers la collection, jamais supprimé.
  3. sinon, détermine la date (EXIF, puis nom de fichier, jamais mtime) :
       - date trouvée : renommé et rangé dans --dest selon la convention
         AAAA/AAAAMM/AAAAMMJJ/AAAAMMJJ-HHMMSS.ext
       - aucune date : déplacé tel quel (nom conservé) vers --sans-date-dir
         pour traitement manuel
  4. en cas de collision de nom (fichier différent, même nom cible) : un
     suffixe "-2", "-3"... est ajouté pour ne jamais écraser un fichier
     existant.

Usage :
    python ingest.py --source /data/a_trier --dest /data/photos_et_videos

Prérequis pour une détection de doublons complète :
    La détection de doublons contre la collection déjà rangée s'appuie sur
    le cache SQLite partagé avec audit.py (--cache-db). Si ce cache n'a
    jamais été rempli par un audit.py --scan <dest> complet, les doublons
    ne seront détectés qu'ENTRE les fichiers du lot en cours, pas contre
    la collection existante. Un avertissement est affiché si le cache
    semble vide pour la collection.

Sécurité :
    --dry-run simule le traitement (aucun déplacement réel, aucune
    écriture de cache ou de log) et affiche ce qui serait fait.
"""

import argparse
import csv
import shutil
from datetime import datetime
from pathlib import Path

from photo_common import (
    ALL_EXTENSIONS,
    compute_sha256,
    delete_cache_entry,
    exiftool_available,
    expected_relative_path,
    get_best_date,
    get_logger,
    open_cache_db,
    store_cache_entry,
)

logger = get_logger("gestion_photo.ingest")


def load_known_hashes(cache_conn, dest_root):
    """
    Précharge en mémoire tous les hashs déjà connus pour la collection
    (--dest), depuis le cache persistant. Retourne un dict hash -> chemin.
    Vide si le cache n'a jamais été rempli pour ce répertoire (audit.py
    pas encore lancé sur la collection) — dans ce cas, seuls les doublons
    à l'intérieur du lot en cours seront détectés.
    """
    prefix_pattern = str(dest_root).rstrip("/") + "/%"
    rows = cache_conn.execute(
        "SELECT hash, path FROM file_cache WHERE hash IS NOT NULL AND path LIKE ?",
        (prefix_pattern,),
    ).fetchall()
    return {file_hash: path for file_hash, path in rows}


def resolve_unique_path(desired_path):
    """
    Si desired_path existe déjà, ajoute un suffixe -2, -3... avant
    l'extension jusqu'à trouver un chemin libre. Ne touche jamais à un
    fichier existant.
    """
    if not desired_path.exists():
        return desired_path

    stem = desired_path.stem
    suffix = desired_path.suffix
    parent = desired_path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def append_doublon_log(log_path, quarantine_path, original_path, file_hash):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not log_path.exists()
    with open(log_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow([
                "horodatage", "fichier_en_quarantaine",
                "fichier_original_correspondant", "hash",
            ])
        writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            str(quarantine_path), str(original_path), file_hash,
        ])


def move_and_recache(src_path, dest_path, file_hash, date_obj, date_source,
                      cache_conn, dry_run):
    """
    Déplace src_path vers dest_path (en créant les dossiers nécessaires),
    met à jour le cache SQLite (nouvelle entrée pour dest_path, suppression
    de l'ancienne entrée pour src_path). No-op réel si dry_run.
    """
    if dry_run:
        logger.info("[dry-run] %s -> %s", src_path, dest_path)
        return

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src_path), str(dest_path))

    try:
        new_stat = dest_path.stat()
        store_cache_entry(
            cache_conn, str(dest_path), new_stat.st_size, new_stat.st_mtime,
            file_hash, date_obj.isoformat() if date_obj else None, date_source,
        )
    except OSError as e:
        logger.warning("Impossible de mettre à jour le cache pour %s : %s", dest_path, e)

    delete_cache_entry(cache_conn, str(src_path))
    cache_conn.commit()


def process_file(filepath, dest_root, quarantine_dir, sans_date_dir,
                  known_hashes, cache_conn, doublons_log_path, exiftool_ok, dry_run):
    """
    Traite un fichier unique. Retourne une chaîne indiquant le résultat :
    "ingere", "doublon", "sans_date" ou "erreur".
    """
    file_hash = compute_sha256(filepath)
    if file_hash is None:
        return "erreur"

    # --- Cas 1 : doublon (déjà dans la collection, ou déjà vu dans ce lot) ---
    if file_hash in known_hashes:
        original_path = known_hashes[file_hash]
        quarantine_path = resolve_unique_path(quarantine_dir / filepath.name)
        move_and_recache(filepath, quarantine_path, file_hash, None, "aucune",
                          cache_conn, dry_run)
        if not dry_run:
            append_doublon_log(doublons_log_path, quarantine_path, original_path, file_hash)
        logger.info("DOUBLON : %s (identique à %s) -> %s",
                     filepath, original_path, quarantine_path)
        return "doublon"

    # --- Cas 2 : date trouvée -> rangement selon la convention ---
    date_obj, date_source = get_best_date(filepath, exiftool_ok)

    if date_obj is not None:
        expected_rel = expected_relative_path(date_obj, filepath.suffix)
        target_path = dest_root / expected_rel

        if target_path.exists():
            existing_hash = compute_sha256(target_path)
            if existing_hash == file_hash:
                # Doublon exact non détecté par le cache (rare : cache pas
                # à jour) — traité comme un doublon, jamais écrasé.
                quarantine_path = resolve_unique_path(quarantine_dir / filepath.name)
                move_and_recache(filepath, quarantine_path, file_hash, None,
                                  "aucune", cache_conn, dry_run)
                if not dry_run:
                    append_doublon_log(doublons_log_path, quarantine_path,
                                        target_path, file_hash)
                logger.info("DOUBLON (détecté à la collision) : %s -> %s",
                             filepath, quarantine_path)
                return "doublon"
            else:
                # Même nom cible, contenu différent (ex: rafale à la même
                # seconde) -> suffixe pour ne jamais écraser l'existant.
                target_path = resolve_unique_path(target_path)

        move_and_recache(filepath, target_path, file_hash, date_obj,
                          date_source, cache_conn, dry_run)
        known_hashes[file_hash] = str(target_path)
        logger.info("INGÉRÉ : %s -> %s", filepath, target_path)
        return "ingere"

    # --- Cas 3 : aucune date trouvée -> _sans_date_exif/, nom conservé ---
    target_path = resolve_unique_path(sans_date_dir / filepath.name)
    move_and_recache(filepath, target_path, file_hash, None, "aucune",
                      cache_conn, dry_run)
    known_hashes[file_hash] = str(target_path)
    logger.info("SANS DATE : %s -> %s (traitement manuel requis)", filepath, target_path)
    return "sans_date"


def ingest_directory(source_dirs, dest_root, quarantine_dir, sans_date_dir,
                      cache_conn, doublons_log_path, exiftool_ok, dry_run):
    known_hashes = load_known_hashes(cache_conn, dest_root)
    if not known_hashes:
        logger.warning(
            "Le cache ne contient aucun hash connu pour %s : la détection "
            "de doublons contre la collection existante ne fonctionnera "
            "pas (seuls les doublons à l'intérieur de ce lot seront "
            "détectés). Lancer audit.py --scan %s au moins une fois pour "
            "peupler le cache.", dest_root, dest_root,
        )

    counts = {"ingere": 0, "doublon": 0, "sans_date": 0, "erreur": 0}

    for source_dir in source_dirs:
        source_path = Path(source_dir)
        if not source_path.exists():
            logger.warning("Répertoire source introuvable, ignoré : %s", source_dir)
            continue

        for filepath in sorted(source_path.rglob("*")):
            if not filepath.is_file():
                continue
            if filepath.suffix.lower() not in ALL_EXTENSIONS:
                continue

            try:
                result = process_file(
                    filepath, dest_root, quarantine_dir, sans_date_dir,
                    known_hashes, cache_conn, doublons_log_path, exiftool_ok, dry_run,
                )
                counts[result] += 1
            except OSError as e:
                logger.error("Erreur en traitant %s : %s", filepath, e)
                counts["erreur"] += 1

    return counts


def print_summary(counts, dry_run):
    print("\n--- Résumé de l'ingestion" + (" (dry-run, rien n'a été modifié)" if dry_run else "") + " ---")
    print(f"Ingérés (rangés)   : {counts['ingere']}")
    print(f"Doublons           : {counts['doublon']}")
    print(f"Sans date          : {counts['sans_date']}")
    print(f"Erreurs            : {counts['erreur']}")


def main():
    parser = argparse.ArgumentParser(
        description="Ingestion des fichiers de _a_trier vers la collection rangée."
    )
    parser.add_argument(
        "--source", nargs="+", default=["/data/a_trier"],
        help="Un ou plusieurs répertoires source à traiter (défaut: /data/a_trier).",
    )
    parser.add_argument(
        "--dest", default="/data/photos_et_videos",
        help="Racine de la collection rangée (défaut: /data/photos_et_videos).",
    )
    parser.add_argument(
        "--quarantine", default="/data/doublons_detectes",
        help="Répertoire de quarantaine pour les doublons (défaut: /data/doublons_detectes).",
    )
    parser.add_argument(
        "--sans-date-dir", default="/data/sans_date_exif",
        help="Répertoire pour les fichiers sans date exploitable (défaut: /data/sans_date_exif).",
    )
    parser.add_argument(
        "--cache-db", default="/data/app_data/hash_cache.db",
        help="Base SQLite de cache, partagée avec audit.py (défaut: /data/app_data/hash_cache.db).",
    )
    parser.add_argument(
        "--doublons-log", default="/data/app_data/rapports/doublons_log.csv",
        help="Fichier CSV listant les doublons détectés (défaut: /data/app_data/rapports/doublons_log.csv).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simule le traitement sans déplacer aucun fichier ni modifier le cache.",
    )
    args = parser.parse_args()

    dest_root = Path(args.dest)
    quarantine_dir = Path(args.quarantine)
    sans_date_dir = Path(args.sans_date_dir)
    doublons_log_path = Path(args.doublons_log)

    logger.info(
        "Ingestion lancée — source: %s | dest: %s%s",
        ", ".join(args.source), args.dest, " [DRY-RUN]" if args.dry_run else "",
    )

    exiftool_ok = exiftool_available()
    if not exiftool_ok:
        logger.warning(
            "exiftool n'est pas trouvé sur ce système. "
            "Le script tentera d'extraire la date depuis le nom du fichier. "
            "Pour installer exiftool : "
            "Debian/Ubuntu : sudo apt install libimage-exiftool-perl | https://exiftool.org/"
        )

    cache_conn = open_cache_db(args.cache_db)
    logger.info("Cache utilisé : %s", args.cache_db)

    if not args.dry_run:
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        sans_date_dir.mkdir(parents=True, exist_ok=True)

    counts = ingest_directory(
        args.source, dest_root, quarantine_dir, sans_date_dir,
        cache_conn, doublons_log_path, exiftool_ok, args.dry_run,
    )
    cache_conn.close()

    print_summary(counts, args.dry_run)
    logger.info(
        "Ingestion terminée — ingérés: %d | doublons: %d | sans_date: %d | erreurs: %d",
        counts["ingere"], counts["doublon"], counts["sans_date"], counts["erreur"],
    )


if __name__ == "__main__":
    main()
