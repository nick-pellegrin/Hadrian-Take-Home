from pathlib import Path

import pytest

from cnc_warmup.config import Catalog, load_catalog

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture(scope="session")
def catalog() -> Catalog:
    """The configuration shipped in config/."""
    return load_catalog(CONFIG_DIR)
