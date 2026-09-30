"""The generator core runs without the optional UI packages."""

import subprocess
import sys
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"

_CHECK = """
import sys
from cnc_warmup.cli import main

config, out = sys.argv[1], sys.argv[2]
assert main(["validate", "--config", config]) == 0
assert main(["generate", "--all", "--controller", "heidenhain", "fanuc", "--config", config,
             "-o", out]) == 0
ui_packages = {"nicegui", "tomlkit", "fastapi", "uvicorn", "starlette"}
leaked = sorted(name for name in sys.modules if name.split(".")[0] in ui_packages)
assert not leaked, f"core imported UI packages: {leaked}"
"""


def test_core_never_imports_the_ui_packages(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _CHECK, str(CONFIG_DIR), str(tmp_path)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
