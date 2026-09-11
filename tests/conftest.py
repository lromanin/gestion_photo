"""Conftest partagé — fixtures et sys.path pour les tests."""
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("DEBUG", "0")


@pytest.fixture
def media_dir(tmp_path):
    d = tmp_path / "photos"
    d.mkdir()
    return d


@pytest.fixture
def make_media_file(media_dir):
    def _make(relpath, content=b"\x00\x01\x02img", mtime=None):
        path = media_dir / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if mtime is not None:
            epoch = mtime.timestamp()
            os.utime(path, (epoch, epoch))
        return path

    return _make


@pytest.fixture
def cache_conn():
    import photo_common
    conn = photo_common.open_cache_db(":memory:")
    yield conn
    conn.close()
