"""Command-line interface: ``cnc-warmup <command>``.

Commands:
- ``list``: the configured machines and profiles.
- ``show-plan``: one warm-up's stage table and warnings, without writing anything.
- ``generate``: write programs, each verified by reading it back first.
- ``validate``: check that every machine/profile pair plans, renders and verifies.

Exit status: 0 on success, 1 for configuration or verification errors, 2 for
usage errors. Errors and warnings go to stderr.
"""

import argparse
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

from cnc_warmup import __version__
from cnc_warmup.config import Catalog, ConfigError, load_catalog
from cnc_warmup.issues import Issue
from cnc_warmup.model import Controller, SweepMove, ZStrokeAt
from cnc_warmup.plan import WarmupPlan, format_duration, format_number
from cnc_warmup.service import GenerationRequest, preview, write_programs

OK, FAILED = 0, 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return OK
    try:
        catalog = load_catalog(args.config)
    except ConfigError as error:
        return _fail(error.issues)
    handlers = {
        "list": _list,
        "show-plan": _show_plan,
        "generate": _generate,
        "validate": _validate,
    }
    return handlers[args.command](catalog, args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cnc-warmup",
        description=(
            "Generate CNC machine warm-up programs for Heidenhain TNC 640 (Klartext) "
            "and Fanuc 31i controls."
        ),
        epilog="Run 'cnc-warmup <command> --help' for a command's options.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    config = argparse.ArgumentParser(add_help=False)
    config.add_argument(
        "--config",
        type=Path,
        default=Path("config"),
        metavar="DIR",
        help="folder holding machines.toml and profiles.toml (default: config)",
    )
    overrides = argparse.ArgumentParser(add_help=False)
    overrides.add_argument(
        "--set",
        dest="overrides",
        type=parse_override,
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a profile setting for this run, e.g. --set coolant=flood "
        '(repeatable; values are TOML: 12000, true, flood, ["perimeter", "z_stroke"])',
    )

    commands.add_parser("list", parents=[config], help="show the configured machines and profiles")

    show = commands.add_parser(
        "show-plan",
        parents=[config, overrides],
        help="print a warm-up's stage table without writing programs",
    )
    show.add_argument("--machine", required=True, metavar="ID")
    show.add_argument(
        "--profile", metavar="NAME", help="default: the first profile in profiles.toml"
    )

    generate = commands.add_parser(
        "generate",
        parents=[config, overrides],
        help="write warm-up programs, each verified by reading it back first",
    )
    target = generate.add_mutually_exclusive_group(required=True)
    target.add_argument("--machine", nargs="+", metavar="ID", help="machines to generate for")
    target.add_argument(
        "--all", action="store_true", help="every machine, with every profile unless --profile"
    )
    generate.add_argument(
        "--profile", nargs="+", metavar="NAME", help="default: the first profile in profiles.toml"
    )
    generate.add_argument(
        "--controller",
        nargs="+",
        type=Controller,
        choices=list(Controller),
        metavar="{heidenhain,fanuc}",
        help="default: each machine's configured controller",
    )
    generate.add_argument(
        "-o",
        "--out",
        type=Path,
        default=Path("out"),
        metavar="DIR",
        help="output folder; programs go to DIR/<controller>/ (default: out)",
    )

    commands.add_parser(
        "validate",
        parents=[config],
        help="check every machine/profile pair plans, renders and verifies (for CI)",
    )
    return parser


def parse_override(text: str) -> tuple[str, object]:
    """Parse ``KEY=VALUE``. The value is read as TOML, or as a plain word: flood."""
    key, separator, raw = text.partition("=")
    if not separator or not key.strip():
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {text!r}")
    try:
        value: object = tomllib.loads(f"value = {raw}")["value"]
    except tomllib.TOMLDecodeError:
        value = raw.strip()
    return key.strip(), value


# --- Commands ------------------------------------------------------------------------------


def _list(catalog: Catalog, args: argparse.Namespace) -> int:
    machine_rows = []
    for machine in catalog.machines.values():
        travel = [
            f"{format_number(limits.min)}..{format_number(limits.max)}"
            for limits in (machine.x, machine.y, machine.z)
        ]
        fanuc = f"O{machine.fanuc.program_number:04d}" if machine.fanuc else "-"
        machine_rows.append(
            [
                machine.id,
                machine.controller.value,
                *travel,
                f"{format_number(machine.spindle_max_rpm)} rpm",
                f"{format_number(machine.max_feed)} mm/min",
                fanuc,
            ]
        )
    print(f"Machines ({(args.config / 'machines.toml').as_posix()}), in machine coordinates:")
    _table(["ID", "Controller", "X", "Y", "Z", "Spindle max", "Max feed", "Fanuc"], machine_rows)

    profile_rows = [
        [
            profile.name,
            f"{format_number(profile.duration_min)} min",
            str(profile.stages),
            f"{format_number(profile.rpm_start)}-{format_number(profile.rpm_end)}",
            f"{format_number(profile.feed_start)}-{format_number(profile.feed_end)}",
            profile.ramp.value,
            profile.coolant.value,
        ]
        for profile in catalog.profiles.values()
    ]
    print(f"\nProfiles ({(args.config / 'profiles.toml').as_posix()}):")
    _table(["Name", "Duration", "Stages", "RPM", "Feed mm/min", "Ramp", "Coolant"], profile_rows)
    return OK


def _show_plan(catalog: Catalog, args: argparse.Namespace) -> int:
    request = GenerationRequest(
        args.machine, args.profile or _first_profile(catalog), dict(args.overrides)
    )
    result = preview(catalog, request)
    if result.plan is None or result.errors:
        return _fail(result.issues)
    _print_plan(result.plan)
    _warn(result.warnings)
    return OK


def _generate(catalog: Catalog, args: argparse.Namespace) -> int:
    machine_ids = list(catalog.machines) if args.all else args.machine
    if args.profile:
        profile_names = args.profile
    else:
        profile_names = list(catalog.profiles) if args.all else [_first_profile(catalog)]
    controllers = tuple(args.controller or ())
    overrides = dict(args.overrides)

    results = [
        preview(catalog, GenerationRequest(machine_id, name, overrides, controllers))
        for machine_id in machine_ids
        for name in profile_names
    ]
    errors = [issue for result in results for issue in result.errors]
    if errors:
        print("Nothing written.", file=sys.stderr)
        return _fail(errors)

    rows = []
    for result in results:
        assert result.plan is not None  # no errors, so every request was planned
        for program, path in zip(result.programs, write_programs(result, args.out), strict=True):
            rows.append(
                [
                    result.request.machine_id,
                    result.request.profile_name,
                    program.controller.value,
                    path.as_posix(),
                    f"{program.text.count(chr(10))} lines",
                    f"est. {format_duration(result.plan.estimated_seconds)}",
                    "verified",
                ]
            )
    _table(["Machine", "Profile", "Controller", "File", "Size", "Run time", "Check"], rows)
    programs = "program" if len(rows) == 1 else "programs"
    print(f"\nWrote {len(rows)} {programs} to {args.out.as_posix()}/.")
    _warn([issue for result in results for issue in result.warnings])
    return OK


def _validate(catalog: Catalog, args: argparse.Namespace) -> int:
    """Plan, render and verify every machine/profile pair for every controller it supports."""
    results = []
    for machine in catalog.machines.values():
        controllers = (Controller.HEIDENHAIN,) + ((Controller.FANUC,) if machine.fanuc else ())
        for name in catalog.profiles:
            request = GenerationRequest(machine.id, name, controllers=controllers)
            results.append(preview(catalog, request))
    errors = [issue for result in results for issue in result.errors]
    if errors:
        return _fail(errors)
    programs = sum(len(result.programs) for result in results)
    print(
        f"Configuration OK: {len(catalog.machines)} machines, {len(catalog.profiles)} profiles. "
        f"All {programs} programs plan, render and verify."
    )
    _warn([issue for result in results for issue in result.warnings])
    return OK


# --- Output --------------------------------------------------------------------------------

_MOVE_NAMES = {
    SweepMove.PERIMETER: "perimeter",
    SweepMove.DIAGONALS: "XY diagonals",
}
_Z_STROKE_NAMES = {
    ZStrokeAt.CENTER: "Z stroke at the XY center",
    ZStrokeAt.START: "Z stroke at the start corner",
}


def _print_plan(plan: WarmupPlan) -> None:
    machine, profile, envelope = plan.machine, plan.profile, plan.envelope
    described = f" ({machine.description})" if machine.description else ""
    print(f"Machine {machine.id}{described}, profile {profile.name}")
    print(
        f"Sweep envelope X {format_number(envelope.x.min)}..{format_number(envelope.x.max)}  "
        f"Y {format_number(envelope.y.min)}..{format_number(envelope.y.max)}  "
        f"Z {format_number(envelope.z.min)}..{format_number(envelope.z.max)}  "
        f"({format_number(profile.edge_margin_mm)} mm inside travel)"
    )
    moves = ", ".join(
        _Z_STROKE_NAMES[profile.z_stroke_at] if move is SweepMove.Z_STROKE else _MOVE_NAMES[move]
        for move in profile.pattern
    )
    print(f"One sweep pass: {format_number(round(plan.sweep_length_mm))} mm ({moves})\n")
    _table(
        ["Stage", "RPM", "Feed", "Passes", "Pass time", "Dwell", "Stage time"],
        [
            [
                str(stage.number),
                format_number(stage.rpm),
                format_number(stage.feed),
                str(stage.passes),
                format_duration(stage.pass_seconds),
                f"{format_number(stage.dwell_seconds)} s",
                format_duration(stage.seconds),
            ]
            for stage in plan.stages
        ],
    )
    print(
        f"\nEstimated run time {format_duration(plan.estimated_seconds)}, plus rapid positioning."
    )


def _table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [max(len(cell) for cell in column) for column in zip(headers, *rows, strict=True)]
    for row in [headers, *rows]:
        print(
            "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        )


def _first_profile(catalog: Catalog) -> str:
    return next(iter(catalog.profiles))


def _fail(issues: Sequence[Issue]) -> int:
    for issue in issues:
        print(f"error: {issue}", file=sys.stderr)
    return FAILED


def _warn(issues: Sequence[Issue]) -> None:
    for issue in issues:
        print(f"warning: {issue}", file=sys.stderr)
