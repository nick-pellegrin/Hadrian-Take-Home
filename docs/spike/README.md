# Klartext syntax checks (TNC 640 programming station)

The generated Heidenhain programs rely on the constructs listed in
[`../DESIGN.md`](../DESIGN.md#controller-syntax-verification). Some come straight from the
manual (NC SW 34059x-11); others are extrapolated from its examples. These five small
programs exercise each one on a **TNC 640 programming station**: the control's own
software running on a Windows PC.

| File | Tests | Blocks |
|---|---|---|
| `heidenhain/SPK01_LIMITS.H` | `FN 18` soft-limit reads (ID230), `M91` moves, feed from `QL`/`Q` parameters, no `BLK FORM` | 35 |
| `heidenhain/SPK02_SPINDLE.H` | Speed-only `TOOL CALL`, `M3`/`M8`/`M5 M9`, Cycle 9 dwell, `STOP`, `FUNCTION DWELL`, long comment | 29 |
| `heidenhain/SPK03_SUBPGM.H` | `QL` arithmetic, named subprogram after `M30`, `CALL LBL … REP`, `FN 11`/`FN 12` jumps | 33 |
| `heidenhain/SPK04_GUARD.H` | The exact travel-check pattern: `FN 18` → `FN 12` → `FN 14: ERROR = 1004` | 11 |
| `heidenhain/SPK05_LONG.H` | A 216-block program, to see what the demo's 100-block limit blocks | 216 |

All files are ASCII with CRLF line endings; `.gitattributes` stops git from converting them.

## Procedure

1. **Install the programming station.** Download the TNC 640 programming station from
   <https://www.heidenhain.com/products/cnc-controls/programming-stations> (Windows).
   Without a USB dongle it runs as a free demo with no time limit and no registration,
   but it only saves programs of up to 100 NC blocks. Note the **NC software number**
   shown at start-up or under MOD: syntax support varies by version.
2. **Copy the five `.H` files onto the station's `TNC:` drive.** The `TNC:` drive is a
   Windows folder created by the installer (its install notes give the path). You can
   also copy them in with PGM MGT from a Windows drive or USB stick.
3. **Open each file in Programming mode** and note:
   - any block the control turns into an `ERROR` block;
   - any block it rewrites, for example spacing, an added `+` sign, or `QL` shown
     differently.
4. **Run each file in Test Run, full sequence.**
   - Turn on the traverse range check / working-space monitoring if the station offers it.
   - After the run, open the additional status display → **Q parameter** tab and add
     `QL1`–`QL20` to the list.
   - Note the **machining time** Test Run reports.
5. **Record the results** in [`../DESIGN.md`](../DESIGN.md#controller-syntax-verification):
   set each construct's status to *Runs in Test Run*, with what you saw, or note what
   happened and use the fallback below. The first check (1 October 2026, NC software
   340595 18 SP4) covered SPK01, SPK04 and the six warm-up programs; SPK02, SPK03 and
   SPK05 are still to run.

## Then try the generated programs

With the travel check turned off, every generated Heidenhain program fits the demo's
100-block limit (87–97 blocks); the check itself is covered by SPK01 and SPK04 above.

```sh
uv run cnc-warmup generate --all --controller heidenhain --set runtime_guards=false -o out/demo
```

Copy `out/demo/heidenhain/*.H` to the `TNC:` drive, open each one in Programming mode
(no `ERROR` blocks expected), and run it in Test Run. The machining time should be close
to the `EST. RUN TIME` in the program header, which excludes rapid positioning. With a
licensed station (or on a real control), the full programs in `examples/heidenhain/` run
as they are.

## Expected results

### SPK01_LIMITS
- Loads with no `ERROR` blocks, and Test Run runs even though there is no `BLK FORM`.
  If Test Run insists on a workpiece blank, note the message.
- `QL1`–`QL6` are sensible software limits, with negative < positive on each axis.
  Record the actual values.
- `QL7 = QL11` and `QL8 = QL16`. This proves the ID230 limits and the `M91` positions
  use the same frame (the REF system).
- `QL9 = 0` means the limits are active.
- Blocks 23 (`FQL20`) and 24 (`FQ1600`) are both accepted.

### SPK02_SPINDLE
- `TOOL CALL S1000` is accepted with no tool change and no "tool axis missing" message.
  `QL2 = 1000` and `QL3 = QL1`.
- `QL4 = 2500`, `QL5 = 1`, `QL6 = 4000`, `QL7 = QL1`.
- `STOP` halts the run. Note whether the spindle and coolant status stay on.
- `FUNCTION DWELL TIME2`: note whether it is accepted or becomes an `ERROR` block. This
  depends on the NC software version.
- Block 2 (a 120-character comment): note whether it wraps or shows `>>`.

### SPK03_SUBPGM
- Finishes with **no error message**, and `QL2 = 5`, `QL3 = 3`, `QL4 = 1`.
- If `QL3 = 2`, then `REP 2` means 2 executions in total, not 3.

### SPK04_GUARD
- Stops with **error 1004, "Range exceeded"**. `QL9` is never set.
- Note the exact message text, and whether the comment above the `FN 14` block is
  visible to the operator.

### SPK05_LONG
- Record which of these the demo allows: open, scroll, Test Run.
- If Test Run completes, `QL1 = 70`. If it stops at the block limit, `QL1` is about 32.

## If something fails

| Construct | Fallback |
|---|---|
| `TOOL CALL S…` | `TOOL CALL Z S…`: naming the active tool axis also means a speed-only change (manual §4.1) |
| `FQL1` | `FQ1600`, from the Q1600–Q1999 user range (`FEED_PARAM` in the Heidenhain post) |
| `FUNCTION DWELL TIME…` | Cycle 9, which the programs already use |
| The `FN 18 ID230` frame differs from `M91` | Turn the travel check off (`runtime_guards = false`), or compare against ID240 positions instead |
| Test Run needs a `BLK FORM` | Add a `BLK FORM` covering the travel envelope |
| The demo can't run programs over 100 blocks | Test in the demo with `runtime_guards = false`, as above |
