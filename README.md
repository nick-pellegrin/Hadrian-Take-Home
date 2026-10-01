# CNC Warm-Up Generator

Generates warm-up programs for CNC mills, for Heidenhain TNC 640 (Klartext, `.H`) and
Fanuc 31i (`.nc`) controls. Each program starts safely from wherever the machine is,
sweeps the full XYZ travel, and ramps the feed and spindle speed from a start value to a
finish value in stages, with optional flood coolant. Machines and warm-up settings live
in two config files, so nothing needs editing in the Python or the generated code.

The easiest way to use it is the browser UI: set up the machine and the warm-up, check
the program, and generate it, all on one page (see [Running it](#running-it)).

**Where to look first:**
- The generated programs for the three machines:
  [`examples/heidenhain/`](examples/heidenhain/) (e.g.
  [`WARMUP_M1_DAILY.H`](examples/heidenhain/WARMUP_M1_DAILY.H)) and
  [`examples/fanuc/`](examples/fanuc/).
- How they were produced: [`examples/RUN_LOG.md`](examples/RUN_LOG.md), an example run of
  every command.
- Why they look the way they do: [`docs/DESIGN.md`](docs/DESIGN.md).

## What's in the project

| Folder | What it holds |
|---|---|
| `config/` | `machines.toml` (each machine's travel, spindle and feed limits, Fanuc settings) and `profiles.toml` (warm-up settings: duration, stages, feed and spindle ramps, coolant) |
| `src/cnc_warmup/` | The generator, in Python. Its core uses only the standard library; the optional UI uses NiceGUI |
| `examples/` | Generated programs for the three example machines on both controls, and `RUN_LOG.md`, an example run of every command |
| `tests/` | The test suite (pytest) |
| `docs/` | [`DESIGN.md`](docs/DESIGN.md) (design decisions and the controller syntax the programs rely on), [`UI_SPEC.md`](docs/UI_SPEC.md) (the browser UI) and [`spike/`](docs/spike/README.md) (small programs for checking that syntax on a TNC 640 programming station) |

Inside `src/cnc_warmup/`:

- `config.py` loads and checks the config files.
- `plan.py` and `ramp.py` turn a machine and a profile into a warm-up plan: the stages, their speeds and feeds, and the sweep path.
- `posts/` writes the plan as a Heidenhain or Fanuc program.
- `verify/` reads every generated program back with a small simulator of each control and checks that it matches the plan before the program is saved.
- `cli.py` is the command line, and `ui/` is an optional browser UI for editing machines and profiles and generating programs.

## Setup

The project uses [uv](https://docs.astral.sh/uv/), which also installs the right Python
(3.11 or newer) if you don't have it.

1. Install uv.

   **Windows** (PowerShell):

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

   **macOS / Linux** (Terminal):

   ```sh
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

   Then open a new terminal so the `uv` command is found.

2. In the project folder, install the project and its tools:

   ```sh
   uv sync
   ```

The commands below are the same on Windows, macOS and Linux.

## Running it

### In the browser (recommended)

```sh
uv run cnc-warmup ui
```

This opens the configurator in your browser at `http://127.0.0.1:8080`. It works in
three steps:

1. **Machine:** pick a saved machine (M1, M2 and M3 come with the project), or adjust
   its travel and limits.
2. **Warm-up profile:** pick a profile and set the feed and spindle ramps, the duration
   and coolant. Less common settings are under **Advanced**.
3. **Generate:** tick Heidenhain and/or Fanuc and click **Generate files**. The programs
   are written to `out/<controller>/`.

The **Output** button at the top right opens a summary of the warm-up and the generated
programs, which update as you edit. Problems show under the field they belong to.
Generating uses what's on screen, saved or not. **Save changes** writes your edits to
`config/`, so they're there next time and from the command line. Stop the UI with
Ctrl+C in the terminal.

### From the command line

The same generator runs as commands, for scripting or CI:

```sh
uv run cnc-warmup list                          # the configured machines and profiles
uv run cnc-warmup show-plan --machine M1        # a warm-up's stage table, without writing files
uv run cnc-warmup generate --machine M1         # writes out/heidenhain/WARMUP_M1_DAILY.H
uv run cnc-warmup generate --all --controller heidenhain fanuc   # every machine and profile, both controls
uv run cnc-warmup generate --machine M2 --profile extended --set coolant=flood   # a one-off override
```

Run `uv run cnc-warmup <command> --help` for all the options; `examples/RUN_LOG.md` shows
the output of each command.

## Before running a program on a machine

Each program stops for an operator checklist before the spindle starts (unless that's
turned off in the profile). Still, read the program and dry-run it in the control's
simulation first. [`docs/DESIGN.md`](docs/DESIGN.md#controller-syntax-verification) lists
the controller syntax the programs rely on and where each item comes from, and
[`docs/spike/README.md`](docs/spike/README.md) explains how to check the Heidenhain
programs in HEIDENHAIN's free TNC 640 programming station.

## Development

```sh
uv run pytest       # tests
uv run ruff check   # lint
uv run mypy         # type check
```
