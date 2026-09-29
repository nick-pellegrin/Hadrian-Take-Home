# Design notes

This file records the design decisions behind the warm-up generator and the evidence for
them. Architecture decisions will be added as each phase lands. This first section
records which controller constructs the generated programs depend on, and how each one
was verified.

## Controller syntax verification

**Status meanings:**
- **Documented**: taken directly from the manufacturer's manual; no run-time check possible.
- **Pending**: a Phase 0 spike program exists and still needs running (see [`spike/README.md`](spike/README.md)).
- **Verified**: the spike ran and behaved as expected.
- **Failed → fallback**: the spike failed; the generator uses the fallback listed.

Programming station NC software version: `__________` *(fill in)*

### Heidenhain TNC 640 (Klartext)

Manual references are to the *TNC 640 Klartext Programming User's Manual, NC SW 34059x-11 (01/2021)*.

| # | Construct | Used for | Source | Spike | Status | Notes / observed |
|---|---|---|---|---|---|---|
| H1 | `L X… Y… R0 FMAX M91` | All motion in machine coordinates | §7.3 | SPK01 b21–22 | Pending | |
| H2 | `M91` ignores presets, datum shifts and **tool length**; radius comp unchanged, hence `R0` | Safe start regardless of offsets | §7.3: *"The tool length will not be taken into account"* | — | Documented | Test Run can't show this; it relies on the manual |
| H3 | `QLn = <expr>` local parameters | Stage feed, guard values | §9 (QL = local to program) | SPK01, SPK03 | Pending | |
| H4 | Feed from a parameter: `FQL20` | Stage feed inside the sweep subprogram | Extrapolated from `FQ10` in manual examples | SPK01 b23, SPK03 b27 | Pending | |
| H5 | Fallback `FQ1600` | Same as H4 | `FQ10` examples; Q1600–1999 user range | SPK01 b24 | Pending | |
| H6 | `FN 18: SYSREAD QLn = ID230 NR2/NR3 IDX1–3` | Runtime soft-limit guard | System data table, group 230 "Traverse range" | SPK01 | Pending | Record the station's limits here |
| H7 | ID230 limits are in the same frame as `M91` coordinates | The guard compares config values (in M91 coordinates) against ID230 | §7.3: "machine datum… defines traverse limits" | SPK01 (`QL7 = QL11`, `QL8 = QL16`) | Pending | |
| H8 | `FN 18 … ID230 NR5` (are the limits active?) | Skip the guard if limits are disabled | System data table | SPK01 b32 | Pending | |
| H9 | `TOOL CALL S1000` (no number, no axis) changes speed only | Stage spindle-speed steps | §4.1 | SPK02 test A | Pending | |
| H10 | `TOOL CALL Z S4000` changes speed only | Fallback for H9 | §4.1 | SPK02 test C | Pending | |
| H11 | `M3`, `M8`, `M5 M9` as their own blocks | Spindle and coolant control | §7.1 ("up to four M functions… or in a separate NC block") | SPK02 | Pending | |
| H12 | `CYCL DEF 9.0 DWELL TIME` / `CYCL DEF 9.1 DWELL n` | Spindle top-up dwell | Cycles manual, Cycle 9 | SPK02 | Pending | |
| H13 | `FUNCTION DWELL TIMEn` | Alternative dwell | §10.18 | SPK02 test E | Pending | Depends on the NC SW version |
| H14 | `STOP` behavior | Operator checklist confirmation | §7.1 | SPK02 test D | Pending | `M0` also stops the spindle (§7.2) |
| H15 | Named `LBL "…"` subprogram after `M30`, ended by `LBL 0` | Sweep subprogram | §8.2 | SPK03 test A | Pending | |
| H16 | `CALL LBL n REP m` gives m + 1 executions | Passes per stage | §8.3 | SPK03 test B | Pending | |
| H17 | `FN 11` / `FN 12` with a literal first operand | Guard comparisons | §9.6 examples | SPK03 test C | Pending | |
| H18 | `FN 14: ERROR = 1004` shows "Range exceeded" | Guard alarm | §9.8 error list | SPK04 | Pending | |
| H19 | CRLF `.H` files load cleanly | Output format | — | all | Pending | |
| H20 | Test Run works without `BLK FORM` | Keeps the output minimal | — | SPK01 | Pending | |
| H21 | What the demo's 100-block limit blocks (edit, open or run) | Whether compact output is required | Third-party description | SPK05 | Pending | |
| H22 | Display of long (~120 character) comments | Header formatting | §6.3 (`lineBreak` machine parameter) | SPK02 b2 | Pending | |

### Fanuc 31i

No Fanuc simulator is available (FANUC NCGuide is licensed). The Fanuc output therefore
relies on documented behavior plus the generator's own round-trip verifier. Constructs
that depend on the control model are behind configuration flags.

| # | Construct | Used for | Source | Status |
|---|---|---|---|---|
| F1 | `G53` is one-shot, **always rapid**, and ignored in `G91` | Absolute positioning only; feed sweeps use `G91` from `G53` anchors | CNC Concepts "Using G53"; Fanuc manual notes | Documented |
| F2 | `G91 G28 Z0.` retracts via the current position | Safe first move | Standard Fanuc practice | Documented |
| F3 | Always emit decimal points (`X760.`) | With parameter 3401#0 = 0, `X760` means 0.760 mm | Fanuc parameter manual (DPI) | Documented |
| F4 | `G04 X30.` (seconds) / `G04 P30000` (ms, no decimal point) | Dwell | Fanuc G04 references | Documented |
| F5 | `#3006=1(MSG)` stops with a message; `#3000=n(MSG)` raises an alarm | Operator confirmation; guard alarm | Custom Macro B | Documented (needs Macro B) |
| F6 | `PRM[1320]/[axis]`, `PRM[1321]/[axis]` read the stroke limits | Runtime soft-limit guard | MMS "Accessing parameter values…"; 30i-B | Documented; **model-dependent** (flag) |
| F7 | `G50`/`G51`, `G68`/`G69`, `G50.1` raise alarms without their options | Configurable modal-cancel line | Fanuc option list | Documented |
| F8 | O8000–O8999 can be edit-protected; O9000+ belong to the MTB | Program-number range | Fanuc program number areas | Documented |
