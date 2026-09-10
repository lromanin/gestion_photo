import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEBUG_MODE = os.getenv("DEBUG", "0") == "1"
LOG_LEVEL = __import__("logging").DEBUG if DEBUG_MODE else __import__("logging").INFO

logging = __import__("logging")
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s",
    level=LOG_LEVEL,
)
logger = logging.getLogger("gestion_photo_tests")
for _noisy_lib in ("httpx", "urllib3", "anthropic", "apscheduler", "telegram"):
    logging.getLogger(_noisy_lib).setLevel(logging.WARNING)


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
        logger.debug("Fichier média factice créé: %s", path)
        return path

    return _make
