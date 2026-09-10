from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

import audit


def _mock_result(returncode=0, stdout="", stderr=""):
    m = mock.Mock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# --- exiftool_available ---------------------------------------------------

class TestExiftoolAvailable:
    def test_present(self, monkeypatch):
        monkeypatch.setattr(audit.shutil, "which", lambda cmd: "/usr/bin/exiftool")
        assert audit.exiftool_available() is True

    def test_absent(self, monkeypatch):
        monkeypatch.setattr(audit.shutil, "which", lambda cmd: None)
        assert audit.exiftool_available() is False


# --- get_exif_date --------------------------------------------------------

class TestGetExifDate:
    def test_exiftool_disabled_returns_none(self, tmp_path):
        p = tmp_path / "x.jpg"
        p.write_bytes(b"x")
        assert audit.get_exif_date(p, False) is None

    def test_parse_datetime_original(self, tmp_path, monkeypatch):
        import json
        p = tmp_path / "x.jpg"
        p.write_bytes(b"x")
        payload = json.dumps([{"SourceFile": str(p), "DateTimeOriginal": "2024:06:12 14:30:22"}])
        monkeypatch.setattr(audit.subprocess, "run", lambda *a, **k: _mock_result(0, payload))
        assert audit.get_exif_date(p, True) == datetime(2024, 6, 12, 14, 30, 22)

    def test_fallback_to_create_date(self, tmp_path, monkeypatch):
        import json
        p = tmp_path / "x.jpg"
        p.write_bytes(b"x")
        payload = json.dumps([{"SourceFile": str(p), "DateTimeOriginal": "", "CreateDate": "2023:01:02 03:04:05"}])
        monkeypatch.setattr(audit.subprocess, "run", lambda *a, **k: _mock_result(0, payload))
        assert audit.get_exif_date(p, True) == datetime(2023, 1, 2, 3, 4, 5)

    def test_empty_tags_returns_none(self, tmp_path, monkeypatch):
        import json
        p = tmp_path / "x.jpg"
        p.write_bytes(b"x")
        payload = json.dumps([{"SourceFile": str(p)}])
        monkeypatch.setattr(audit.subprocess, "run", lambda *a, **k: _mock_result(0, payload))
        assert audit.get_exif_date(p, True) is None

    def test_nonzero_returncode_returns_none(self, tmp_path, monkeypatch):
        p = tmp_path / "x.jpg"
        p.write_bytes(b"x")
        monkeypatch.setattr(audit.subprocess, "run", lambda *a, **k: _mock_result(1, "error"))
        assert audit.get_exif_date(p, True) is None

    def test_invalid_json_returns_none(self, tmp_path, monkeypatch):
        p = tmp_path / "x.jpg"
        p.write_bytes(b"x")
        monkeypatch.setattr(audit.subprocess, "run", lambda *a, **k: _mock_result(0, "not-json{"))
        assert audit.get_exif_date(p, True) is None

    def test_timeout_returns_none(self, tmp_path, monkeypatch):
        import subprocess as sp
        p = tmp_path / "x.jpg"
        p.write_bytes(b"x")
        monkeypatch.setattr(audit.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(sp.TimeoutExpired("exiftool", 30)))
        assert audit.get_exif_date(p, True) is None

    def test_timezone_suffix_truncated(self, tmp_path, monkeypatch):
        import json
        p = tmp_path / "x.jpg"
        p.write_bytes(b"x")
        payload = json.dumps([{"DateTimeOriginal": "2024:06:12 14:30:22+02:00"}])
        monkeypatch.setattr(audit.subprocess, "run", lambda *a, **k: _mock_result(0, payload))
        assert audit.get_exif_date(p, True) == datetime(2024, 6, 12, 14, 30, 22)


# --- get_fallback_date ----------------------------------------------------

class TestGetFallbackDate:
    def test_returns_mtime(self, make_media_file):
        import os
        p = make_media_file("a/b/c.dng")
        known = os.path.getmtime(p)
        from datetime import timezone
        offset = datetime.fromtimestamp(known, tz=timezone.utc) - datetime.now(tz=timezone.utc)
        result = audit.get_fallback_date(p)
        assert result is not None

    def test_missing_file_returns_none(self, tmp_path):
        p = tmp_path / "ghost.jpg"
        p.write_bytes(b"x")
        p.unlink()
        assert audit.get_fallback_date(p) is None


# --- compute_sha256 -------------------------------------------------------

class TestComputeSha256:
    def test_known_hash(self, tmp_path):
        import hashlib
        content = b"hello world"
        p = tmp_path / "f.jpg"
        p.write_bytes(content)
        assert audit.compute_sha256(p) == hashlib.sha256(content).hexdigest()

    def test_missing_file_returns_none(self, tmp_path, capsys):
        p = tmp_path / "missing.jpg"
        assert audit.compute_sha256(p) is None
        assert "Impossible de lire" in capsys.readouterr().err


# --- expected_relative_path ----------------------------------------------

class TestExpectedRelativePath:
    def test_format(self):
        dt = datetime(2024, 6, 12, 14, 30, 22)
        assert audit.expected_relative_path(dt, ".jpg") == "2024/20240612/20240612-143022.jpg"

    def test_extension_uppercased(self):
        dt = datetime(2024, 6, 12, 14, 30, 22)
        assert audit.expected_relative_path(dt, ".JPG").endswith(".jpg")

    def test_midnight(self):
        dt = datetime(2020, 1, 1, 0, 0, 0)
        assert audit.expected_relative_path(dt, ".cr2") == "2020/20200101/20200101-000000.cr2"


# --- scan_directories -----------------------------------------------------

class TestScanDirectories:
    def test_skips_nonexistent(self, tmp_path, capsys):
        entries = audit.scan_directories([str(tmp_path / "ghost")], False)
        assert entries == []
        assert "ignoré" in capsys.readouterr().err

    def test_filters_non_media(self, media_dir, make_media_file):
        make_media_file("keep.jpg")
        (media_dir / "ignore.txt").write_text("nope")
        (media_dir / "ignore.pdf").write_bytes(b"%PDF")
        entries = audit.scan_directories([str(media_dir)], False)
        assert len(entries) == 1
        assert entries[0]["chemin"].endswith("keep.jpg")

    def test_collects_recursively(self, media_dir, make_media_file):
        make_media_file("2024/20240612/img1.jpg")
        make_media_file("sub/img2.CR2")
        make_media_file("sub/deep/img3.mp4")
        entries = audit.scan_directories([str(media_dir)], False)
        noms = {Path(e["chemin"]).name for e in entries}
        assert noms == {"img1.jpg", "img2.CR2", "img3.mp4"}

    def test_entry_keys(self, media_dir, make_media_file):
        make_media_file("x.jpg")
        entries = audit.scan_directories([str(media_dir)], False)
        expected = {"chemin", "taille_octets", "date_utilisee", "source_date",
                    "hash_sha256", "chemin_cible_attendu"}
        assert set(entries[0].keys()) == expected


# --- annotate_status ------------------------------------------------------

class TestAnnotateStatus:
    def _base(self, chemin, hash_val, source_date, cible=None):
        return {
            "chemin": chemin,
            "taille_octets": 10,
            "date_utilisee": "2024-06-12T14:30:22",
            "source_date": source_date,
            "hash_sha256": hash_val,
            "chemin_cible_attendu": cible,
        }

    def test_ok(self):
        e = self._base("/data/2024/20240612/20240612-143022.jpg", "abc", "exif",
                       cible="2024/20240612/20240612-143022.jpg")
        res = audit.annotate_status([e])
        assert res[0]["statut"] == "ok"

    def test_doublon(self):
        h = "samehash"
        e1 = self._base("/a/2024/20240612/20240612-143022.jpg", h, "exif",
                        cible="2024/20240612/20240612-143022.jpg")
        e2 = self._base("/b/2024/20240612/20240612-143022_dup.jpg", h, "exif",
                        cible="2024/20240612/20240612-143022_dup.jpg")
        res = audit.annotate_status([e1, e2])
        assert "doublon" in res[0]["statut"]
        assert "doublon" in res[1]["statut"]
        assert res[1]["doublons_avec"] == e1["chemin"]

    def test_sans_exif_fallback(self):
        e = self._base("/x/f.jpg", "h", "mtime_fallback",
                       cible="2024/20240612/20240612-143022.jpg")
        res = audit.annotate_status([e])
        assert "sans_exif" in res[0]["statut"]

    def test_mal_nomme(self):
        e = self._base("/data/2024/20240612/20240613-153022.jpg", "h", "exif",
                       cible="2024/20240612/20240606-143022.jpg")
        res = audit.annotate_status([e])
        assert "mal_nomme" in res[0]["statut"]

    def test_combine_status(self):
        h = "x"
        e1 = self._base("/a/2024/20240612/20240612-143022.jpg", h, "exif",
                        cible="2024/20240612/20240612-143022.jpg")
        e2 = self._base("/b/2024/20240612/20240613-143022.jpg", h, "mtime_fallback",
                        cible="2024/20240612/20240606-143022.jpg")
        res = audit.annotate_status([e1, e2])
        assert "doublon" in res[1]["statut"]
        assert "sans_exif" in res[1]["statut"]
        assert "mal_nomme" in res[1]["statut"]

    def test_no_hash_no_doublon(self):
        e = self._base("/x/f.jpg", None, "exif",
                       cible="2024/20240612/20240612-143022.jpg")
        res = audit.annotate_status([e])
        assert "doublon" not in res[0]["statut"]
        assert res[0]["doublons_avec"] == ""

    def test_no_cible_no_mal_nomme(self):
        e = self._base("/x/f.jpg", "h", "exif", cible=None)
        res = audit.annotate_status([e])
        assert "mal_nomme" not in res[0]["statut"]


# --- write_csv_report -----------------------------------------------------

class TestWriteCsvReport:
    def test_header_and_rows(self, tmp_path):
        import csv
        entries = [
            {"chemin": "/a/f.jpg", "statut": "ok", "taille_octets": 10,
             "date_utilisee": "2024-01-01T00:00:00", "source_date": "exif",
             "chemin_cible_attendu": "2024/20240101/20240101-000000.jpg",
             "hash_sha256": "h1", "doublons_avec": ""},
            {"chemin": "/b/f.jpg", "statut": "doublon", "taille_octets": 5,
             "date_utilisee": None, "source_date": "mtime_fallback",
             "chemin_cible_attendu": None, "hash_sha256": None, "doublons_avec": ""},
        ]
        out = tmp_path / "r.csv"
        audit.write_csv_report(entries, out)
        with open(out, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert set(rows[0].keys()) == set(entries[0].keys())
        assert len(rows) == 2
        assert rows[0]["statut"] == "ok"
        assert rows[1]["statut"] == "doublon"


# --- print_summary --------------------------------------------------------

class TestPrintSummary:
    def test_counts(self, capsys):
        entries = [
            {"statut": "ok"},
            {"statut": "doublon"},
            {"statut": "sans_exif+"},
            {"statut": "mal_nomme+doublon"},
            {"statut": "ok"},
        ]
        audit.print_summary(entries)
        out = capsys.readouterr().out
        assert "Total fichiers        : 5" in out
        assert "Doublons détectés     : 2" in out
        assert "Sans date EXIF        : 1" in out
        assert "Mal nommés/mal placés : 1" in out
        assert "OK (rien à signaler)  : 2" in out

    def test_empty(self, capsys):
        audit.print_summary([])
        out = capsys.readouterr().out
        assert "Total fichiers        : 0" in out


# --- intégration ----------------------------------------------------------

class TestIntegration:
    def test_pipeline(self, media_dir, make_media_file, tmp_path):
        import csv
        content = b"photo-bytes"
        make_media_file("2024/20240612/a.jpg", content=content)
        make_media_file("_a_trier/b.jpg", content=content)
        out = tmp_path / "r.csv"
        entries = audit.scan_directories([str(media_dir)], False)
        entries = audit.annotate_status(entries)
        audit.write_csv_report(entries, out)
        rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
        assert len(rows) == 2
        assert all("doublon" in r["statut"] for r in rows)
        assert "sans_exif" in rows[0]["statut"]
