# Phase 0: Klartext syntax spike

Before building the generator, check the Heidenhain constructs it relies on in the
**TNC 640 Programming Station**. Some come straight from the manual
(NC SW 34059x-11); others are extrapolated from manual examples. These five
small programs exercise each one. Record the results in
[`../DESIGN.md`](../DESIGN.md#controller-syntax-verification).

| File | Tests | Blocks |
|---|---|---|
| `heidenhain/SPK01_LIMITS.H` | `FN 18` soft-limit reads (ID230), `M91` moves, feed from `QL`/`Q` parameters, no `BLK FORM` | 35 |
| `heidenhain/SPK02_SPINDLE.H` | Speed-only `TOOL CALL`, `M3`/`M8`/`M5 M9`, Cycle 9 dwell, `STOP`, `FUNCTION DWELL`, long comment | 29 |
| `heidenhain/SPK03_SUBPGM.H` | `QL` arithmetic, named subprogram after `M30`, `CALL LBL … REP`, `FN 11`/`FN 12` jumps | 33 |
| `heidenhain/SPK04_GUARD.H` | The exact travel-guard pattern: `FN 18` → `FN 12` → `FN 14: ERROR = 1004` | 11 |
| `heidenhain/SPK05_LONG.H` | A 216-block program, to see what the demo's 100-block limit really blocks | 216 |

All files are ASCII with CRLF line endings. `.gitattributes` stops git from converting them.

## Procedure

1. **Install the programming station.** Download it from
   <https://www.heidenhain.com/service/downloads/software>: PC software → TNC 640
   programming station (Windows). Without a USB dongle it runs as a free, unlimited-time
   demo, but edits are limited to 100 NC blocks. Write down the **NC software number**
   shown at start-up or under MOD. Syntax support varies by version.
2. **Copy the five `.H` files onto the station's `TNC:` drive.** The `TNC:` drive is a
   Windows folder set up by the installer; its install notes give the path. You can also
   use PGM MGT to copy from a Windows drive or USB stick if the station shows one.
3. **Check each file in Programming mode.** Open the file and note:
   - any block the control turns into an `ERROR` block;
   - any block it rewrites, for example spacing, a `+` sign added, or `QL` shown differently.
4. **Run each file in Test Run, full sequence.**
   - Turn on working-space monitoring / the traverse range check if the station offers it.
   - After the run, open the additional status display → **Q parameter** tab and add
     `QL1`–`QL20` to the list.
   - Note the **machining time** Test Run reports. We'll use it to calibrate the generator's
     runtime estimate.
5. **Fill in the result column** in `docs/DESIGN.md`, using the expected values below.

## Expected results

### SPK01_LIMITS
- Loads with no `ERROR` blocks, and Test Run runs even though there is no `BLK FORM`.
  If Test Run insists on a blank, note the message.
- `QL1`–`QL6` are sensible software limits, with negative < positive on each axis.
  **Record the actual values.**
- `QL7 = QL11` and `QL8 = QL16`. This proves the ID230 limits and the `M91` positions
  use the same frame (the REF system).
- `QL9 = 0` means the limits are active.
- Blocks 23 (`FQL20`) and 24 (`FQ1600`) are both accepted.

### SPK02_SPINDLE
- `TOOL CALL S1000` is accepted with no tool change and no "tool axis missing" message.
  `QL2 = 1000` and `QL3 = QL1`.
- `QL4 = 2500`, `QL5 = 1`, `QL6 = 4000`, `QL7 = QL1`.
- `STOP` halts the run. Note whether the spindle/coolant status stays on.
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

| Construct | Fallback the generator will use |
|---|---|
| `TOOL CALL S…` | `TOOL CALL Z S…`: the same axis also means a speed-only change (manual §4.1) |
| `FQL1` | `FQ1600`, from the Q1600–Q1999 user range |
| `FUNCTION DWELL TIME…` | Cycle 9 (the planned default anyway) |
| `FN 18 ID230` frame differs from `M91` | Drop the runtime guard, or compare against ID240 positions instead |
| Test Run needs a `BLK FORM` | Emit a `BLK FORM` that covers the travel envelope |
| The demo can't run programs over 100 blocks | Keep the compact rendering, and add a test that the default profile stays ≤ 100 blocks |
