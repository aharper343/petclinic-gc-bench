# TODO

- [ ] **Check k6 exit status** (`run_experiment.py:354`, `:398`, `:409`).
  `run_k6` returns k6's result, but no caller checks it. If the measured run
  fails, the row is written with empty HTTP percentiles,
  `k6_dropped_iterations=0` and an empty `error` field. If the warmup fails,
  measurement runs on a JVM that never warmed up. If the priming load fails,
  the archive is built from startup only. Fail the run, or record the error
  in the row, when k6 exits non-zero or `k6_summary.json` is missing.

- [ ] **Fill the heap-pressure cache before the server starts**
  (`bench/src/main/java/org/springframework/samples/petclinic/bench/HeapPressureCache.java:34`).
  The cache is filled in a `CommandLineRunner`, which Spring Boot runs after
  Tomcat is already serving requests. Readiness and `verify_run` checks can
  pass while the cache is only partly filled. Request threads also read the
  plain `ArrayList` while it is still growing, which can throw
  `IndexOutOfBoundsException` and return a 500. Move the fill into
  `@PostConstruct` or `InitializingBean`, and have `verify_run`
  (`run_experiment.py:248`) compare `/api/bench/cache/size` with
  `HEAP_CACHE_SIZE`.

- [ ] **Narrow the measurement window to k6's own run** (`run_experiment.py:408`).
  `window_start` and `window_end` are taken around the whole
  `docker run grafana/k6` call, so the window includes container start-up and
  teardown. GC pauses from that idle time count toward the
  "measured window only" `gc_pause_*` stats, and `cpu_pct_of_pinned` comes out
  slightly low. Take the bounds from k6's own start and end times, or from the
  timestamps in `k6_raw.csv.gz`.
