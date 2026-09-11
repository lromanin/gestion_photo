import csv
from pathlib import Path

import pytest

import ingest
import photo_common


class TestResolveUniquePath:
    def test_no_collision(self, tmp_path):
        des = tmp_path / "dest" / "a-2.jpg"
        assert ingest.resolve_unique_path(des) == des

    def test_collision_adds_suffix(self, tmp_path):
        (tmp_path / "dest").mkdir(parents=True, exist_ok=True)
        (tmp_path / "dest" / "a.jpg").write_bytes(b"x")
        out = ingest.resolve_unique_path(tmp_path / "dest" / "a.jpg")
        assert out == tmp_path / "dest" / "a-2.jpg"

    def test_multiple_collisions(self, tmp_path):
        (tmp_path / "a.jpg").write_bytes(b"x")
        (tmp_path / "a-2.jpg").write_bytes(b"x")
        out = ingest.resolve_unique_path(tmp_path / "a.jpg")
        assert out.name == "a-3.jpg"


class TestLoadKnownHashes:
    def test_empty(self, cache_conn):
        assert ingest.load_known_hashes(cache_conn, "/dest") == {}

    def test_populated(self, cache_conn):
        photo_common.store_cache_entry(cache_conn, "/dest/2024/202406/20240612/f.jpg",
                                        50, 1.0, "hX", "2024-01-01T00:00:00", "exif")
        cache_conn.commit()
        known = ingest.load_known_hashes(cache_conn, "/dest")
        assert known == {"/dest/2024/202406/20240612/f.jpg": "hX"} or known == {"hX": "/dest/2024/202406/20240612/f.jpg"}

    def test_prefix_filter(self, cache_conn):
        photo_common.store_cache_entry(cache_conn, "/dest/f.jpg", 50, 1.0, "hA", None, "aucune")
        photo_common.store_cache_entry(cache_conn, "/other/f.jpg", 50, 1.0, "hB", None, "aucune")
        cache_conn.commit()
        known = ingest.load_known_hashes(cache_conn, "/dest")
        assert known == {"hA": "/dest/f.jpg"}


class TestAppendDoublonLog:
    def test_creates_header_on_new(self, tmp_path):
        log_path = tmp_path / "doublons.csv"
        ingest.append_doublon_log(log_path, "q/a.jpg", "d/b.jpg", "hash1")
        rows = list(csv.reader(open(log_path)))
        assert rows[0] == ["horodatage", "fichier_en_quarantaine",
                           "fichier_original_correspondant", "hash"]
        assert rows[1][1:] == ["q/a.jpg", "d/b.jpg", "hash1"]

    def test_no_duplicate_header(self, tmp_path):
        log_path = tmp_path / "doublons.csv"
        ingest.append_doublon_log(log_path, "q/a.jpg", "d/b.jpg", "h1")
        ingest.append_doublon_log(log_path, "q/c.jpg", "d/b.jpg", "h2")
        rows = list(csv.reader(open(log_path)))
        assert len(rows) == 3
        assert rows[1][3] == "h1" and rows[2][3] == "h2"


class TestMoveAndRecache:
    def test_dry_run_no_move(self, tmp_path, cache_conn):
        src = tmp_path / "src.jpg"; src.write_bytes(b"data")
        dest = tmp_path / "dest.jpg"
        ingest.move_and_recache(src, dest, "h", None, "aucune", cache_conn, dry_run=True)
        assert src.exists()
        assert not dest.exists()

    def test_real_move_updates_cache(self, tmp_path, cache_conn):
        src = tmp_path / "src.jpg"; src.write_bytes(b"data")
        dest = tmp_path / "out" / "dest.jpg"
        ingest.move_and_recache(src, dest, "h", None, "aucune", cache_conn, dry_run=False)
        assert not src.exists()
        assert dest.exists() and dest.read_bytes() == b"data"
        st = dest.stat()
        assert photo_common.get_cached_entry(cache_conn, str(dest), st.st_size, st.st_mtime) is not None
        assert photo_common.get_cached_entry(cache_conn, str(src), 0, 0.0) is None


class TestProcessFile:
    def test_ingere_with_filename_date(self, tmp_path, cache_conn):
        src = tmp_path / "src"; src.mkdir()
        f = src / "IMG_20240612_143022.jpg"; f.write_bytes(b"img-data")
        dest = tmp_path / "dest"; dest.mkdir()
        result = ingest.process_file(f, dest, tmp_path / "q", tmp_path / "sans",
                                      {}, cache_conn, tmp_path / "d.csv", False, False)
        assert result == "ingere"
        assert not f.exists()
        assert (dest / "2024/202406/20240612/20240612-143022.jpg").exists()

    def test_doublon_intra_lot(self, tmp_path, cache_conn):
        src = tmp_path / "src"; src.mkdir()
        f1 = src / "IMG_20240612_143022.jpg"; f1.write_bytes(b"img-data")
        f2 = src / "IMG_20240612_143022_dup.jpg"; f2.write_bytes(b"img-data")
        dest = tmp_path / "dest"; dest.mkdir()
        known = {}
        r1 = ingest.process_file(f1, dest, tmp_path / "q", tmp_path / "sans",
                                  known, cache_conn, tmp_path / "d.csv", False, False)
        r2 = ingest.process_file(f2, dest, tmp_path / "q", tmp_path / "sans",
                                  known, cache_conn, tmp_path / "d.csv", False, False)
        assert r1 == "ingere" and r2 == "doublon"
        assert (tmp_path / "q" / "IMG_20240612_143022_dup.jpg").exists()

    def test_sans_date(self, tmp_path, cache_conn):
        src = tmp_path / "src"; src.mkdir()
        f = src / "random.jpg"; f.write_bytes(b"x")
        dest = tmp_path / "dest"; dest.mkdir()
        result = ingest.process_file(f, dest, tmp_path / "q", tmp_path / "sans",
                                      {}, cache_conn, tmp_path / "d.csv", False, False)
        assert result == "sans_date"
        assert (tmp_path / "sans" / "random.jpg").exists()

    def test_erreur_on_unreadable(self, tmp_path, cache_conn, monkeypatch):
        src = tmp_path / "src"; src.mkdir()
        f = src / "bad.jpg"; f.write_bytes(b"x")
        monkeypatch.setattr(ingest, "compute_sha256", lambda path: None)
        dest = tmp_path / "dest"; dest.mkdir()
        result = ingest.process_file(f, dest, tmp_path / "q", tmp_path / "sans",
                                      {}, cache_conn, tmp_path / "d.csv", False, False)
        assert result == "erreur"


class TestIngestDirectory:
    def test_counts(self, tmp_path, cache_conn, monkeypatch):
        src = tmp_path / "src"; src.mkdir()
        (src / "a.jpg").write_bytes(b"x")
        dest = tmp_path / "dest"; dest.mkdir()
        counts = ingest.ingest_directory([str(src)], dest, tmp_path / "q",
                                          tmp_path / "sans", cache_conn,
                                          tmp_path / "d.csv", False, False)
        assert sum(counts.values()) >= 1


class TestPrintSummary:
    def test_dry_run(self, capsys):
        ingest.print_summary({"ingere": 1, "doublon": 1, "sans_date": 1, "erreur": 0}, True)
        out = capsys.readouterr().out
        assert "(dry-run" in out and "Ingérés (rangés)   : 1" in out
