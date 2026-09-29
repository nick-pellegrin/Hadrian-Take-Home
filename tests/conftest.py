from pathlib import Path

import pytest

from cnc_warmup.config import Catalog, load_catalog

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
EXAMPLES_DIR = ROOT / "examples"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-golden",
        action="store_true",
        help="rewrite the example programs in examples/ from the current generator output",
    )


@pytest.fixture(scope="session")
def catalog() -> Catalog:
    """The configuration shipped in config/."""
    return load_catalog(CONFIG_DIR)


@pytest.fixture(scope="session")
def update_golden(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--update-golden"))
