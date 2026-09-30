"""Application service shared by the CLI and the UI.

One request -> one preview: validate the request, build the plan, render every
requested controller, and verify each program by reading it back. Problems in the
request come back as Issues rather than exceptions, so the CLI can print them and
a UI can show each one next to the field it concerns. Nothing is written until
`write_programs`, which refuses a preview with errors.
"""

import json
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from cnc_warmup.config import (
    Catalog,
    ConfigError,
    apply_overrides,
    parse_machines,
    parse_profiles,
)
from cnc_warmup.issues import Issue, Severity
from cnc_warmup.model import Controller, Machine, WarmupProfile
from cnc_warmup.plan import WarmupPlan, build_plan
from cnc_warmup.posts import render
from cnc_warmup.posts.base import Program
from cnc_warmup.verify import verify


@dataclass(frozen=True)
class GenerationRequest:
    machine_id: str
    profile_name: str
    overrides: Mapping[str, object] = field(default_factory=dict)  # profiles.toml keys
    controllers: tuple[Controller, ...] = ()  # empty: the machine's configured controller


@dataclass(frozen=True)
class Preview:
    """Everything a request produces, before anything is written."""

    request: GenerationRequest
    plan: WarmupPlan | None  # None when the request itself is invalid
    programs: tuple[Program, ...]
    issues: tuple[Issue, ...]

    @property
    def errors(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if issue.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[Issue, ...]:
        return tuple(issue for issue in self.issues if issue.severity is Severity.WARNING)


def preview(catalog: Catalog, request: GenerationRequest) -> Preview:
    """Plan, render and verify a request."""
    try:
        machine = catalog.machine(request.machine_id)
        profile = apply_overrides(catalog.profile(request.profile_name), request.overrides)
        plan = build_plan(machine, profile)
        controllers = request.controllers or (machine.controller,)
        programs = tuple(render(plan, controller) for controller in controllers)
    except ConfigError as error:
        return Preview(request, None, (), error.issues)

    mismatches = tuple(
        Issue(f"programs.{program.filename}", f"failed verification: {problem}")
        for program in programs
        for problem in verify(plan, program)
    )
    return Preview(request, plan, programs, (*plan.warnings, *mismatches))


def preview_config(
    machine_id: str,
    machine: Mapping[str, object],
    profile_name: str,
    profile: Mapping[str, object],
    controllers: tuple[Controller, ...] = (),
) -> Preview:
    """Preview unsaved machine and profile tables, e.g. from the UI's forms.

    The tables are validated exactly like machines.toml and profiles.toml, then
    planned, rendered and verified like any saved configuration.
    """
    request = GenerationRequest(machine_id, profile_name, controllers=controllers)
    issues: list[Issue] = []
    machines: dict[str, Machine] = {}
    profiles: dict[str, WarmupProfile] = {}
    try:
        machines = parse_machines({"machines": {machine_id: dict(machine)}}, source=None)
    except ConfigError as error:
        issues += error.issues
    try:
        profiles = parse_profiles({"profiles": {profile_name: dict(profile)}}, source=None)
    except ConfigError as error:
        issues += error.issues
    if issues:
        return Preview(request, None, (), tuple(issues))
    return preview(Catalog(machines, profiles), request)


def write_programs(result: Preview, out_dir: Path) -> list[Path]:
    """Write each program to ``<out_dir>/<controller>/<filename>`` and return the paths."""
    if result.errors:
        raise ValueError("refusing to write programs from a preview with errors")
    paths = []
    for program in result.programs:
        path = out_dir / program.controller.value / program.filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(program.data)  # bytes: keep each dialect's line endings exactly
        paths.append(path)
    return paths


def cli_command(request: GenerationRequest) -> str:
    """The shell command that generates the same programs as `request`."""
    args = ["uv", "run", "cnc-warmup", "generate", "--machine", request.machine_id]
    args += ["--profile", request.profile_name]
    if request.controllers:
        args += ["--controller", *(controller.value for controller in request.controllers)]
    for key, value in request.overrides.items():
        args += ["--set", f"{key}={toml_literal(value)}"]
    return shlex.join(args)


_BARE_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TOML_KEYWORDS = {"true", "false", "inf", "nan"}


def toml_literal(value: object, *, bare_strings: bool = True) -> str:
    """Write an override value the way `--set` reads it back: 12000, true, flood, [..]."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # Exact, so the command reproduces the same programs: 12000.0 -> 12000, 0.1 -> 0.1.
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, str):
        if bare_strings and _BARE_WORD.fullmatch(value) and value not in _TOML_KEYWORDS:
            return value
        # A JSON string is a TOML basic string, except that TOML also forbids a raw DEL.
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, list | tuple):
        return "[" + ", ".join(toml_literal(item, bare_strings=False) for item in value) + "]"
    raise TypeError(f"cannot write {value!r} as an override")
