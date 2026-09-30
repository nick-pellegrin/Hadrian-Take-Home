"""examples/RUN_LOG.md: the example execution, captured from real CLI runs.

The documented commands run in a scratch copy of the project. Their output must
match RUN_LOG.md, and the programs they write must match examples/ byte for
byte, so the log can never drift from what the tool really does. Regenerate
with `uv run pytest --update-golden`.
"""

import shutil
from pathlib import Path

import pytest

from cnc_warmup.cli import main

ROOT = Path(__file__).resolve().parents[1]
RUN_LOG = ROOT / "examples" / "RUN_LOG.md"

COMMANDS = [
    ("Version.", ["--version"]),
    ("The configured machines and warm-up profiles.", ["list"]),
    ("Preview a warm-up without writing anything.", ["show-plan", "--machine", "M1"]),
    (
        "Generate every example program: 3 machines x 2 profiles x 2 controllers. Each program "
        "is read back by a simulated control and checked against its plan before it is written.",
        ["generate", "--all", "--controller", "heidenhain", "fanuc", "-o", "examples"],
    ),
    (
        "Per-run overrides, without editing any file: flood coolant and a final rapid pass.",
        [
            "generate",
            "--machine",
            "M2",
            "--set",
            "coolant=flood",
            "--set",
            "final_rapid_pass=true",
            "-o",
            "out",
        ],
    ),
    (
        "A feed too slow for the largest machine: the stage overruns, so there is a warning.",
        ["show-plan", "--machine", "M3", "--set", "feed_start=1000"],
    ),
    (
        "A request the machine cannot run is refused, and nothing is written.",
        ["generate", "--machine", "M1", "--set", "feed_end=30000"],
    ),
    ("Check the whole configuration, e.g. in CI.", ["validate"]),
]

HEADER = """\
# Example run

Output of each command, captured from real runs by `tests/test_run_log.py` (which
fails if this file is out of date). Programs land in `<out>/<controller>/`; the
`examples/heidenhain/` and `examples/fanuc/` programs were written by the
`generate --all` command below.
"""


def test_run_log_is_up_to_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    update_golden: bool,
) -> None:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    monkeypatch.chdir(tmp_path)

    sections = [HEADER]
    for description, argv in COMMANDS:
        try:
            status = main(argv)
        except SystemExit as exit_info:  # --version exits from argparse
            status = int(exit_info.code or 0)
        out, err = capsys.readouterr()
        transcript = f"$ uv run cnc-warmup {' '.join(argv)}\n{out}{err}"
        if status:
            transcript += f"[exit status {status}]\n"
        sections.append(f"{description}\n\n```console\n{transcript.rstrip()}\n```\n")
    log = "\n".join(sections)

    for program in (tmp_path / "examples").rglob("*.*"):
        committed = ROOT / "examples" / program.relative_to(tmp_path / "examples")
        assert program.read_bytes() == committed.read_bytes(), f"{committed} differs"

    if update_golden:
        RUN_LOG.write_text(log, encoding="utf-8", newline="\n")
    assert RUN_LOG.read_text(encoding="utf-8") == log, (
        "outdated: run `uv run pytest --update-golden`"
    )
