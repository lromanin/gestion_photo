#!/usr/bin/env python3
"""
photo_common.py — Fonctions partagées entre audit.py et ingest.py.

Regroupe toute la logique commune pour éviter que les deux scripts
dérivent l'un de l'autre au fil des modifications :
  - extraction de date (EXIF, puis nom de fichier, jamais mtime)
  - hash SHA-256
  - cache SQLite (hash + date par chemin/taille/mtime)
  - calcul du chemin cible selon la convention
    AAAA/AAAAMM/AAAAMMJJ/AAAAMMJJ-HHMMSS.ext
"""

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
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

# --- Logging : console uniquement (voir décision du 11/09 : pas de syslog,
# on ne pollue pas le journal système de l'hôte avec les logs applicatifs) ---
DEBUG_MODE = os.getenv("DEBUG", "0") == "1"
LOG_LEVEL = logging.DEBUG if DEBUG_MODE else logging.INFO


def get_logger(name):
    """Retourne un logger nommé, configuré une seule fois (console uniquement)."""
    logger = logging.getLogger(name)
    logger.setLevel(LOG_LEVEL)
    logger.propagate = False  # évite les doublons via un logger parent partagé
    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s"
        )
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


logger = get_logger("gestion_photo")


# --- Cache SQLite (hash + date, indexé par chemin/taille/mtime) ---

def open_cache_db(db_path):
    """
    Ouvre (ou crée) la base SQLite de cache. Indexée par chemin absolu ;
    une entrée n'est réutilisée que si taille ET date de modification
    correspondent encore au fichier sur disque (sinon il est retraité).
    """
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_cache (
            path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime REAL NOT NULL,
            hash TEXT,
            exif_date TEXT,
            date_source TEXT
        )
    """)
    # Index sur le hash pour permettre une recherche rapide "ce hash
    # existe-t-il déjà dans la collection ?" (utilisé par ingest.py).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_cache_hash ON file_cache(hash)")
    conn.commit()
    return conn


def get_cached_entry(conn, path, size, mtime):
    """
    Retourne (hash, exif_date_iso, date_source) depuis le cache si le
    fichier n'a pas changé (taille + mtime identiques), sinon None.
    """
    row = conn.execute(
        "SELECT size, mtime, hash, exif_date, date_source FROM file_cache WHERE path = ?",
        (path,),
    ).fetchone()
    if row is None:
        return None
    cached_size, cached_mtime, cached_hash, cached_exif_date, cached_date_source = row
    if cached_size != size or cached_mtime != mtime:
        return None
    return cached_hash, cached_exif_date, cached_date_source


def store_cache_entry(conn, path, size, mtime, file_hash, exif_date_iso, date_source):
    conn.execute(
        """
        INSERT INTO file_cache (path, size, mtime, hash, exif_date, date_source)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            size=excluded.size, mtime=excluded.mtime, hash=excluded.hash,
            exif_date=excluded.exif_date, date_source=excluded.date_source
        """,
        (path, size, mtime, file_hash, exif_date_iso, date_source),
    )


def delete_cache_entry(conn, path):
    """Supprime une entrée du cache (utile quand un fichier est déplacé)."""
    conn.execute("DELETE FROM file_cache WHERE path = ?", (path,))


def find_cached_path_by_hash(conn, file_hash, path_prefix=None):
    """
    Recherche un chemin déjà connu du cache pour ce hash. Si path_prefix
    est fourni, ne renvoie que les correspondances dont le chemin
    commence par ce préfixe (utile pour ne considérer comme "doublon de
    la collection" que les fichiers déjà présents dans la collection
    rangée, pas dans les dossiers de transit type _a_trier).
    Retourne le premier chemin trouvé, ou None.
    """
    if path_prefix:
        row = conn.execute(
            "SELECT path FROM file_cache WHERE hash = ? AND path LIKE ? LIMIT 1",
            (file_hash, path_prefix.rstrip("/") + "/%"),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT path FROM file_cache WHERE hash = ? LIMIT 1", (file_hash,)
        ).fetchone()
    return row[0] if row else None


# --- Extraction de date : EXIF, puis nom de fichier, jamais mtime ---

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


# Motifs de date reconnus dans les noms de fichiers, testés dans l'ordre
# (le plus précis/spécifique en premier). Chaque motif est associé à un
# booléen indiquant s'il capture aussi l'heure (6 groupes Y,M,D,H,Mi,S)
# ou seulement la date (3 groupes Y,M,D, heure mise à 00:00:00).
FILENAME_DATE_PATTERNS = [
    # AAAAMMJJ-HHMMSS ou AAAAMMJJ_HHMMSS (avec ou sans préfixe :
    # IMG_, VID_, PXL_, Screenshot_, MVIMG_, etc. — le préfixe est ignoré
    # car le motif est cherché n'importe où dans le nom)
    (re.compile(r"(\d{4})(\d{2})(\d{2})[-_](\d{2})(\d{2})(\d{2})"), True),
    # AAAA-MM-JJ-HH-MM-SS / AAAA-MM-JJ_HH.MM.SS / variantes de séparateurs
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})[-_ ](\d{2})[-:.](\d{2})[-:.](\d{2})"), True),
    # WhatsApp : IMG-AAAAMMJJ-WAxxxx / VID-AAAAMMJJ-WAxxxx (date seule,
    # WhatsApp ne conserve pas l'heure dans le nom)
    (re.compile(r"(?:IMG|VID|MVIMG)-(\d{4})(\d{2})(\d{2})-WA\d+", re.IGNORECASE), False),
    # AAAA-MM-JJ (date seule)
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), False),
    # AAAAMMJJ isolé (date seule), en dernier recours — validé strictement
    # (année plausible) pour éviter de confondre avec un numéro de série
    (re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)"), False),
]


def get_filename_date(filepath):
    """
    Tente d'extraire une date depuis le nom du fichier (formats
    appareil/téléphone/WhatsApp courants). Retourne (date_obj, avec_heure)
    où avec_heure indique si l'heure a été trouvée dans le nom, ou
    (None, False) si aucun motif ne correspond ou si la date extraite
    n'est pas plausible.
    """
    name = filepath.name
    current_year = datetime.now().year

    for pattern, has_time in FILENAME_DATE_PATTERNS:
        match = pattern.search(name)
        if not match:
            continue
        try:
            groups = [int(g) for g in match.groups()]
            if has_time:
                year, month, day, hour, minute, second = groups
            else:
                year, month, day = groups
                hour = minute = second = 0

            # Année plausible uniquement (évite de matcher un numéro de
            # série ou un identifiant qui ressemblerait à une date)
            if not (1990 <= year <= current_year + 1):
                continue

            return datetime(year, month, day, hour, minute, second), has_time
        except ValueError:
            # Date invalide (ex: mois 13, jour 32) — motif suivant
            continue

    return None, False


def get_best_date(filepath, exiftool_ok):
    """
    Détermine la meilleure date disponible pour ce fichier, dans l'ordre :
    EXIF, puis nom de fichier, puis aucune (jamais de mtime).
    Retourne (date_obj_ou_None, source) où source est l'une de :
    "exif", "filename_datetime", "filename_date_only", "aucune".
    """
    exif_date = get_exif_date(filepath, exiftool_ok)
    if exif_date is not None:
        return exif_date, "exif"

    filename_date, had_time = get_filename_date(filepath)
    if filename_date is not None:
        return filename_date, "filename_datetime" if had_time else "filename_date_only"

    return None, "aucune"


# --- Hash ---

def compute_sha256(filepath):
    sha256 = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(HASH_CHUNK_SIZE), b""):
                sha256.update(chunk)
        return sha256.hexdigest()
    except OSError as e:
        logger.warning("Impossible de lire %s : %s", filepath, e)
        return None


# --- Convention de nommage/rangement ---

def expected_relative_path(date_obj, extension):
    """
    Construit le chemin relatif attendu selon la convention
    AAAA/AAAAMM/AAAAMMJJ/AAAAMMJJ-HHMMSS.ext
    """
    yyyy = date_obj.strftime("%Y")
    yyyymm = date_obj.strftime("%Y%m")
    yyyymmdd = date_obj.strftime("%Y%m%d")
    yyyymmdd_hhmmss = date_obj.strftime("%Y%m%d-%H%M%S")
    return f"{yyyy}/{yyyymm}/{yyyymmdd}/{yyyymmdd_hhmmss}{extension.lower()}"


# Suffixe de désambiguïsation pour les rafales : plusieurs appareils
# nomment la 1ère photo d'une seconde donnée sans suffixe
# (AAAAMMJJ-HHMMSS.ext), puis ajoutent une ou plusieurs lettres minuscules
# pour les suivantes prises à la même seconde (AAAAMMJJ-HHMMSSa.ext,
# AAAAMMJJ-HHMMSSb.ext...). Ce n'est pas universel (dépend de l'appareil)
# mais assez répandu pour être reconnu comme un nommage valide plutôt que
# signalé comme une erreur.
BURST_SUFFIX_RE = re.compile(r"^[a-z]+$")


def is_expected_filename(actual_path, expected_relative):
    """
    Vérifie si actual_path correspond au chemin cible attendu
    (expected_relative, une chaîne du type "AAAA/AAAAMM/AAAAMMJJ/AAAAMMJJ-HHMMSS.ext"),
    en acceptant un suffixe de rafale (lettres minuscules) sur le nom de
    fichier. Compare uniquement les N derniers éléments du chemin (N =
    profondeur de expected_relative), pas le chemin absolu complet.
    """
    expected = Path(expected_relative)
    expected_dirs = expected.parts[:-1]
    expected_stem = expected.stem
    expected_suffix = expected.suffix.lower()

    actual = Path(actual_path)
    depth = len(expected_dirs) + 1
    actual_relevant_parts = actual.parts[-depth:]
    actual_dirs = actual_relevant_parts[:-1]
    actual_stem = actual.stem
    actual_suffix = actual.suffix.lower()

    if tuple(actual_dirs) != expected_dirs:
        return False
    if actual_suffix != expected_suffix:
        return False
    if actual_stem == expected_stem:
        return True

    # Variante de rafale : même base + suffixe lettres uniquement
    if actual_stem.startswith(expected_stem):
        remainder = actual_stem[len(expected_stem):]
        if BURST_SUFFIX_RE.fullmatch(remainder):
            return True

    return False
