import csv
import logging
from datetime import datetime
from pathlib import Path

import pytest

import audit


class TestScanDirectories:
    def test_skips_nonexistent(self, tmp_path, cache_conn, caplog):
        caplog.set_level(logging.WARNING, logger="gestion_photo")
        entries = audit.scan_directories([str(tmp_path / "ghost")], False, cache_conn)
        assert entries == []
        assert any("ignoré" in r.message for r in caplog.records)

    def test_filters_non_media(self, media_dir, make_media_file, cache_conn):
        make_media_file("keep.jpg")
        (media_dir / "ignore.txt").write_text("nope")
        (media_dir / "ignore.pdf").write_bytes(b"%PDF")
        entries = audit.scan_directories([str(media_dir)], False, cache_conn)
        assert len(entries) == 1
        assert entries[0]["chemin"].endswith("keep.jpg")

    def test_collects_recursively(self, media_dir, make_media_file, cache_conn):
        make_media_file("2024/20240612/img1.jpg")
        make_media_file("sub/img2.CR2")
        make_media_file("sub/deep/img3.mp4")
        entries = audit.scan_directories([str(media_dir)], False, cache_conn)
        noms = {Path(e["chemin"]).name for e in entries}
        assert noms == {"img1.jpg", "img2.CR2", "img3.mp4"}

    def test_entry_keys(self, media_dir, make_media_file, cache_conn):
        make_media_file("x.jpg")
        entries = audit.scan_directories([str(media_dir)], False, cache_conn)
        expected = {"chemin", "taille_octets", "date_utilisee", "source_date",
                    "hash_sha256", "chemin_cible_attendu"}
        assert set(entries[0].keys()) == expected

    def test_filename_date_picked_up(self, media_dir, make_media_file, cache_conn):
        make_media_file("2024/20240612/IMG_20240612_143022.jpg")
        entries = audit.scan_directories([str(media_dir)], False, cache_conn)
        assert entries[0]["source_date"] == "filename_datetime"
        assert entries[0]["chemin_cible_attendu"] == "2024/202406/20240612/20240612-143022.jpg"

    def test_cache_hit_when_unchanged(self, media_dir, make_media_file, cache_conn):
        make_media_file("IMG_20240612-143022.jpg")
        e1 = audit.scan_directories([str(media_dir)], False, cache_conn)
        e2 = audit.scan_directories([str(media_dir)], False, cache_conn)
        assert len(e1) == len(e2) == 1
        assert e1[0]["hash_sha256"] == e2[0]["hash_sha256"]

    def test_cache_miss_on_size_change(self, media_dir, make_media_file, monkeypatch, cache_conn):
        p = make_media_file("IMG_20240612-143022.jpg")
        calls = {"n": 0}
        real = audit.compute_sha256
        def counting(path):
            calls["n"] += 1
            return real(path)
        monkeypatch.setattr(audit, "compute_sha256", counting)
        audit.scan_directories([str(media_dir)], False, cache_conn)
        assert calls["n"] == 1
        audit.scan_directories([str(media_dir)], False, cache_conn)
        assert calls["n"] == 1
        p.write_bytes(b"nouveau-contenu-plus-long")
        audit.scan_directories([str(media_dir)], False, cache_conn)
        assert calls["n"] == 2

    def test_force_refresh_recomputes_even_when_cached(self, media_dir, make_media_file, monkeypatch, cache_conn):
        p = make_media_file("IMG_20240612-143022.jpg")
        calls = {"n": 0}
        real = audit.compute_sha256
        def counting(path):
            calls["n"] += 1
            return real(path)
        monkeypatch.setattr(audit, "compute_sha256", counting)

        # Premier scan : cache miss -> calcule
        audit.scan_directories([str(media_dir)], False, cache_conn)
        assert calls["n"] == 1

        # Deuxième scan normal : cache hit -> ne recalcule pas
        audit.scan_directories([str(media_dir)], False, cache_conn)
        assert calls["n"] == 1

        # Troisième scan avec force_refresh=True : recalcule malgré le cache valide
        audit.scan_directories([str(media_dir)], False, cache_conn, force_refresh=True)
        assert calls["n"] == 2


class TestAnnotateStatus:
    def _base(self, chemin, hash_val, source_date, cible=None):
        return {
            "chemin": chemin, "taille_octets": 10,
            "date_utilisee": "2024-06-12T14:30:22", "source_date": source_date,
            "hash_sha256": hash_val, "chemin_cible_attendu": cible,
        }

    def test_ok(self):
        e = self._base("/data/2024/202406/20240612/20240612-143022.jpg", "abc", "exif",
                       cible="2024/202406/20240612/20240612-143022.jpg")
        assert audit.annotate_status([e])[0]["statut"] == "ok"

    def test_doublon(self):
        h = "samehash"
        e1 = self._base("/a/2024/202406/20240612/20240612-143022.jpg", h, "exif",
                        cible="2024/202406/20240612/20240612-143022.jpg")
        e2 = self._base("/b/2024/202406/20240612/20240612-143022_dup.jpg", h, "exif",
                        cible="2024/202406/20240612/20240612-143022_dup.jpg")
        res = audit.annotate_status([e1, e2])
        assert "doublon" in res[0]["statut"] and "doublon" in res[1]["statut"]
        assert res[1]["doublons_avec"] == e1["chemin"]

    def test_sans_exif(self):
        e = self._base("/x/f.jpg", "h", "aucune",
                       cible="2024/202406/20240612/20240612-143022.jpg")
        assert "sans_exif" in audit.annotate_status([e])[0]["statut"]

    def test_sans_exif_filename_only(self):
        e = self._base("/x/f.jpg", "h", "filename_date_only", cible=None)
        assert "sans_exif" in audit.annotate_status([e])[0]["statut"]

    def test_mal_nomme(self):
        e = self._base("/data/2024/202406/20240612/20240613-153022.jpg", "h", "exif",
                       cible="2024/202406/20240612/20240606-143022.jpg")
        assert "mal_nomme" in audit.annotate_status([e])[0]["statut"]

    def test_combine_status(self):
        h = "x"
        e1 = self._base("/a/2024/202406/20240612/20240612-143022.jpg", h, "exif",
                        cible="2024/202406/20240612/20240612-143022.jpg")
        e2 = self._base("/b/2024/202406/20240612/20240613-143022.jpg", h, "filename_date_only",
                        cible="2024/202406/20240612/20240606-143022.jpg")
        res = audit.annotate_status([e1, e2])
        assert all(s in res[1]["statut"] for s in ("doublon", "sans_exif", "mal_nomme"))

    def test_no_hash_no_doublon(self):
        e = self._base("/x/f.jpg", None, "exif",
                       cible="2024/202406/20240612/20240612-143022.jpg")
        res = audit.annotate_status([e])
        assert "doublon" not in res[0]["statut"] and res[0]["doublons_avec"] == ""

    def test_no_cible_no_mal_nomme(self):
        e = self._base("/x/f.jpg", "h", "exif", cible=None)
        assert "mal_nomme" not in audit.annotate_status([e])[0]["statut"]


class TestWriteCsvReport:
    def test_header_and_rows(self, tmp_path):
        entries = [
            {"chemin": "/a/f.jpg", "statut": "ok", "taille_octets": 10,
             "date_utilisee": "2024-01-01T00:00:00", "source_date": "exif",
             "chemin_cible_attendu": "2024/202406/20240612/20240612-143022.jpg",
             "hash_sha256": "h1", "doublons_avec": ""},
            {"chemin": "/b/f.jpg", "statut": "doublon", "taille_octets": 5,
             "date_utilisee": None, "source_date": "aucune",
             "chemin_cible_attendu": None, "hash_sha256": None, "doublons_avec": ""},
        ]
        out = tmp_path / "r.csv"
        audit.write_csv_report(entries, out)
        rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
        assert set(rows[0].keys()) == set(entries[0].keys())
        assert len(rows) == 2
        assert rows[0]["statut"] == "ok" and rows[1]["statut"] == "doublon"


class TestPrintSummary:
    def test_counts(self, capsys):
        entries = [{"statut": s} for s in ["ok", "doublon", "sans_exif+", "mal_nomme+doublon", "ok"]]
        audit.print_summary(entries)
        out = capsys.readouterr().out
        assert "Total fichiers        : 5" in out
        assert "Doublons détectés     : 2" in out
        assert "Sans date EXIF        : 1" in out
        assert "Mal nommés/mal placés : 1" in out
        assert "OK (rien à signaler)  : 2" in out

    def test_empty(self, capsys):
        audit.print_summary([])
        assert "Total fichiers        : 0" in capsys.readouterr().out


class TestIntegration:
    def test_pipeline(self, media_dir, make_media_file, tmp_path, cache_conn):
        make_media_file("2024/20240612/a.jpg", content=b"photo-bytes")
        make_media_file("_a_trier/b.jpg", content=b"photo-bytes")
        out = tmp_path / "r.csv"
        entries = audit.scan_directories([str(media_dir)], False, cache_conn)
        entries = audit.annotate_status(entries)
        audit.write_csv_report(entries, out)
        rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
        assert len(rows) == 2
        assert all("doublon" in r["statut"] for r in rows)
        assert "sans_exif" in rows[0]["statut"]


class TestAuditMain:
    def test_main_writes_csv(self, media_dir, make_media_file, tmp_path, monkeypatch):
        make_media_file("IMG_20240612_143022.jpg")
        out = tmp_path / "rapports" / "rapport_audit.csv"
        monkeypatch.setattr("sys.argv", ["audit.py", "--scan", str(media_dir),
                                         "--output", str(out), "--no-cache"])
        audit.main()
        assert out.exists()
        rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
        assert len(rows) == 1
        assert rows[0]["source_date"] == "filename_datetime"

    def test_force_refresh_recomputes(self, media_dir, make_media_file, tmp_path, monkeypatch):
        make_media_file("IMG_20240612_143022.jpg")
        out = tmp_path / "rapports" / "rapport_audit.csv"
        cache_db = tmp_path / "hash_cache.db"
        monkeypatch.setattr("sys.argv", ["audit.py", "--scan", str(media_dir),
                                         "--output", str(out), "--force-refresh",
                                         "--cache-db", str(cache_db)])
        audit.main()
        assert out.exists()
        rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
        assert len(rows) == 1

    def test_force_refresh_and_no_cache_incompatible(self, tmp_path, monkeypatch):
        out = tmp_path / "rapports" / "rapport_audit.csv"
        monkeypatch.setattr("sys.argv", ["audit.py", "--scan", str(tmp_path),
                                         "--output", str(out), "--no-cache", "--force-refresh"])
        with pytest.raises(SystemExit) as exc:
            audit.main()
        assert exc.value.code == 2
