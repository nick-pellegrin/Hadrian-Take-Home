# Design notes

The design decisions behind the warm-up generator, and the evidence for them. The
generator runs as a pipeline, and each section below covers one stage:

```
config/*.toml ─► config ─► plan ─► posts (Heidenhain, Fanuc) ─► verify ─► files
                                        ▲
                         service (shared by the CLI and the UI)
```

- [Configuration](#configuration): loading and validating machines and profiles.
- [Planner](#planner): the controller-neutral warm-up plan, where every safety decision is made.
- [Heidenhain TNC 640 post](#heidenhain-tnc-640-post) and [Fanuc 31i post](#fanuc-31i-post): writing the plan as NC code.
- [Round-trip verification](#round-trip-verification): reading each program back and checking it against the plan.
- [Service layer and CLI](#service-layer-and-cli) and [Configurator UI](#configurator-ui): the two front ends.
- [Controller syntax verification](#controller-syntax-verification): each controller construct the programs rely on, and its source.

## Configuration

Machines (`config/machines.toml`) and warm-up profiles (`config/profiles.toml`) are data.
Supporting a new machine means adding a TOML table, never editing Python or NC code.
`cnc_warmup.config` turns both files into the frozen dataclasses in `cnc_warmup.model`.

| Decision | Why |
|---|---|
| **TOML**, read with stdlib `tomllib` | It allows comments, which config files full of assumptions need. It is readable by non-programmers and adds no dependency |
| **Unknown keys are errors**, with a "did you mean" hint | A typo such as `max_feeds` must not silently fall back to a default in a program that moves a machine |
| **Every problem is reported in one pass**, each with its file and dotted TOML path | Fix everything in one edit, not one error per run. The path also lets the UI put each message under its form field |
| One problem is reported once, with no knock-on errors | A wrong-type table (`travel = 762`) doesn't also produce "missing x/y/z". Tests assert an exact single issue per case |
| Axis limits are **machine coordinates** (the Heidenhain M91 / Fanuc G53 frame) | This is what makes the program independent of presets, work offsets and tool offsets |
| `travel` + `home` shorthand **or** explicit `limits`, never both | Matches the assignment table (travel only) while supporting machines whose datum isn't at an end of travel |
| `home` is required with `travel` | The coordinate convention is safety-relevant, so it is never assumed silently |
| Fanuc cancel codes come from a **fixed list** (`G15`, `G50`, `G50.1`, `G69`) | Each needs a control option, so a machine opts in. Free text is never copied into a program |
| Fanuc O9000–O9999 rejected | That range holds the machine tool builder's macros |
| Profile defaults are declared once, on the `WarmupProfile` dataclass | The loader reads them from the dataclass, so the defaults can't drift apart |
| A warm-up only ramps up (`*_start ≤ *_end`), and the sweep pattern must reach all six axis extremes | These encode the assignment's requirements: gradual warm-up, and entire XYZ travel |
| Machine-dependent limits are checked separately (`check_compatibility`) | Profiles are reusable across the fleet. Only the machine/profile pair can be checked against max feed, spindle speed and travel |
| CLI `--set` and UI edits go through `apply_overrides`, which re-runs the profile validation | There is one validation path, so an override can never bypass a check |

## Planner

`cnc_warmup.plan.build_plan(machine, profile)` returns a controller-neutral `WarmupPlan`.
It contains an ordered list of **steps** (the program), one **sweep pass** that every
stage repeats, and a **stage table** (RPM, feed, passes, dwell). The post-processors only
translate it into Klartext or Fanuc code. Every safety-relevant decision is therefore made
once, here, for both controls.

```
safe start ─► [runtime guard] ─► [operator stop] ─► retract Z ─► XY to start corner
   ─► spindle on [+ coolant] ─► stages 1..N ─► [rapid pass] ─► [coolant off]
   ─► spindle stop ─► retract Z ─► park XY ─► end
```

| Decision | Why |
|---|---|
| The plan is an explicit, ordered list of steps | The order is the safety property. It is tested once, on the plan, rather than separately in each dialect |
| Section headings (`Section` steps: safe start, travel check, stage n, shutdown) are part of the plan | Both posts get the same structure and wording. The stage titles carry the RPM, feed, passes and dwell |
| The guard and the operator checklist come **before any motion**, and the spindle starts only after the retract | Nothing moves before the program has checked it is on the right machine and a person has confirmed the checklist. Nothing spins before Z is clear |
| The first move is **Z only**, straight up to the top of travel | Z-up is the one move that is safe from any starting position. XY follows at the top |
| All positions are machine coordinates, inside an **envelope**: the travel inset by `edge_margin_mm` | Position doesn't depend on presets or offsets. The margin keeps servo overshoot from tripping an overtravel alarm |
| The envelope is rounded **inward** to 0.001 mm | A property-based test found that rounding to nearest could push the envelope 0.0004 mm past the margin, so it now rounds inward. The config also requires at least 1 mm of travel left after the margins |
| One **closed** sweep pass that reaches all six axis extremes | Passes chain without repositioning, every pass covers the entire travel, and the Heidenhain post can render it once as a subprogram |
| Sweep = perimeter (X and Y strokes) + XY diagonals (two drives interpolating together) + Z stroke | Covers each axis end to end, plus simultaneous multi-axis motion. The Z stroke runs over the XY center by default, or at the start corner if configured |
| **Time-based stages:** each stage holds its RPM for `duration_min / stages`. The axes repeat whole passes at the stage feed, then a spindle-only dwell fills the remainder | Spindle warm-up needs time at each speed, while the axes should keep moving. Stages end within 1 s of their share |
| A stage whose single pass overruns its share is a **warning**, not an error | The program is still safe, just longer. The warning names the stage and the fix (raise `feed_start` or `duration_min`) |
| Intermediate feeds and RPMs are rounded to 10, endpoints kept exactly | Readable programs (`S3250`, `F4880`), while honoring the configured start and finish values. Rounding can never break monotonicity |
| The runtime guard checks that the **commanded envelope** fits inside the control's soft limits | This is the exact safety condition. It catches a program generated for a larger machine. A program for a *smaller* machine still passes, which is safe but sweeps less than the full travel |
| Park at the XY center with Z at the top | A neutral position that leaves the table accessible |
| The runtime estimate covers the stages only | Rapid positioning and acceleration are excluded, and the optional rapid pass is estimated at `max_feed`, because the machine's rapid rate isn't configured. The estimate is labeled as such in the output |
| `trace(plan)` flattens the plan into moves and dwells | Property tests check the safety invariants on it across random machines and profiles. The round-trip verifier compares it with the programs read back from each post |

## Heidenhain TNC 640 post

`cnc_warmup.posts.heidenhain.render(plan)` writes the plan as a Klartext program. The
examples for M1–M3 are in `examples/heidenhain/`. The golden tests pin them byte for
byte; regenerate them with `uv run pytest --update-golden`.

| Decision | Why |
|---|---|
| Klartext (`.H`), not the TNC's ISO dialect (`.I`) | The assignment asks for Klartext. It is also the TNC's native, most readable format |
| Every move is `L … R0 FMAX\|FQL1 M91` | M91 = machine coordinates, ignoring presets, datum shifts and tool length (manual §7.3). R0 because radius compensation would otherwise still apply |
| No reset block at the start | M91 already ignores every coordinate transformation. `PLANE RESET` is avoided because tilting (option 8) may be missing on a 3-axis machine |
| The sweep is written **once**, as `LBL "SWEEP"` after `M30`. Each stage sets `QL1 = <feed>` and calls it | Short, idiomatic program. The stage table reads straight from the main program. QL parameters are local to the program, so they can't collide with Q parameters used by OEM or HEIDENHAIN cycles |
| Repeated passes use a program-section repeat (`LBL n` … `CALL LBL n REP passes-1`) | REP counts *extra* runs (manual §8.3). Stage numbers make unique, non-zero labels |
| Speed changes use `TOOL CALL S…` with no tool number, and a redundant change is skipped | With no tool number or axis, the control changes only the speed and never runs the tool-change macro (manual §4.1) |
| Dwell uses Cycle 9 (`CYCL DEF 9.0/9.1`) | Supported by every TNC 640 software version. `FUNCTION DWELL` needs newer software |
| Travel check: `FN 18 ID230 NR2/NR3` reads the soft limits into QL10–QL15, and `FN 11`/`FN 12` jump to `LBL "TRAVEL_ERR"`, which raises `FN 14: ERROR = 1004` ("Range exceeded") | Runs before anything moves. The values stay visible in the Q-parameter status display, which helps diagnose a mismatch |
| The operator `STOP` comes before the spindle starts | `M0`/`STOP` would stop a running spindle. Nothing is running yet, so the program resumes cleanly on NC start |
| The program name is `WARMUP_<MACHINE>_<PROFILE>`, and the file name matches `BEGIN`/`END PGM` | Profiles don't collide on the control, and the TNC finds the name it expects |
| ASCII only, CRLF, upper-case comments with `;`, `~` and quotes removed | `;` would open a nested comment, a trailing `~` continues a block, and quotes delimit labels. CRLF is the TNC's native line ending |
| The header lists the settings, the sweep envelope and the stage table, but **no timestamp** | The operator sees what will run. Regenerating produces identical bytes, so diffs and golden tests only show real changes |
| Section headings are structure items (`* - …`) | They appear in the TNC's program structure window, so each stage can be found at a glance |

**Size:** the programs are 106 blocks (daily) to 116 blocks (the 6-stage extended
profile), or 87–97 with `runtime_guards = false`. A TNC 640 has no length limit, but the
free demo of the programming station only saves programs of up to 100 blocks. To try the
programs there, generate them with the travel check turned off; the check itself is
covered by the test programs in [`spike/`](spike/README.md).

**If the programming station rejects a construct,** the change is small (see the table
below): the feed parameter is one constant (`FEED_PARAM`, e.g. `Q1600` instead of
`QL1`), the speed change is written in two lines of the post (`TOOL CALL Z S…` instead
of `TOOL CALL S…`), and `runtime_guards = false` removes the `FN 18` travel check.

## Fanuc 31i post

`cnc_warmup.posts.fanuc.render(plan)` writes the same plan as an ISO G-code program. The
examples are in `examples/fanuc/` and are pinned byte for byte, like the Heidenhain ones.

| Decision | Why |
|---|---|
| Positioning moves are always `G90 G53 G00` | `G53` means machine coordinates, so no work offset (G54–G59, G52, external offset) applies. It is one-shot and **ignored in G91**, so it is always written together with `G90` |
| Sweep feed moves are `G91 G01` increments, starting right after a `G53` move to the start corner | `G53` always moves at rapid, so it can't do feed moves. An increment from a machine-coordinate point is just as independent of work offsets. Increments are differences of points on the 0.001 mm grid, so no rounding error accumulates, and every pass returns exactly to its start |
| The retract uses `G90 G53 G00 Z<top>`, not `G91 G28 Z0.` | `G28` goes to the reference point, which is the top of Z only when home is at the + end. Using the configured top keeps Fanuc identical to Heidenhain's M91 behavior for every `home`/`limits` setting |
| Safe start is `G21` on its own line, then `G17 G40 G49 G80 G90 G94`, then the machine's opted-in cancel codes (`G15`, `G50`, `G50.1 X0. Y0. Z0.`, `G69`) | Units come first, before any coordinates. Length compensation and cutter compensation are cancelled. Rotation, scaling and mirroring would distort G91 increments, but their cancel codes raise alarms on controls without the option, so each machine opts in |
| Every coordinate has a decimal point (`X760.`). `S` is whole rpm. Dwell is `G04 P<ms>` | With parameter 3401#0 = 0, `X760` means 0.760 mm. `G04 P` takes integer milliseconds and avoids any doubt that a dwell `X` might move the X axis during G91 |
| Passes are written out in full, with no macro loops or `M98` subprograms | Apart from the optional travel check, the program needs no Custom Macro, and any backplotter can read it. `M98 Q` local subprograms need parameter 6005#0. The cost is longer files (~300 lines for M1 daily) |
| **Each stage re-anchors**, Z first (`N100 G90 G53 G00 Z<top>`, then `G90 G53 G00 X… Y…`), restates `S… M03` and coolant, and starts a new `G91` run | Any stage N-number is a safe restart point: after a restart from a low Z, Z clears the travel before X and Y move, as at the program start. In a normal run both moves go nowhere. The header warns never to restart mid-pass, because the passes are incremental |
| Travel check: `IF [PRM[1321]/[n] GT <min>] THEN #3000=1(SWEEP EXCEEDS X- LIMIT)`, and the same with 1320/LT for the + side | Same safety condition as Heidenhain: the stored stroke limits must contain the sweep. It needs Custom Macro plus `PRM[]` (30i-B family), so it stays behind `runtime_guards`. Messages are kept to 26 characters or fewer for older alarm displays |
| The operator check is a plain `M00`, with the checklist as comments | Universal: needs no macro option. It comes before the spindle starts |
| The final rapid pass is `G91 G00` | Fanuc `G00` may move each axis independently (a dog-leg path) unless parameter 1401#1 is set. The corner-to-corner moves still stay inside the envelope rectangle |
| Comments are upper-case ASCII, with parentheses, `;`, `%` and `:` removed. Durations are written `19M57S` | Parentheses would end the comment. `%` is end-of-tape and `;` is end-of-block to some transfer tools. `:` is an alternative program-number address |
| The program is `O<number>` from `[machines.<id>.fanuc]`, and the file is `O8001_M1_DAILY.nc`, with LF line endings | O-numbers are per machine, so both profiles of a machine share `O8001`: load one at a time. `.gitattributes` keeps `.nc` files byte for byte |

**Tests:** the Fanuc linter in `tests/test_fanuc.py` simulates the program's moves. It
fails:
- any axis move in work coordinates;
- any `G53` not written with `G90`;
- any axis word without a decimal point;
- any feed move without a feed rate or a running spindle;
- any position outside the sweep envelope.

It runs on the examples and on 100 random plans, and is itself mutation-tested with 14
kinds of broken program.

## Round-trip verification

`cnc_warmup.verify.verify(plan, program)` reads a generated program back and checks it
against the plan:
1. It uses a small interpreter for that program's dialect.
2. It runs the program on a simulated machine that tracks position, spindle and coolant, with
   soft limits set to the machine's configured travel.
3. It compares every move and dwell with `plan.trace()`. An empty result means the program
   does exactly what the plan says.

All 12 shipped programs, and both posts' output for 100 random plans per test run, pass.

| Decision | Why |
|---|---|
| Programs are **executed**, not just pattern-matched | Only running a program catches a wrong repeat count, a subprogram that doesn't return, a feed read from the wrong parameter, or a modal slip. The format linters in the tests still check layout rules |
| The readers copy the control's semantics, including its traps | Heidenhain: `REP` counts extra runs, subprograms return at `LBL 0`, and `FN 18` reads the soft limits. Fanuc: `G53` is one-shot, always rapid and **ignored in `G91`**, and `G00`/`G01`/`G90`/`G91` are modal. So a `G53` missing its `G90` shows up as a wrong position, just as it would on the machine |
| A strict subset: anything else is a `ReaderError` | The verifier only vouches for what it fully understands. A Fanuc move in work coordinates (`G90` without `G53`) is rejected as unverifiable |
| Moves that go nowhere are ignored in the comparison | Fanuc's per-stage re-anchoring and a retract when Z is already up change nothing on the machine |
| Tolerances: position 1e-6 mm, feed 0.001 mm/min, spindle 0.5 rpm, dwell 0.001 s | These match the resolution the posts write. Fanuc `S` words are whole rpm |
| The travel checks are **executed** against simulated soft limits | For every axis, side and controller, a soft limit 0.5 mm inside the sweep must stop the program before its first move. Limits exactly equal to the sweep must let it run |

**The safety argument in one line:** property tests prove the plan's invariants on
`trace()` (inside the travel, full travel swept, ramps rising, safe start and end, coolant
only when configured). Round-trip tests prove the programs reproduce `trace()` exactly. So
the programs have the invariants too.

**What it does not prove:** that the real control accepts the syntax (checked with the
test programs in [`spike/`](spike/README.md) on a TNC 640 programming station), or
behavior that depends on the machine (the frame of the `FN 18` values, the units of
`PRM`). Those are listed in [Controller syntax verification](#controller-syntax-verification).

## Service layer and CLI

`cnc_warmup.service` is the application layer. Both the CLI and the UI use it, so
neither front end holds any generation logic.

- `preview(catalog, request)` validates the request, builds the plan, renders each
  requested controller, and **verifies every program by round-trip**. Problems come back
  as `Issue`s (errors and warnings, each with its TOML path) rather than exceptions.
- `write_programs(preview, out)` writes to `<out>/<controller>/<file>`, and refuses a
  preview that has any error.
- `cli_command(request)` returns the exact shell command that reproduces the request.

| Command | What it does |
|---|---|
| `cnc-warmup list` | The configured machines (travel in machine coordinates, limits, Fanuc O-number) and profiles |
| `cnc-warmup show-plan --machine M1 [--profile …] [--set …]` | The stage table, sweep envelope, pass length and estimate, with warnings. Writes nothing |
| `cnc-warmup generate (--machine ID… \| --all) [--profile …] [--controller heidenhain fanuc] [--set …] [-o DIR]` | Writes verified programs. If any request fails, **nothing is written** |
| `cnc-warmup validate` | Plans, renders and verifies every machine × profile for every controller the machine supports. Exit status 0/1, for CI |

| Decision | Why |
|---|---|
| Every program is verified **before** it is written | A post-processor bug becomes a refused generation, not a program on a machine |
| `--set key=value` overrides are read as TOML values, or as a bare word (`coolant=flood`), and go through `apply_overrides` | They use the same syntax as `profiles.toml` and pass the same validation. `cli_command` writes values in a form `--set` reads back exactly (tested with random values) |
| All-or-nothing `generate` | A batch never ends up half written, with a mix of old and new programs |
| Exit status: 0 = OK, 1 = config or verification errors, 2 = usage. Errors and warnings go to stderr | Scriptable and CI-friendly |
| Output uses POSIX paths, and a single program says "program" | The output reads the same on Windows and Linux, which the run log depends on |
| `examples/RUN_LOG.md` is **generated by a test** that runs the documented commands in a scratch copy of the project | The "example execution" can never drift from the real tool. The same test checks that `generate --all` reproduces `examples/` byte for byte |

## Configurator UI

`cnc-warmup ui` opens a local NiceGUI page for editing machines and profiles and
generating programs. The full specification and implementation notes are in
[`UI_SPEC.md`](UI_SPEC.md).

| Decision | Why |
|---|---|
| NiceGUI, as an **optional extra**. The core never imports it, and a test enforces that | The generator stays stdlib-only for the CLI and CI. The UI is a convenience layer, not a dependency |
| The page's settings are passed into a **root function** (`ui.run(root)`), with no module globals | Each browser tab gets its own `Editor`. Tests run the real page with NiceGUI's simulated user against a scratch config |
| **All generation goes through `service.preview_config`** | The UI can't produce anything the CLI couldn't. Every program on screen has passed round-trip verification |
| The core validator is the only source of field errors, and NiceGUI's auto-validation is disabled | One set of rules and messages for the TOML files, the CLI and the UI |
| Saving edits TOML **in place** with tomlkit (key by key, bytes in and out). Only changed values are rewritten; each keeps the file's number style (a float stays a float) and its inline comment's column | The files stay human-edited documents: comments, alignment, inline tables and line endings survive a UI save, and saving without changes is a no-op diff |
| Renaming rewrites only the entry's `[section.name]` header lines, then re-parses to check nothing else changed | A rename is a two-line diff: the entry keeps its place and comments. Files in another layout fall back to moving the table to the new key |
| Three numbered steps (Machine, Warm-up profile, Generate). Each card shows the settings most warm-ups change; the rest are in a collapsed **Advanced** section | Feed, speed and duration change often. Machine conventions and safety switches are set once. Showing everything at once made the page read like a config file |
| The three cards sit side by side, equal in height and centered on the page. The output (summary, then the programs) is in a right-hand drawer opened from the header's **Output** button, which shows a red problem count while closed | The settings get the page; the output is one click away. The drawer pushes the cards aside instead of covering them, so the preview stays live while editing |
| One **Save changes / Discard** footer for both cards, shown only while something is unsaved | One place to save, and unsaved work is hard to miss. Save writes only what changed, and nothing if a changed entry is invalid |
| Machine coordinates are asked as one question, **"Where is machine zero?"** (+ end, − end, or custom limits) | It's what an engineer can answer at the machine. The form converts between `travel` + `home` and explicit `limits` |
| Names are edited in a validated dialog (✏), not as a form field. Save (just this card), Duplicate and Delete sit in a ⋮ menu | The name is an identity, not a setting. The dialog applies the config's naming rule and rejects names already in use, so a save can never overwrite another entry |
| The command-line equivalent is shown for saved machines, with profile edits as `--set` overrides | Anything done in the UI can be scripted or reproduced |

## Controller syntax verification

Every controller construct the generated programs depend on, where it comes from, and
how far it has been confirmed.

**Programming-station check (1 October 2026).** On the TNC 640 programming station, NC
software 340595 18 SP4 (free demo), these programs were loaded and run in Test Run:
- `SPK01_LIMITS.H` and `SPK04_GUARD.H` from [`spike/`](spike/README.md);
- all six warm-up programs (M1–M3, daily and extended), generated with
  `runtime_guards = false` so they fit the demo's 100-block limit (87–97 blocks).

Every program loaded with no `ERROR` blocks and ran to the end without errors, pausing at
the operator `STOP` as intended. SPK04 stopped with error 1004, "Range exceeded", as
intended. Behavior beyond that (the soft-limit values and their frame, spindle speeds,
run times) has not been checked yet.

**Status:**
- **Documented:** stated in the manufacturer's documentation.
- **Runs in Test Run:** loads with no `ERROR` blocks and runs without errors on the
  programming station (check above). Notes record what was seen; behavior hasn't been
  checked beyond that.
- **To confirm:** not run on the programming station yet. A test program in
  [`spike/`](spike/README.md) exercises it, and the spike README lists the expected
  result and the fallback if it fails.

### Heidenhain TNC 640 (Klartext)

Manual references are to the *TNC 640 Klartext Programming User's Manual, NC SW 34059x-11 (01/2021)*.

| # | Construct | Used for | Source | Test program | Status | Notes |
|---|---|---|---|---|---|---|
| H1 | `L X… Y… R0 FMAX M91` | All motion in machine coordinates | §7.3 | SPK01 | Runs in Test Run | SPK01 and all six warm-ups |
| H2 | `M91` ignores presets, datum shifts and **tool length**; radius comp unchanged, hence `R0` | Safe start regardless of offsets | §7.3: *"The tool length will not be taken into account"* | — | Documented | Test Run can't show this, so it rests on the manual |
| H3 | `QLn = <expr>` local parameters | Stage feed, travel-check values | §9 (QL = local to the program) | SPK01, SPK03 | Runs in Test Run | QL values read 0 after SPK01 ended: they are cleared at program end, as local parameters should be |
| H4 | Feed from a parameter: `FQL1` | Stage feed inside the sweep subprogram | Extrapolated from `FQ10` in the manual's examples | SPK01, SPK03 | Runs in Test Run | `FQL20` in SPK01, `FQL1` in all six warm-ups |
| H5 | `FQ1600` | Fallback for H4 | `FQ10` examples; Q1600–Q1999 user range | SPK01 | Runs in Test Run | Not needed: H4 works |
| H6 | `FN 18: SYSREAD QLn = ID230 NR2/NR3 IDX1–3` | Travel check: reads the soft limits | System data table, group 230 "Traverse range" | SPK01 | Runs in Test Run | Values not inspected. SPK01's M91 moves to targets 10 mm inside the values read ran without a limit error and ended at X −2490, Y −990, Z +1640 |
| H7 | The ID230 limits are in the same frame as `M91` coordinates | The travel check compares them with M91 values | §7.3: "machine datum… defines traverse limits" | SPK01 | To confirm | Consistent with SPK01 (see H6); the direct comparison (QL7 = QL11, QL8 = QL16) wasn't read |
| H8 | `FN 18 … ID230 NR5` (are the limits active?) | A possible refinement of the travel check | System data table | SPK01 | Runs in Test Run | Value not read. Not used by the programs |
| H9 | `TOOL CALL S1000` (no number, no axis) changes the speed only | Stage spindle-speed steps | §4.1 | SPK02 | Runs in Test Run | All six warm-ups, 5–6 speed steps each |
| H10 | `TOOL CALL Z S4000` changes the speed only | Fallback for H9 | §4.1 | SPK02 | To confirm | Not needed: H9 works |
| H11 | `M3`, `M8`, `M5`, `M9` as their own blocks | Spindle and coolant control | §7.1 ("up to four M functions… or in a separate NC block") | SPK02 | Runs in Test Run (`M3`, `M5`, `M9`) | `M8` to confirm: the shipped profiles have coolant off, so the warm-ups tested don't contain it |
| H12 | `CYCL DEF 9.0 DWELL TIME` / `CYCL DEF 9.1 DWELL n` | Spindle top-up dwell | Cycles manual, Cycle 9 | SPK02 | Runs in Test Run | All six warm-ups |
| H13 | `FUNCTION DWELL TIMEn` | Alternative dwell | §10.18 | SPK02 | To confirm | Not used: needs newer NC software |
| H14 | `STOP` behavior | Operator checklist confirmation | §7.1 | SPK02 | Runs in Test Run | Test Run paused at the checklist and continued on START. `M0` would also stop the spindle (§7.2) |
| H15 | Named `LBL "…"` subprogram after `M30`, ended by `LBL 0` | Sweep subprogram | §8.2 | SPK03 | Runs in Test Run | All six warm-ups |
| H16 | `CALL LBL n REP m` gives m + 1 executions | Passes per stage | §8.3 | SPK03 | Runs in Test Run | Runs in all six warm-ups; the m + 1 count wasn't checked separately |
| H17 | `FN 11` / `FN 12` jumps to a named label | Travel-check comparisons | §9.6 examples | SPK03, SPK04 | Runs in Test Run (`FN 12`) | SPK04's `FN 12` jumped to `LBL "TRAVEL_ERR"`. `FN 11` to confirm (SPK03) |
| H18 | `FN 14: ERROR = 1004` shows "Range exceeded" | Travel-check alarm | §9.8 error list | SPK04 | Runs in Test Run | Stopped with error 1004, "Range exceeded" |
| H19 | CRLF `.H` files load cleanly | Output format | — | all | Runs in Test Run | Every file tested |
| H20 | Test Run works without `BLK FORM` | Keeps the output minimal | — | SPK01 | Runs in Test Run | SPK01 and all six warm-ups |
| H21 | The free demo saves programs of up to 100 blocks | Testing in the demo | HEIDENHAIN programming-station information | SPK05 | Documented | SPK05 would show whether a longer program can still be opened or run |
| H22 | Display of long (~120 character) comments | Header formatting | §6.3 (`lineBreak` machine parameter) | SPK02 | To confirm | |
| H23 | `M91` positions are unaffected by the transformation cycles (7 datum shift, 8 mirror, 10 rotation, 11/26 scaling) | Safe start without a reset block (and the header comment saying so) | Reference systems: `M91` programs in the machine coordinate system, while those cycles act in the workpiece and working-plane systems | — | Documented | From the reference-system model rather than a per-cycle statement. Exception: the Global Program Settings option's "additive offset (M-CS)" acts in the machine coordinate system |

### Fanuc 31i

No Fanuc simulator was available (FANUC NCGuide is licensed), so the Fanuc output rests on
documented behavior plus the generator's own round-trip verifier. Constructs that depend
on the control model are behind configuration flags.

| # | Construct | Used for | Source | Status |
|---|---|---|---|---|
| F1 | `G53` is one-shot, **always rapid**, and ignored in `G91` | Absolute positioning only; feed sweeps use `G91` from `G53` anchors | CNC Concepts "Using G53"; Fanuc manual notes | Documented |
| F2 | Always write decimal points (`X760.`) | With parameter 3401#0 = 0, `X760` means 0.760 mm | Fanuc parameter manual (DPI) | Documented |
| F3 | `G04 P30000` (ms, no decimal point) | Dwell | Fanuc G04 references | Documented |
| F4 | `#3000=n(MSG)` raises an alarm with a message | Travel-check alarm | Custom Macro B | Documented (needs Custom Macro) |
| F5 | `IF [<expr>] THEN #3000=1(<message>)` is a valid single-line alarm | Travel check | Custom Macro B IF-THEN form | Documented; not yet run on a control |
| F6 | `PRM[1320]/[axis]`, `PRM[1321]/[axis]` read the stroke limits | Travel check | MMS "Accessing parameter values…"; 30i-B | Documented; **model-dependent**, so behind `runtime_guards` |
| F7 | `PRM[1320]/[n]` returns the stroke limit in mm (not detection units) | The travel check compares it with mm values | 30i-series parameters are real-number type | **Not confirmed.** If a control returns detection units, the check passes without catching a mismatch (it can't raise a false alarm). Turn it off with `runtime_guards = false` if in doubt |
| F8 | `G50`/`G51`, `G68`/`G69`, `G50.1` raise alarms without their options | The machine's opted-in cancel codes | Fanuc option list | Documented |
| F9 | O8000–O8999 can be edit-protected; O9000+ belong to the machine builder | Program-number range | Fanuc program-number areas | Documented |
| F10 | `G53` needs the reference position established after power-on | Every positioning move | Fanuc manual | Documented. True on any machine with absolute encoders, or after homing |
