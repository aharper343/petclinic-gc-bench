# petclinic-gc-bench

A small, independent benchmark of a few JDK 25 JVM settings on one Spring
Boot application in one container configuration: the JDK 25 default
garbage collector versus G1 on one CPU with a small, pressured heap, crossed
with AppCDS and compact object headers.

## Scope: read this first

**This repository is a record of specific tests. It isn't a tool, a
recommendation engine, or a replacement for anything.**

The questions it tests were inspired by
[jaz](https://learn.microsoft.com/en-us/java/jaz/overview), Microsoft's
launcher for Java applications, which picks JVM settings such as heap size
and collector options for the environment it runs in. Looking at the kind of
settings a launcher like that applies prompted the questions here: which
collector to use in a small container, how big the heap should be, and
whether AppCDS and compact headers are worth turning on.

That is the whole relationship:

- **Not affiliated with or endorsed by Microsoft.** jaz and Microsoft are
  named only to explain where the questions came from.
- **Not an evaluation of jaz.** jaz was never run in any measured test. JVM
  flags were passed directly to Eclipse Temurin JDK 25. jaz chooses its
  settings at launch for each environment and may use features of the
  Microsoft Build of OpenJDK that weren't tested here, so nothing in these
  results says how jaz itself performs.
- **Not a replacement for jaz** or any other launcher or tuning tool. There
  is no code here that chooses flags for an application.
- **Results apply only to the conditions tested:** one application, one CPU,
  a 1024m container, light load, one host. See [Limits](#limits).

## Results (batch `gc_heap_v2`, 2026-09-25 to 2026-09-28)

130 runs: 13 cells × 10 blocks, each run a 30-minute measured window after a
5-minute warmup at 40 requests per second (72,001 requests per run). All runs
completed with no errors, failed requests or dropped requests. Data:
[`results/gc_heap_v2.csv`](results/gc_heap_v2.csv).

Medians of 10 runs for the main cells:

| Cell | Startup | HTTP p99 | HTTP p999 | App CPU | G1 concurrent cycles / 30 min |
|---|---|---|---|---|---|
| `default_small` (Serial, 128m) | 19.9 s | 2.9 ms | 4.5 ms | 5.0% | – |
| `g1_small` (G1, 128m) | 20.3 s | 3.1 ms | 14.2 ms | 6.6% | ~120 |
| `default_small_cds` (Serial, 128m, AppCDS) | 16.9 s | 2.9 ms | 4.5 ms | 5.0% | – |
| `default_untuned` (no flags) | 20.0 s | 2.6 ms | 4.6 ms | 5.0% | – |

The noise control (`default_small` run twice under two names) differed by
0.03 ms in p999 and 0.13 s in startup, so differences of a few tenths of a
millisecond in this batch are distinguishable from noise.

What these tests showed:

1. **On one CPU at 128m, Serial (the JDK 25 default there) had a third of
   G1's p999 and used less CPU.** G1's extra tail latency came from its
   concurrent mark cycles: about one every 15 seconds, each ending in a
   Remark pause of about 19 ms median.
2. **The G1 result sits on a threshold.** G1 settled at 56–57 MB after each
   young collection, right at its default initiating threshold (45% of 128m,
   about 57.6 MB). In `g1_small_cds_coh`, 8 of 10 runs stayed at or just
   below it, ran 0–2 concurrent mark cycles in the whole window (5 ran none),
   and matched Serial (p999 4.5–5.0 ms); the other 2 crossed it, ran about 95
   cycles each, and looked like the other G1 cells. So these tests show G1
   behaving badly at about 45–50% heap occupancy on one CPU, not G1 being
   slower in general. Other heap sizes for G1 weren't tested.
3. **AppCDS cut startup by 2.7–3.2 s (13–16%) in every pair**, with no
   measurable change in steady-state latency when the heap was pinned.
4. **With no flags at all plus AppCDS, Serial took one ~190 ms Full GC about
   2 minutes after start** (triggered by code cache growth). It resized the
   young generation, and the application was healthy afterwards.
5. **With a pinned heap, compact object headers meant 15–18% fewer
   collections, an 8–9% smaller heap after GC, and a p99 about 0.3 ms
   lower.** p999 didn't change meaningfully, and container memory showed no
   consistent change; the heap was about a third of the ~400 MB container
   footprint.
6. **The untuned default heap did as well as a heap pinned at 128m** once
   warm. It grew to about 165 MB.

## Limits

- **One application, lightly loaded.** The app used about 5% of one core.
  Throughput and behaviour near saturation weren't tested.
- **One CPU only.** On two or more cores, G1's concurrent work no longer
  competes with the application for the same core; none of that was tested.
- **One heap size for the collector comparison**, which turned out to sit on
  G1's marking threshold.
- **A synthetic part of the live set.** About 20 MB of it is a static cache
  added to create heap pressure; real live data changes more.
- **A laptop host** (Intel i5-6300U, SMT off, turbo on). Every cell saw rare
  ~60 ms stalls during which the JVM was off the CPU; they set the maximum
  latency in many runs but are too rare to move p999. Absolute numbers won't
  transfer to other machines.
- **Startup is Spring's own "Started in" figure**, which excludes JVM boot
  before Spring starts.
- **AppCDS only, not the newer AOT cache** (`-XX:AOTCacheOutput`) that JDK 25
  also offers.

## The matrix

`flags_gc_heap.json`, 13 cells, all on 1 CPU in a 1024m container. Three
base cells, each crossed with AppCDS (on/off) and compact object headers
(on/off), plus one noise control:

| Base cell | GC | Heap |
|---|---|---|
| `default_untuned` | JDK 25 default (Serial) | JVM default (16m initial, 256m max) |
| `default_small` | Serial | pinned 128m |
| `g1_small` | G1 | pinned 128m |

Cell names add `_cds` for AppCDS and `_coh` for compact headers, e.g.
`g1_small_cds_coh`. `default_small_aa` duplicates `default_small` under
another name to measure run-to-run noise; it isn't crossed.

- On one CPU the JDK 25 default is Serial. The default cells pass no GC flag,
  and the harness fails any run where the JVM selected a different collector.
- G1 cells pass only `-XX:+UseG1GC` and the heap size. No tuning flags.
- Every cell runs with `HEAP_CACHE_SIZE=120000`, about 20 MB of resident
  objects, for a live set of about 64 MB: half the 128m heap.
- Compact headers are `-XX:+UseCompactObjectHeaders`. AppCDS is set with
  `"appcds": true` and handled by the harness (below).
- Native memory tracking is off.

## Running it

Requires Docker, Python 3, cgroup v2 with Docker's systemd cgroup driver, and
the `performance` frequency governor on the app and load-generator CPUs.

`app/` is a git submodule, so clone with `--recurse-submodules`:

```
git clone --recurse-submodules https://github.com/aharper343/petclinic-gc-bench.git
cd petclinic-gc-bench
```

In a clone made without it, run `git submodule update --init` to fetch
`app/`. Then build the image from the repository root and run the batch:

```
docker build -f bench/Dockerfile -t petclinic-bench:jdk25-v2 .
python3 run_experiment.py --flags flags_gc_heap.json --out results/my_batch.csv
```

Defaults: 10 blocks (every cell once per block, shuffled), 40 req/s,
5-minute warmup, 30-minute measured window. That's 130 runs of roughly
36 minutes each, about 3.3 days, plus about 20 minutes of AppCDS priming.
Use `--only <cells> --repeats 1 --warmup 15s --measure 30s --out <scratch
path>` for a quick check first.

## What the harness guarantees

- **The run is what the flags file says.** Before measuring it checks the
  collector the JVM chose, that owner 5010 exists, and that the heap-pressure
  cache is populated.
- **AppCDS archives are built fresh for each batch.** Before any measured
  run, each AppCDS cell gets a priming run with its own flags: startup, 2
  minutes of load (`--prime-load`), then a graceful stop that writes the
  archive. Measured runs map it read-only with `-Xshare:on`, so a missing or
  rejected archive stops the JVM instead of quietly running without CDS.
- **Nothing is overwritten.** Each batch gets its own folder next to the CSV
  (`results/<csv stem>/`), holding a copy of the flags file, the invocation,
  the image ID, and one folder per run. The harness refuses to reuse a batch
  name or a run folder. Raw per-run files and AppCDS archives are gitignored.
- **Measurements don't touch the app's CPU.** Memory and CPU are read from
  the container's cgroup on the host, not through `docker exec`.
  `mem_anon_max_bytes` excludes page cache; `mem_max_bytes` includes it.
- **GC stats cover the measured window only**, and the window timestamps are
  saved on every row. GC logging is asynchronous and identical for all cells.
- **Tail latency isn't hidden.** k6 keeps 50 idle virtual users ready so a
  stall of about a second doesn't drop requests. Dropped iterations, max
  latency, and every request's raw timing (`k6_raw.csv.gz`) are saved.
- **Host drift is recorded.** Governor, turbo and SMT state are on every
  row, and a run fails if they change mid-run.

## Host notes

The test host is an i5-6300U: 2 physical cores, 4 logical CPUs. With SMT on,
CPU 0 (app) shares a physical core with CPU 2 and CPU 1 (load generator)
with CPU 3, so anything the scheduler puts on 2 or 3 competes with the
benchmark. Disabling SMT (`echo off | sudo tee
/sys/devices/system/cpu/smt/control`) gives each a whole physical core; host
processes then share those two cores, so close other applications during a
batch. The harness warns when SMT is on. The `gc_heap_v2` batch ran with SMT
off.

## The application

`app/` is a git submodule of
[spring-petclinic-rest](https://github.com/spring-petclinic/spring-petclinic-rest)
(Apache License 2.0, see `app/LICENSE.txt`), pinned to upstream commit
`4cd8e1b` (2026-09-01, version 4.0.2) and left unmodified. The benchmark's
changes live in `bench/`, which the image build copies over the upstream
source:

- `bench/Dockerfile`: multi-stage build on Eclipse Temurin JDK 25.
- `bench/src/main/resources/db/h2/data.sql`: replaces upstream's with a
  generated 5,010-owner dataset.
- `bench/src/main/java/.../petclinic/bench`: an added heap-pressure cache,
  sized by `HEAP_CACHE_SIZE` and disabled by default.

## License

Licensed under the [Apache License, Version 2.0](LICENSE). The application
in `app/` is spring-petclinic-rest, included as a submodule under its own
Apache 2.0 license (`app/LICENSE.txt`); the files in `bench/` modify and
extend it.
