import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="pidash-test-")
os.environ.update(DATA_DIR=_tmp, DASH_PASSWORD="segreta", HOST_ROOT="/nonexistent",
                  LIVE_INTERVAL="0.2", SAMPLE_INTERVAL="1", RETENTION_DAYS="30", RAW_RETENTION_HOURS="6")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app import db  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path):
    db.init(tmp_path / "t.db")
    yield db
