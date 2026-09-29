import subprocess
import sys

import pytest

from cnc_warmup import __version__
from cnc_warmup.cli import main


def test_version_flag_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"cnc-warmup {__version__}"


def test_no_arguments_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert capsys.readouterr().out.startswith("usage: cnc-warmup")


def test_module_entry_point_runs() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cnc_warmup", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == f"cnc-warmup {__version__}"
