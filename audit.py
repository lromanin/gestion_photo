#!/usr/bin/env python3
"""
audit.py — Scan en LECTURE SEULE de répertoires photo/vidéo.

Ne déplace, ne renomme, ne supprime RIEN. Produit un rapport CSV.

Pour chaque fichier trouvé :
  - calcule un hash SHA-256 (détection de doublons stricts)
  - extrait la date de prise de vue (EXIF via exiftool, sinon fallback)
  - calcule le chemin/nom "cible" attendu selon la convention
    AAAA/AAAAMMJJ/AAAAMMJJ-HHMMSS.ext
  - détermine un statut : ok / doublon / sans_exif / mal_nomme

Usage :
    python audit.py --scan /mnt/pool/Photos /mnt/pool/_a_trier \
                     --output rapport_audit.csv

Prérequis :
    - Python 3.8+
    - exiftool installé sur le système (recommandé). Le script fonctionne
      sans, mais bascule alors sur la date de modification du fichier pour
      TOUS les fichiers (moins fiable), et le signale dans le rapport.
"""

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Extensions prises en compte (on élargit large : photos + RAW + vidéos)
PHOTO_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".webp",
    ".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp", ".mts", ".m2ts",
}
ALL_EXTENSIONS = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS

# Tags EXIF/QuickTime à essayer, dans l'ordre de préférence
EXIFTOOL_DATE_TAGS = [
    "DateTimeOriginal",
    "CreateDate",
    "MediaCreateDate",
    "TrackCreateDate",
]

HASH_CHUNK_SIZE = 1024 * 1024  # 1 Mo


def exiftool_available():
    return shutil.which("exiftool") is not None


def get_exif_date(filepath, exiftool_ok):
    """
    Retourne un objet datetime pour la date de prise de vue, ou None si
    introuvable / pas d'exiftool disponible.
    """
    if not exiftool_ok:
        return None

    try:
        result = subprocess.run(
            ["exiftool", "-json", "-DateTimeOriginal", "-CreateDate",
             "-MediaCreateDate", "-TrackCreateDate", str(filepath)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        if not data:
            return None

        info = data[0]
        for tag in EXIFTOOL_DATE_TAGS:
            raw = info.get(tag)
            if not raw:
                continue
            # Format exiftool typique : "2024:06:12 14:30:22" (parfois avec
            # un fuseau horaire en suffixe, qu'on tronque volontairement)
            raw_clean = raw.strip()
            try:
                return datetime.strptime(raw_clean[:19], "%Y:%m:%d %H:%M:%S")
            except ValueError:
                continue
        return None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def get_fallback_date(filepath):
    """Date de modification du fichier, en dernier recours."""
    try:
        return datetime.fromtimestamp(filepath.stat().st_mtime)
    except OSError:
        return None


def compute_sha256(filepath):
    sha256 = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(HASH_CHUNK_SIZE), b""):
                sha256.update(chunk)
        return sha256.hexdigest()
    except OSError as e:
        print(f"  [!] Impossible de lire {filepath} : {e}", file=sys.stderr)
        return None


def expected_relative_path(date_obj, extension):
    """
    Construit le chemin relatif attendu selon la convention
    AAAA/AAAAMMJJ/AAAAMMJJ-HHMMSS.ext
    """
    yyyy = date_obj.strftime("%Y")
    yyyymmdd = date_obj.strftime("%Y%m%d")
    yyyymmdd_hhmmss = date_obj.strftime("%Y%m%d-%H%M%S")
    return f"{yyyy}/{yyyymmdd}/{yyyymmdd_hhmmss}{extension.lower()}"


def scan_directories(scan_dirs, exiftool_ok):
    """
    Parcourt les répertoires donnés, retourne une liste de dicts décrivant
    chaque fichier trouvé.
    """
    entries = []
    total_files = 0

    for scan_dir in scan_dirs:
        scan_path = Path(scan_dir)
        if not scan_path.exists():
            print(f"[!] Répertoire introuvable, ignoré : {scan_dir}", file=sys.stderr)
            continue

        for filepath in scan_path.rglob("*"):
            if not filepath.is_file():
                continue
            if filepath.suffix.lower() not in ALL_EXTENSIONS:
                continue

            total_files += 1
            print(f"  [{total_files}] {filepath}")

            exif_date = get_exif_date(filepath, exiftool_ok)
            date_source = "exif"
            date_obj = exif_date
            if date_obj is None:
                date_obj = get_fallback_date(filepath)
                date_source = "mtime_fallback"

            file_hash = compute_sha256(filepath)

            expected_rel = None
            if date_obj is not None:
                expected_rel = expected_relative_path(date_obj, filepath.suffix)

            try:
                size = filepath.stat().st_size
            except OSError:
                size = None

            entries.append({
                "chemin": str(filepath),
                "taille_octets": size,
                "date_utilisee": date_obj.isoformat() if date_obj else None,
                "source_date": date_source if date_obj else "aucune",
                "hash_sha256": file_hash,
                "chemin_cible_attendu": expected_rel,
            })

    print(f"\nTotal fichiers scannés : {total_files}")
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
            # On compare juste la fin du chemin (nom + 2 niveaux de dossiers)
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
        "--output", default="rapport_audit.csv",
        help="Chemin du fichier CSV de sortie (défaut: rapport_audit.csv).",
    )
    args = parser.parse_args()

    exiftool_ok = exiftool_available()
    if not exiftool_ok:
        print(
            "[!] exiftool n'est pas trouvé sur ce système.\n"
            "    Toutes les dates utilisées seront des dates de modification\n"
            "    de fichier (moins fiable que l'EXIF). Pour l'installer :\n"
            "      - Debian/Ubuntu : sudo apt install libimage-exiftool-perl\n"
            "      - ou voir https://exiftool.org/\n",
            file=sys.stderr,
        )

    print(f"Scan de : {', '.join(args.scan)}\n")
    entries = scan_directories(args.scan, exiftool_ok)
    entries = annotate_status(entries)
    write_csv_report(entries, args.output)
    print_summary(entries)
    print(f"\nRapport écrit dans : {args.output}")


if __name__ == "__main__":
    main()
