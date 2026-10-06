"""Every copied domain test runs beneath its own disposable data root."""
import os
from pathlib import Path
import tempfile


def pytest_configure(config):
    root = Path(tempfile.mkdtemp(prefix="publicgex-backend-tests-"))
    config.option.basetemp = str(root / "pytest")
    os.environ["PUBLICGEX_DATA_DIR"] = str(root)
    os.environ["PUBLICGEX_DEMO"] = "0"

