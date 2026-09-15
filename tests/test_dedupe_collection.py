"""Tests pour dedupe_collection.py — dédoublonnage de la collection existante."""
import os
import logging
os.environ.setdefault("TELEGRAM_TOKEN", "dummy_token")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dedupe_collection import (
    choose_keeper,
    find_duplicate_groups,
    resolve_duplicate_group,
)


class TestChooseKeeper:
    def test_bare_name_preferred(self, tmp_path):
        """Parmi un groupe de doublons, le nom 'nu' (AAAAMMJJ-HHMMSS.ext) est gardé."""
        f1 = tmp_path / "20240612-143022.jpg"; f1.write_bytes(b"x")
        f2 = tmp_path / "20240612-143022a.jpg"; f2.write_bytes(b"x")
        f3 = tmp_path / "20240612-143022b.jpg"; f3.write_bytes(b"x")
        
        keeper = choose_keeper([f1, f2, f3])
        assert keeper == f1  # nom nu

    def test_no_bare_name_alphabetical(self, tmp_path):
        """Sans nom nu, le premier par ordre alphabétique est gardé (déterministe)."""
        f1 = tmp_path / "20240612-143022a.jpg"; f1.write_bytes(b"x")
        f2 = tmp_path / "20240612-143022b.jpg"; f2.write_bytes(b"x")
        
        keeper = choose_keeper([f1, f2])
        assert keeper == f1  # 'a' < 'b'

    def test_single_file(self, tmp_path):
        """Groupe d'un seul fichier : le fichier est gardé."""
        f = tmp_path / "20240612-143022a.jpg"; f.write_bytes(b"x")
        assert choose_keeper([f]) == f


class TestFindDuplicateGroups:
    def test_groups_only_duplicates(self, tmp_path, cache_conn):
        """Retourne seulement les groupes de 2+ fichiers (hash identique)."""
        (tmp_path / "unique.jpg").write_bytes(b"unique-content")
        (tmp_path / "dup1.jpg").write_bytes(b"same")
        (tmp_path / "dup2.jpg").write_bytes(b"same")
        
        groups = find_duplicate_groups([str(tmp_path)], cache_conn, False)
        
        assert len(groups) == 1
        assert len(list(groups.values())[0]) == 2

    def test_skips_unreadable(self, tmp_path, cache_conn, caplog):
        """Fichiers illisibles ignorés avec warning."""
        caplog.set_level(logging.WARNING, logger="gestion_photo")
        import os
        hidden = tmp_path / "hidden"; hidden.mkdir()
        (hidden / "secret.jpg").write_bytes(b"x")
        os.chmod(hidden, 0o000)
        try:
            find_duplicate_groups([str(tmp_path)], cache_conn, False)
        finally:
            os.chmod(hidden, 0o700)
        assert any("Accès refusé" in r.message for r in caplog.records)


class TestResolveDuplicateGroup:
    def test_moves_others_to_quarantine(self, tmp_path, cache_conn):
        """Garde un exemplaire, déplace les autres en quarantaine."""
        f1 = tmp_path / "20240612-143022.jpg"; f1.write_bytes(b"same")
        f2 = tmp_path / "20240612-143022a.jpg"; f2.write_bytes(b"same")
        quarantine = tmp_path / "quarantine"
        log_path = tmp_path / "doublons.csv"
        
        moved = resolve_duplicate_group(
            "hash123", [f1, f2], quarantine, log_path, cache_conn, dry_run=False
        )
        
        assert moved == 1
        assert f1.exists()  # keeper intact
        assert not f2.exists()  # moved
        assert (quarantine / "20240612-143022a.jpg").exists()

    def test_logs_duplicate_in_csv(self, tmp_path, cache_conn):
        """Écrit une entrée dans le log CSV des doublons."""
        f1 = tmp_path / "20240612-143022.jpg"; f1.write_bytes(b"same")
        f2 = tmp_path / "20240612-143022a.jpg"; f2.write_bytes(b"same")
        quarantine = tmp_path / "quarantine"
        log_path = tmp_path / "doublons.csv"
        
        resolve_duplicate_group("hash123", [f1, f2], quarantine, log_path, cache_conn, dry_run=False)
        
        import csv
        rows = list(csv.reader(open(log_path)))
        assert len(rows) == 2  # header + 1 entry
        assert rows[1][3] == "hash123"

    def test_dry_run_does_nothing(self, tmp_path, cache_conn):
        """En dry-run, aucun fichier n'est déplacé, pas d'écriture log."""
        f1 = tmp_path / "20240612-143022.jpg"; f1.write_bytes(b"same")
        f2 = tmp_path / "20240612-143022a.jpg"; f2.write_bytes(b"same")
        quarantine = tmp_path / "quarantine"
        log_path = tmp_path / "doublons.csv"
        
        moved = resolve_duplicate_group(
            "hash123", [f1, f2], quarantine, log_path, cache_conn, dry_run=True
        )
        
        assert moved == 1  # retourne le compte théorique
        assert f1.exists() and f2.exists()  # rien n'a bougé
        assert not log_path.exists()

    def test_handles_oserror_gracefully(self, tmp_path, cache_conn, monkeypatch):
        """Erreur OSError lors du déplacement → log error, continue."""
        import logging
        f1 = tmp_path / "20240612-143022.jpg"; f1.write_bytes(b"same")
        f2 = tmp_path / "20240612-143022a.jpg"; f2.write_bytes(b"same")
        quarantine = tmp_path / "quarantine"
        log_path = tmp_path / "doublons.csv"
        
        def failing_move(*a, **k):
            raise OSError("Permission denied")
        
        monkeypatch.setattr("shutil.move", failing_move)
        
        with patch("dedupe_collection.logger") as mock_logger:
            moved = resolve_duplicate_group("hash123", [f1, f2], quarantine, log_path, cache_conn, dry_run=False)
        
        assert moved == 0
        mock_logger.error.assert_called()


class TestDedupeCollectionMain:
    def test_dry_run_simulates(self, tmp_path, monkeypatch, capfd):
        """CLI dry-run affiche le résumé sans rien modifier."""
        (tmp_path / "a.jpg").write_bytes(b"same")
        (tmp_path / "b.jpg").write_bytes(b"same")
        
        import sys
        monkeypatch.setattr(sys, "argv", [
            "dedupe_collection.py",
            "--root", str(tmp_path),
            "--quarantine", str(tmp_path / "q"),
            "--cache-db", str(tmp_path / "cache.db"),
            "--doublons-log", str(tmp_path / "d.csv"),
            "--dry-run",
        ])
        
        from dedupe_collection import main
        main()
        
        out = capfd.readouterr().out
        assert "(dry-run" in out
        assert "Groupes de doublons trouvés : 1" in out
        assert "Fichiers déplacés en quarantaine : 1" in out

    def test_real_run_moves_files(self, tmp_path, monkeypatch, capfd):
        """CLI réel déplace les fichiers."""
        (tmp_path / "a.jpg").write_bytes(b"same")
        (tmp_path / "b.jpg").write_bytes(b"same")
        
        import sys
        monkeypatch.setattr(sys, "argv", [
            "dedupe_collection.py",
            "--root", str(tmp_path),
            "--quarantine", str(tmp_path / "q"),
            "--cache-db", str(tmp_path / "cache.db"),
            "--doublons-log", str(tmp_path / "d.csv"),
        ])
        
        from dedupe_collection import main
        main()
        
        out = capfd.readouterr().out
        assert "dry-run" not in out
        assert "Groupes de doublons trouvés : 1" in out
        assert "Fichiers déplacés en quarantaine : 1" in out
        assert not (tmp_path / "a.jpg").exists() or not (tmp_path / "b.jpg").exists()