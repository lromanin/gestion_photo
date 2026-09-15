from datetime import datetime
from pathlib import Path
from unittest import mock
import csv
import hashlib
import json
import logging

import pytest

import photo_common


def _mock_result(returncode=0, stdout="", stderr=""):
    m = mock.Mock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


class TestExtensions:
    def test_photo_extensions(self):
        assert {".jpg", ".cr2", ".heic"} <= photo_common.PHOTO_EXTENSIONS

    def test_video_extensions(self):
        assert {".mp4", ".mov", ".mkv"} <= photo_common.VIDEO_EXTENSIONS

    def test_union(self):
        assert photo_common.ALL_EXTENSIONS == photo_common.PHOTO_EXTENSIONS | photo_common.VIDEO_EXTENSIONS


class TestExiftoolAvailable:
    def test_present(self, monkeypatch):
        monkeypatch.setattr(photo_common.shutil, "which", lambda cmd: "/usr/bin/exiftool")
        assert photo_common.exiftool_available() is True

    def test_absent(self, monkeypatch):
        monkeypatch.setattr(photo_common.shutil, "which", lambda cmd: None)
        assert photo_common.exiftool_available() is False


class TestGetExifDate:
    def test_disabled_returns_none(self, tmp_path):
        p = tmp_path / "x.jpg"; p.write_bytes(b"x")
        assert photo_common.get_exif_date(p, False) is None

    def test_parse_datetime_original(self, tmp_path, monkeypatch):
        p = tmp_path / "x.jpg"; p.write_bytes(b"x")
        payload = json.dumps([{"SourceFile": str(p), "DateTimeOriginal": "2024:06:12 14:30:22"}])
        monkeypatch.setattr(photo_common.subprocess, "run", lambda *a, **k: _mock_result(0, payload))
        assert photo_common.get_exif_date(p, True) == datetime(2024, 6, 12, 14, 30, 22)

    def test_fallback_to_create_date(self, tmp_path, monkeypatch):
        p = tmp_path / "x.jpg"; p.write_bytes(b"x")
        payload = json.dumps([{"DateTimeOriginal": "", "CreateDate": "2023:01:02 03:04:05"}])
        monkeypatch.setattr(photo_common.subprocess, "run", lambda *a, **k: _mock_result(0, payload))
        assert photo_common.get_exif_date(p, True) == datetime(2023, 1, 2, 3, 4, 5)

    def test_empty_tags_returns_none(self, tmp_path, monkeypatch):
        p = tmp_path / "x.jpg"; p.write_bytes(b"x")
        payload = json.dumps([{"SourceFile": str(p)}])
        monkeypatch.setattr(photo_common.subprocess, "run", lambda *a, **k: _mock_result(0, payload))
        assert photo_common.get_exif_date(p, True) is None

    def test_nonzero_returncode_returns_none(self, tmp_path, monkeypatch):
        p = tmp_path / "x.jpg"; p.write_bytes(b"x")
        monkeypatch.setattr(photo_common.subprocess, "run", lambda *a, **k: _mock_result(1, "err"))
        assert photo_common.get_exif_date(p, True) is None

    def test_invalid_json_returns_none(self, tmp_path, monkeypatch):
        p = tmp_path / "x.jpg"; p.write_bytes(b"x")
        monkeypatch.setattr(photo_common.subprocess, "run", lambda *a, **k: _mock_result(0, "bad{"))
        assert photo_common.get_exif_date(p, True) is None

    def test_timeout_returns_none(self, tmp_path, monkeypatch):
        import subprocess as sp
        p = tmp_path / "x.jpg"; p.write_bytes(b"x")
        monkeypatch.setattr(photo_common.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(sp.TimeoutExpired("exiftool", 30)))
        assert photo_common.get_exif_date(p, True) is None

    def test_timezone_suffix_truncated(self, tmp_path, monkeypatch):
        p = tmp_path / "x.jpg"; p.write_bytes(b"x")
        payload = json.dumps([{"DateTimeOriginal": "2024:06:12 14:30:22+02:00"}])
        monkeypatch.setattr(photo_common.subprocess, "run", lambda *a, **k: _mock_result(0, payload))
        assert photo_common.get_exif_date(p, True) == datetime(2024, 6, 12, 14, 30, 22)


class TestGetFilenameDate:
    def test_img_pattern_with_time(self, tmp_path):
        p = tmp_path / "IMG_20240612_143022.jpg"; p.write_bytes(b"x")
        assert photo_common.get_filename_date(p) == (datetime(2024, 6, 12, 14, 30, 22), True)

    def test_iso_pattern_with_time(self, tmp_path):
        p = tmp_path / "2023-05-01_03.04.05.jpg"; p.write_bytes(b"x")
        assert photo_common.get_filename_date(p) == (datetime(2023, 5, 1, 3, 4, 5), True)

    def test_whatsapp_date_only(self, tmp_path):
        p = tmp_path / "IMG-20240612-WA0001.jpg"; p.write_bytes(b"x")
        assert photo_common.get_filename_date(p) == (datetime(2024, 6, 12, 0, 0, 0), False)

    def test_iso_date_only(self, tmp_path):
        p = tmp_path / "trip-2021-08-15.jpg"; p.write_bytes(b"x")
        assert photo_common.get_filename_date(p) == (datetime(2021, 8, 15, 0, 0, 0), False)

    def test_no_match_returns_none(self, tmp_path):
        p = tmp_path / "random_name.jpg"; p.write_bytes(b"x")
        assert photo_common.get_filename_date(p) == (None, False)

    def test_implausible_year_skipped(self, tmp_path):
        p = tmp_path / "18880612-143022.jpg"; p.write_bytes(b"x")
        assert photo_common.get_filename_date(p) == (None, False)

    def test_invalid_date_next_pattern(self, tmp_path):
        p = tmp_path / "2024-13-40-25.61.61.jpg"; p.write_bytes(b"x")
        assert photo_common.get_filename_date(p) == (None, False)


class TestGetBestDate:
    def test_exif_priority(self, tmp_path, monkeypatch):
        p = tmp_path / "IMG_20240612_143022.jpg"; p.write_bytes(b"x")
        payload = json.dumps([{"DateTimeOriginal": "2024:06:12 14:30:22"}])
        monkeypatch.setattr(photo_common.subprocess, "run", lambda *a, **k: _mock_result(0, payload))
        assert photo_common.get_best_date(p, True) == (datetime(2024, 6, 12, 14, 30, 22), "exif")

    def test_filename_when_no_exiftool(self, tmp_path):
        p = tmp_path / "IMG_20240612_143022.jpg"; p.write_bytes(b"x")
        assert photo_common.get_best_date(p, False) == (datetime(2024, 6, 12, 14, 30, 22), "filename_datetime")

    def test_filename_date_only(self, tmp_path):
        p = tmp_path / "trip-2023-05-01.jpg"; p.write_bytes(b"x")
        assert photo_common.get_best_date(p, False) == (datetime(2023, 5, 1, 0, 0, 0), "filename_date_only")

    def test_no_date_anywhere(self, tmp_path):
        p = tmp_path / "random.jpg"; p.write_bytes(b"x")
        assert photo_common.get_best_date(p, False) == (None, "aucune")

    def test_exif_none_falls_to_filename(self, tmp_path, monkeypatch):
        p = tmp_path / "IMG_20240612_143022.jpg"; p.write_bytes(b"x")
        payload = json.dumps([{"SourceFile": str(p)}])
        monkeypatch.setattr(photo_common.subprocess, "run", lambda *a, **k: _mock_result(0, payload))
        assert photo_common.get_best_date(p, True) == (datetime(2024, 6, 12, 14, 30, 22), "filename_datetime")


class TestComputeSha256:
    def test_known_hash(self, tmp_path):
        content = b"hello world"
        p = tmp_path / "f.jpg"; p.write_bytes(content)
        assert photo_common.compute_sha256(p) == hashlib.sha256(content).hexdigest()

    def test_missing_file_returns_none(self, tmp_path, caplog):
        caplog.set_level(logging.WARNING, logger="gestion_photo")
        p = tmp_path / "missing.jpg"
        assert photo_common.compute_sha256(p) is None
        assert any("Impossible de lire" in r.message for r in caplog.records)


class TestExpectedRelativePath:
    def test_format(self):
        dt = datetime(2024, 6, 12, 14, 30, 22)
        assert photo_common.expected_relative_path(dt, ".jpg") == "2024/202406/20240612/20240612-143022.jpg"

    def test_extension_lowercased(self):
        dt = datetime(2024, 6, 12, 0, 0, 0)
        assert photo_common.expected_relative_path(dt, ".JPG").endswith(".jpg")

    def test_midnight(self):
        dt = datetime(2020, 1, 1, 0, 0, 0)
        assert photo_common.expected_relative_path(dt, ".cr2") == "2020/202001/20200101/20200101-000000.cr2"


class TestCacheDb:
    def test_store_and_get(self, cache_conn):
        photo_common.store_cache_entry(cache_conn, "/a/x.jpg", 100, 1000.0, "h1", "2024-01-01T00:00:00", "exif")
        cache_conn.commit()
        assert photo_common.get_cached_entry(cache_conn, "/a/x.jpg", 100, 1000.0) == ("h1", "2024-01-01T00:00:00", "exif")

    def test_stale_invalidates(self, cache_conn):
        photo_common.store_cache_entry(cache_conn, "/a/x.jpg", 100, 1000.0, "h1", None, "aucune")
        cache_conn.commit()
        assert photo_common.get_cached_entry(cache_conn, "/a/x.jpg", 999, 1000.0) is None
        assert photo_common.get_cached_entry(cache_conn, "/a/x.jpg", 100, 9999.0) is None
        assert photo_common.get_cached_entry(cache_conn, "/a/x.jpg", 100, 1000.0) == ("h1", None, "aucune")

    def test_delete_entry(self, cache_conn):
        photo_common.store_cache_entry(cache_conn, "/a/x.jpg", 100, 1000.0, "h1", None, "aucune")
        cache_conn.commit()
        photo_common.delete_cache_entry(cache_conn, "/a/x.jpg")
        cache_conn.commit()
        assert photo_common.get_cached_entry(cache_conn, "/a/x.jpg", 100, 1000.0) is None

    def test_find_path_by_hash_with_prefix(self, cache_conn):
        photo_common.store_cache_entry(cache_conn, "/dest/2024/202406/20240612/f.jpg", 50, 1.0, "hX", None, "exif")
        cache_conn.commit()
        assert photo_common.find_cached_path_by_hash(cache_conn, "hX") == "/dest/2024/202406/20240612/f.jpg"
        assert photo_common.find_cached_path_by_hash(cache_conn, "hX", "/dest") == "/dest/2024/202406/20240612/f.jpg"
        assert photo_common.find_cached_path_by_hash(cache_conn, "hX", "/autre") is None
        assert photo_common.find_cached_path_by_hash(cache_conn, "ZZZ") is None


class TestIsExpectedFilename:
    def test_exact_match(self):
        assert photo_common.is_expected_filename(
            "/data/2024/202406/20240612/20240612-143022.jpg",
            "2024/202406/20240612/20240612-143022.jpg",
        ) is True

    def test_burst_suffix_a(self):
        assert photo_common.is_expected_filename(
            "/data/2024/202406/20240612/20240612-143022a.jpg",
            "2024/202406/20240612/20240612-143022.jpg",
        ) is True

    def test_burst_suffix_z(self):
        assert photo_common.is_expected_filename(
            "/mnt/pool/photos/2024/202406/20240612/20240612-143022z.jpg",
            "2024/202406/20240612/20240612-143022.jpg",
        ) is True

    def test_burst_multiple_letters(self):
        assert photo_common.is_expected_filename(
            "/x/2024/202406/20240612/20240612-143022abc.jpg",
            "2024/202406/20240612/20240612-143022.jpg",
        ) is True

    def test_wrong_directory_structure(self):
        assert photo_common.is_expected_filename(
            "/data/2024/202406/20240613/20240612-143022.jpg",
            "2024/202406/20240612/20240612-143022.jpg",
        ) is False

    def test_wrong_suffix(self):
        assert photo_common.is_expected_filename(
            "/data/2024/202406/20240612/20240612-143022.png",
            "2024/202406/20240612/20240612-143022.jpg",
        ) is False

    def test_burst_with_number_suffix_rejected(self):
        assert photo_common.is_expected_filename(
            "/data/2024/202406/20240612/20240612-143022-1.jpg",
            "2024/202406/20240612/20240612-143022.jpg",
        ) is False

    def test_different_base_name(self):
        assert photo_common.is_expected_filename(
            "/data/2024/202406/20240612/20240613-143022.jpg",
            "2024/202406/20240612/20240612-143022.jpg",
        ) is False


class TestWalkMediaFiles:
    def test_walks_recursively(self, media_dir, make_media_file):
        make_media_file("2024/20240612/img1.jpg")
        make_media_file("sub/img2.CR2")
        make_media_file("sub/deep/img3.mp4")
        make_media_file("ignore.txt")  # non-media
        result = list(photo_common.walk_media_files(media_dir))
        noms = {p.name for p in result}
        assert noms == {"img1.jpg", "img2.CR2", "img3.mp4"}

    def test_filters_extensions(self, media_dir, make_media_file):
        make_media_file("keep.jpg")
        make_media_file("keep.cr2")
        make_media_file("ignore.txt")
        make_media_file("ignore.pdf")
        result = list(photo_common.walk_media_files(media_dir))
        assert len(result) == 2
        assert all(p.suffix.lower() in {".jpg", ".cr2"} for p in result)

    def test_skips_nonexistent(self, tmp_path, caplog):
        caplog.set_level(logging.WARNING, logger="gestion_photo")
        result = list(photo_common.walk_media_files(tmp_path / "ghost"))
        assert result == []
        assert any("introuvable" in r.message for r in caplog.records)

    def test_logs_permission_error(self, tmp_path, caplog):
        # Create a directory we can't read
        import os
        caplog.set_level(logging.WARNING, logger="gestion_photo")
        hidden = tmp_path / "hidden"
        hidden.mkdir()
        (hidden / "secret.jpg").write_bytes(b"x")
        # Remove read permission on the directory
        os.chmod(hidden, 0o000)
        try:
            result = list(photo_common.walk_media_files(tmp_path))
            # The function should log the error and skip the hidden dir
            assert len(result) == 0  # no files from the hidden dir
            assert any("Accès refusé" in r.message for r in caplog.records)
        finally:
            # Restore permission for cleanup
            os.chmod(hidden, 0o700)


class TestIsBareName:
    def test_bare_name_valid(self, tmp_path):
        f = tmp_path / "20240612-143022.jpg"
        f.write_bytes(b"x")
        assert photo_common.is_bare_name(f) is True

    def test_bare_name_with_burst_suffix_rejected(self, tmp_path):
        f = tmp_path / "20240612-143022a.jpg"
        f.write_bytes(b"x")
        assert photo_common.is_bare_name(f) is False

    def test_bare_name_with_numeric_suffix_rejected(self, tmp_path):
        f = tmp_path / "20240612-143022-1.jpg"
        f.write_bytes(b"x")
        assert photo_common.is_bare_name(f) is False

    def test_bare_name_wrong_format_rejected(self, tmp_path):
        f = tmp_path / "IMG_20240612_143022.jpg"
        f.write_bytes(b"x")
        assert photo_common.is_bare_name(f) is False


class TestResolveUniquePath:
    def test_no_collision(self, tmp_path):
        p = tmp_path / "dest" / "a.jpg"
        p.parent.mkdir(parents=True)
        assert photo_common.resolve_unique_path(p) == p

    def test_collision_adds_suffix(self, tmp_path):
        (tmp_path / "dest").mkdir(parents=True)
        (tmp_path / "dest" / "a.jpg").write_bytes(b"x")
        out = photo_common.resolve_unique_path(tmp_path / "dest" / "a.jpg")
        assert out.name == "a-2.jpg"

    def test_multiple_collisions(self, tmp_path):
        (tmp_path / "a.jpg").write_bytes(b"x")
        (tmp_path / "a-2.jpg").write_bytes(b"x")
        out = photo_common.resolve_unique_path(tmp_path / "a.jpg")
        assert out.name == "a-3.jpg"


class TestAppendDoublonLog:
    def test_creates_header_on_new(self, tmp_path):
        log_path = tmp_path / "doublons.csv"
        photo_common.append_doublon_log(log_path, "q/a.jpg", "d/b.jpg", "hash1")
        import csv
        rows = list(csv.reader(open(log_path)))
        assert rows[0] == ["horodatage", "fichier_en_quarantaine",
                           "fichier_original_correspondant", "hash"]
        assert rows[1][1:] == ["q/a.jpg", "d/b.jpg", "hash1"]

    def test_appends_without_duplicate_header(self, tmp_path):
        log_path = tmp_path / "doublons.csv"
        photo_common.append_doublon_log(log_path, "q/a.jpg", "d/b.jpg", "h1")
        photo_common.append_doublon_log(log_path, "q/c.jpg", "d/b.jpg", "h2")
        import csv
        rows = list(csv.reader(open(log_path)))
        assert len(rows) == 3
        assert rows[1][3] == "h1" and rows[2][3] == "h2"


class TestMoveAndRecache:
    def test_dry_run_no_move(self, tmp_path, cache_conn):
        src = tmp_path / "src.jpg"; src.write_bytes(b"data")
        dest = tmp_path / "dest.jpg"
        photo_common.move_and_recache(src, dest, "h", None, "aucune", cache_conn, dry_run=True)
        assert src.exists()
        assert not dest.exists()

    def test_real_move_updates_cache(self, tmp_path, cache_conn):
        src = tmp_path / "src.jpg"; src.write_bytes(b"data")
        dest = tmp_path / "out" / "dest.jpg"
        photo_common.move_and_recache(src, dest, "h", None, "aucune", cache_conn, dry_run=False)
        assert not src.exists()
        assert dest.exists() and dest.read_bytes() == b"data"
        st = dest.stat()
        assert photo_common.get_cached_entry(cache_conn, str(dest), st.st_size, st.st_mtime) is not None
        assert photo_common.get_cached_entry(cache_conn, str(src), 0, 0.0) is None


class TestGetCachedOrCompute:
    def test_cache_hit(self, tmp_path, cache_conn):
        f = tmp_path / "test.jpg"; f.write_bytes(b"test-data")
        # First call: cache miss
        h1, d1, s1, hit1 = photo_common.get_cached_or_compute(f, cache_conn, False)
        assert hit1 is False
        # Second call: cache hit
        h2, d2, s2, hit2 = photo_common.get_cached_or_compute(f, cache_conn, False)
        assert hit2 is True
        assert h1 == h2

    def test_cache_miss_on_modification(self, tmp_path, cache_conn):
        f = tmp_path / "test.jpg"; f.write_bytes(b"v1")
        photo_common.get_cached_or_compute(f, cache_conn, False)
        f.write_bytes(b"v2-modified")
        h, d, s, hit = photo_common.get_cached_or_compute(f, cache_conn, False)
        assert hit is False
        # hash has been recalculated for the new content
        assert h == photo_common.compute_sha256(f)

    def test_unreadable_returns_error(self, tmp_path, cache_conn, caplog):
        caplog.set_level(logging.WARNING, logger="gestion_photo")
        f = tmp_path / "missing.jpg"
        h, d, s, hit = photo_common.get_cached_or_compute(f, cache_conn, False)
        assert h is None and d is None and s == "erreur" and hit is False
        assert any("Impossible d'accéder" in r.message for r in caplog.records)
