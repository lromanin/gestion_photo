#!/usr/bin/env python3
"""
dedupe_collection.py — Détecte et met en quarantaine les doublons DÉJÀ
présents dans la collection rangée (--root, par défaut photos_et_videos).

Contexte : audit.py et ingest.py empêchent les nouveaux doublons d'entrer
dans la collection, mais ne corrigent pas ceux qui y sont déjà (par
exemple issus d'un ancien script qui traitait par erreur deux copies
identiques comme une rafale légitime : 20230728-191553.jpg et
20230728-191553a.jpg, alors qu'il s'agit du même contenu).

Pour chaque groupe de fichiers partageant le même hash SHA-256 :
  1. un exemplaire est désigné comme "gardé en place" :
       - priorité au fichier dont le nom est "nu" (AAAAMMJJ-HHMMSS.ext,
         sans lettre de rafale ni suffixe), s'il en existe un dans le
         groupe
       - à défaut (aucun nom nu dans le groupe — cas rare), le premier
         par ordre alphabétique de chemin est gardé, de façon
         déterministe
  2. tous les autres exemplaires du groupe sont déplacés vers
     --quarantine (jamais supprimés), avec une entrée dans
     --doublons-log référençant le fichier gardé

Usage :
    python dedupe_collection.py --root /data/photos_et_videos

Performance :
    S'appuie sur le cache SQLite partagé (--cache-db) pour éviter de
    rehasher les fichiers déjà connus et inchangés — indispensable sur
    une collection de plusieurs dizaines de milliers de fichiers.

Sécurité :
    --dry-run simule le traitement (aucun déplacement réel, aucune
    écriture de cache ou de log) et affiche ce qui serait fait.
"""

import argparse
from pathlib import Path

from photo_common import (
    append_doublon_log,
    exiftool_available,
    get_cached_or_compute,
    get_logger,
    is_bare_name,
    move_and_recache,
    open_cache_db,
    resolve_unique_path,
    walk_media_files,
)

logger = get_logger("gestion_photo.dedupe")


def choose_keeper(paths):
    """
    Choisit quel fichier garder en place parmi un groupe de doublons
    (même hash). Priorité au nom "nu" (AAAAMMJJ-HHMMSS.ext) s'il existe ;
    sinon, premier par ordre alphabétique de chemin (déterministe).
    """
    bare_candidates = sorted(p for p in paths if is_bare_name(p))
    if bare_candidates:
        return bare_candidates[0]
    return sorted(paths)[0]


def find_duplicate_groups(root_dirs, cache_conn, exiftool_ok):
    """
    Parcourt les répertoires donnés, groupe les fichiers par hash.
    Retourne un dict hash -> liste de Path, uniquement pour les groupes
    de 2 fichiers ou plus (les fichiers uniques ne nous intéressent pas
    ici). Les fichiers illisibles sont ignorés (avec avertissement).
    """
    hash_to_paths = {}
    total_files = 0

    for root_dir in root_dirs:
        root_path = Path(root_dir)
        if not root_path.exists():
            logger.warning("Répertoire introuvable, ignoré : %s", root_dir)
            continue

        for filepath in walk_media_files(root_path):
            if not filepath.is_file():
                continue

            total_files += 1
            if total_files % 500 == 0:
                logger.info("... %d fichiers analysés", total_files)

            file_hash, _date_obj, date_source, _cache_hit = get_cached_or_compute(
                filepath, cache_conn, exiftool_ok,
            )
            if file_hash is None or date_source == "erreur":
                continue

            hash_to_paths.setdefault(file_hash, []).append(filepath)

        cache_conn.commit()

    logger.info("Analyse terminée — %d fichier(s) au total", total_files)
    return {h: paths for h, paths in hash_to_paths.items() if len(paths) > 1}


def resolve_duplicate_group(file_hash, paths, quarantine_dir, doublons_log_path,
                             cache_conn, dry_run):
    """
    Traite un groupe de doublons : garde un exemplaire en place, déplace
    les autres en quarantaine. Retourne le nombre de fichiers déplacés.
    """
    keeper = choose_keeper(paths)
    others = [p for p in paths if p != keeper]

    logger.info(
        "Groupe de %d doublons (hash %s...) — gardé : %s",
        len(paths), file_hash[:12], keeper,
    )

    moved = 0
    for duplicate_path in others:
        quarantine_path = resolve_unique_path(quarantine_dir / duplicate_path.name)
        try:
            move_and_recache(
                duplicate_path, quarantine_path, file_hash, None, "aucune",
                cache_conn, dry_run,
            )
        except OSError as e:
            logger.error("Erreur en déplaçant %s : %s", duplicate_path, e)
            continue

        if not dry_run:
            append_doublon_log(doublons_log_path, quarantine_path, keeper, file_hash)
        logger.info("  DOUBLON : %s -> %s", duplicate_path, quarantine_path)
        moved += 1

    return moved


def main():
    parser = argparse.ArgumentParser(
        description="Détecte et met en quarantaine les doublons déjà présents dans la collection rangée."
    )
    parser.add_argument(
        "--root", nargs="+", default=["/data/photos_et_videos"],
        help="Un ou plusieurs répertoires à dédupliquer (défaut: /data/photos_et_videos).",
    )
    parser.add_argument(
        "--quarantine", default="/data/doublons_detectes",
        help="Répertoire de quarantaine pour les doublons (défaut: /data/doublons_detectes).",
    )
    parser.add_argument(
        "--cache-db", default="/data/app_data/hash_cache.db",
        help="Base SQLite de cache, partagée avec audit.py/ingest.py (défaut: /data/app_data/hash_cache.db).",
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

    quarantine_dir = Path(args.quarantine)
    doublons_log_path = Path(args.doublons_log)

    logger.info(
        "Dédoublonnage lancé — root: %s%s",
        ", ".join(args.root), " [DRY-RUN]" if args.dry_run else "",
    )

    exiftool_ok = exiftool_available()
    if not exiftool_ok:
        logger.warning(
            "exiftool n'est pas trouvé sur ce système. "
            "Sans impact direct ici (seule la date est concernée, pas le "
            "hash), mais les nouvelles entrées de cache seront moins "
            "précises pour la date."
        )

    cache_conn = open_cache_db(args.cache_db)
    logger.info("Cache utilisé : %s", args.cache_db)

    if not args.dry_run:
        quarantine_dir.mkdir(parents=True, exist_ok=True)

    duplicate_groups = find_duplicate_groups(args.root, cache_conn, exiftool_ok)

    total_moved = 0
    for file_hash, paths in duplicate_groups.items():
        total_moved += resolve_duplicate_group(
            file_hash, paths, quarantine_dir, doublons_log_path, cache_conn, args.dry_run,
        )

    cache_conn.close()

    print("\n--- Résumé du dédoublonnage" + (" (dry-run, rien n'a été modifié)" if args.dry_run else "") + " ---")
    print(f"Groupes de doublons trouvés : {len(duplicate_groups)}")
    print(f"Fichiers déplacés en quarantaine : {total_moved}")
    logger.info(
        "Dédoublonnage terminé — groupes: %d | fichiers déplacés: %d",
        len(duplicate_groups), total_moved,
    )


if __name__ == "__main__":
    main()
