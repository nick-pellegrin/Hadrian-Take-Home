# Example run

Output of each command, captured from real runs by `tests/test_run_log.py` (which
fails if this file is out of date). Programs land in `<out>/<controller>/`; the
`examples/heidenhain/` and `examples/fanuc/` programs were written by the
`generate --all` command below.

Version.

```console
$ uv run cnc-warmup --version
cnc-warmup 0.1.0
```

The configured machines and warm-up profiles.

```console
$ uv run cnc-warmup list
Machines (config/machines.toml), in machine coordinates:
ID  Controller  X         Y        Z        Spindle max  Max feed      Fanuc
M1  heidenhain  -762..0   -508..0  -500..0  12000 rpm    20000 mm/min  O8001
M2  heidenhain  -1016..0  -660..0  -500..0  12000 rpm    20000 mm/min  O8002
M3  heidenhain  -1270..0  -508..0  -500..0  12000 rpm    20000 mm/min  O8003

Profiles (config/profiles.toml):
Name      Duration  Stages  RPM         Feed mm/min  Ramp       Coolant
daily     20 min    5       1000-10000  2500-12000   linear     off
extended  30 min    6       500-10000   2000-12000   geometric  off
```

Preview a warm-up without writing anything.

```console
$ uv run cnc-warmup show-plan --machine M1
Machine M1 (3-axis VMC, 762 x 508 x 500 mm travel), profile daily
Sweep envelope X -761..-1  Y -507..-1  Z -499..-1  (1 mm inside travel)
One sweep pass: 6267 mm (perimeter, XY diagonals, Z stroke at the XY center)

Stage  RPM    Feed   Passes  Pass time  Dwell  Stage time
1      1000   2500   1       2:30       89 s   3:59
2      3250   4880   3       1:17       8 s    3:59
3      5500   7250   4       0:52       32 s   3:59
4      7750   9630   6       0:39       5 s    3:59
5      10000  12000  7       0:31       20 s   3:59

Estimated run time 19:57, plus rapid positioning.
```

Generate every example program: 3 machines x 2 profiles x 2 controllers. Each program is read back by a simulated control and checked against its plan before it is written.

```console
$ uv run cnc-warmup generate --all --controller heidenhain fanuc -o examples
Machine  Profile   Controller  File                                      Size       Run time    Check
M1       daily     heidenhain  examples/heidenhain/WARMUP_M1_DAILY.H     106 lines  est. 19:57  verified
M1       daily     fanuc       examples/fanuc/O8001_M1_DAILY.nc          305 lines  est. 19:57  verified
M1       extended  heidenhain  examples/heidenhain/WARMUP_M1_EXTENDED.H  116 lines  est. 29:57  verified
M1       extended  fanuc       examples/fanuc/O8001_M1_EXTENDED.nc       356 lines  est. 29:57  verified
M2       daily     heidenhain  examples/heidenhain/WARMUP_M2_DAILY.H     106 lines  est. 19:58  verified
M2       daily     fanuc       examples/fanuc/O8002_M2_DAILY.nc          250 lines  est. 19:58  verified
M2       extended  heidenhain  examples/heidenhain/WARMUP_M2_EXTENDED.H  114 lines  est. 29:58  verified
M2       extended  fanuc       examples/fanuc/O8002_M2_EXTENDED.nc       289 lines  est. 29:58  verified
M3       daily     heidenhain  examples/heidenhain/WARMUP_M3_DAILY.H     106 lines  est. 19:58  verified
M3       daily     fanuc       examples/fanuc/O8003_M3_DAILY.nc          239 lines  est. 19:58  verified
M3       extended  heidenhain  examples/heidenhain/WARMUP_M3_EXTENDED.H  114 lines  est. 29:56  verified
M3       extended  fanuc       examples/fanuc/O8003_M3_EXTENDED.nc       267 lines  est. 29:56  verified

Wrote 12 programs to examples/.
```

Per-run overrides, without editing any file: flood coolant and a final rapid pass.

```console
$ uv run cnc-warmup generate --machine M2 --set coolant=flood --set final_rapid_pass=true -o out
Machine  Profile  Controller  File                              Size       Run time    Check
M2       daily    heidenhain  out/heidenhain/WARMUP_M2_DAILY.H  120 lines  est. 20:22  verified

Wrote 1 program to out/.
```

A feed too slow for the largest machine: the stage overruns, so there is a warning.

```console
$ uv run cnc-warmup show-plan --machine M3 --set feed_start=1000
Machine M3 (3-axis VMC, 1270 x 508 x 500 mm travel), profile daily
Sweep envelope X -1269..-1  Y -507..-1  Z -499..-1  (1 mm inside travel)
One sweep pass: 8640 mm (perimeter, XY diagonals, Z stroke at the XY center)

Stage  RPM    Feed   Passes  Pass time  Dwell  Stage time
1      1000   1000   1       8:38       0 s    8:38
2      3250   3750   1       2:18       101 s  3:59
3      5500   6500   3       1:20       0 s    3:59
4      7750   9250   4       0:56       15 s   3:59
5      10000  12000  5       0:43       24 s   4:00

Estimated run time 24:36, plus rapid positioning.
warning: profiles.daily.feed_start: stage 1 runs 8:38, over its 4:00 share of the duration: one sweep pass at 1000 mm/min takes that long on machine M3. Raise feed_start or duration_min
```

A request the machine cannot run is refused, and nothing is written.

```console
$ uv run cnc-warmup generate --machine M1 --set feed_end=30000
Nothing written.
error: profiles.daily.feed_end: 30000 mm/min exceeds the max_feed of machine M1 (20000 mm/min)
[exit status 1]
```

Check the whole configuration, e.g. in CI.

```console
$ uv run cnc-warmup validate
Configuration OK: 3 machines, 2 profiles. All 12 programs plan, render and verify.
```
