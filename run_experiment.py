#!/usr/bin/env python3
"""Driver for the JDK 25 GC benchmark against spring-petclinic-rest.

Repeats are run as randomized complete blocks: each block runs every combo
once, in a freshly shuffled order, so slow drift (thermals, background load)
spreads evenly across combos instead of piling onto whichever ran last.

For each run:
  1. start the app container with that combo's flags and env, pinned/limited
     per the flags file
  2. wait for /petclinic/api/vets to return 200, record startup time
  3. verify the run is what the flags file says it is: the GC the JVM
     actually selected, the seeded dataset, the heap-pressure cache
  4. run a throwaway k6 warmup (discarded)
  5. run the measured k6 load, sampling the container's cgroup from the host
  6. tear down, parse the GC log over the measured window, append one row

Combos with "appcds": true get a dynamic AppCDS archive built once per batch
by a priming run (startup plus a short burst of load, then a graceful stop),
before any measured run. Measured runs map it read-only with -Xshare:on, so a
missing or rejected archive stops the JVM instead of silently running
without CDS.
"""
import argparse
import csv
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BENCH_DIR = Path(__file__).parent
IMAGE = "petclinic-bench:jdk25-v2"
# Observability, not tuning: applied identically to every combo, never a
# factor. Asynchronous logging keeps log writes out of GC pauses, which
# otherwise cost more for combos that collect more often.
INSTRUMENTATION_FLAGS = [
    "-Xlog:async",
    "-Xlog:gc*,gc+heap=debug:file=/logs/gc.log:time,uptime,level,tags",
    "-Xlog:codecache=info:file=/logs/codecache.log:time,uptime",
]
# What the JVM prints at GC init (gc,init "Using <name>") for each value of a
# combo's expected_gc.
GC_INIT_NAMES = {"serial": "Serial", "g1": "G1", "parallel": "Parallel"}
CDS_MOUNT = "/app/cds"
CDS_ARCHIVE = f"{CDS_MOUNT}/app.jsa"

ROW_FIELDS = [
    "batch", "block", "combo", "run_idx", "image_id", "gc_selected", "appcds",
    "window_start_epoch", "window_end_epoch",
    "startup_log_s", "startup_wall_s", "measured_wall_s",
    "cpu_delta_usec", "cpu_pct_of_pinned", "mem_max_bytes", "mem_anon_max_bytes",
    "gc_pause_count", "gc_pause_count_all", "gc_pause_max_ms", "gc_pause_max_all_ms",
    "gc_full_count", "gc_full_count_all", "gc_conc_cycle_count",
    "gc_pause_p50_ms", "gc_pause_p99_ms", "gc_pause_p999_ms",
    "heap_after_gc_median_mb", "heap_after_full_gc_min_mb",
    "http_p50_ms", "http_p90_ms", "http_p99_ms", "http_p999_ms", "http_max_ms",
    "http_reqs", "http_req_failed_rate", "k6_dropped_iterations", "k6_vus_max",
    "governor", "no_turbo", "smt_active", "error",
]


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def percentile(sorted_data, p):
    if not sorted_data:
        return None
    k = (len(sorted_data) - 1) * (p / 100)
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_data[int(k)]
    return sorted_data[f] + (sorted_data[c] - sorted_data[f]) * (k - f)


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


# --- host environment -------------------------------------------------------

def host_env(cpus):
    """Frequency governor for the given CPUs (joined if they differ), turbo
    and SMT state. Recorded on every row so drift is visible in the data."""
    govs = {read_text(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_governor") for c in cpus}
    return {
        "governor": "/".join(sorted(g or "?" for g in govs)),
        "no_turbo": read_text("/sys/devices/system/cpu/intel_pstate/no_turbo"),
        "smt_active": read_text("/sys/devices/system/cpu/smt/active"),
    }


def image_id(image):
    r = sh(["docker", "image", "inspect", image, "--format", "{{.Id}}"])
    return r.stdout.strip() if r.returncode == 0 else None


# --- container --------------------------------------------------------------

def container_status(name):
    r = sh(["docker", "inspect", name, "--format", "{{.State.Status}}"])
    return r.stdout.strip() if r.returncode == 0 else None


def http_get(url, timeout=5):
    """(status, body) for url; (None, None) if unreachable."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return None, None


def wait_for_ready(url, timeout_s, container_name):
    """Polls url for 200 while verifying THAT container is still running, so a
    stale process bound to the same host port can't pass the health check for
    a container that crashed."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = container_status(container_name)
        if status not in ("running", None):
            raise RuntimeError(f"container {container_name} exited early (status={status})")
        if http_get(url, timeout=2)[0] == 200:
            return True
        time.sleep(0.5)
    return False


class Cgroup:
    """Reads a container's cgroup v2 files from the host. Reading through
    `docker exec` instead would start a process inside the container's cpuset,
    on the app's only CPU, every sample."""

    def __init__(self, container_id):
        self.path = Path(f"/sys/fs/cgroup/system.slice/docker-{container_id}.scope")
        if not self.path.is_dir():
            raise RuntimeError(f"cgroup not found at {self.path} (needs cgroup v2 + systemd driver)")

    def cpu_usage_usec(self):
        for line in (read_text(self.path / "cpu.stat") or "").splitlines():
            if line.startswith("usage_usec"):
                return int(line.split()[1])
        return None

    def mem_current(self):
        v = read_text(self.path / "memory.current")
        return int(v) if v else None

    def mem_anon(self):
        """Anonymous memory: closest cgroup figure to the JVM's RSS, without
        the page cache (log files, the mapped jar) that memory.current counts."""
        for line in (read_text(self.path / "memory.stat") or "").splitlines():
            if line.startswith("anon "):
                return int(line.split()[1])
        return None


class MemSampler(threading.Thread):
    def __init__(self, cgroup, interval=1.0):
        super().__init__(daemon=True)
        self.cgroup = cgroup
        self.interval = interval
        self.current, self.anon = [], []
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            for samples, read in ((self.current, self.cgroup.mem_current),
                                  (self.anon, self.cgroup.mem_anon)):
                v = read()
                if v is not None:
                    samples.append(v)
            self._halt.wait(self.interval)

    def stop(self):
        self._halt.set()


def cds_dir_for(combo, batch_dir):
    return batch_dir / "cds" / combo["name"]


def start_container_cmd(name, combo, run_dir, cfg, image, batch_dir, priming=False):
    """priming=True dumps a dynamic AppCDS archive at JVM exit; otherwise an
    appcds combo maps the batch's archive read-only and refuses to start
    without it."""
    cds_flags, cds_mount = [], []
    if combo.get("appcds"):
        cds_dir = cds_dir_for(combo, batch_dir)
        if priming:
            cds_flags = [f"-XX:ArchiveClassesAtExit={CDS_ARCHIVE}"]
            cds_mount = ["-v", f"{cds_dir}:{CDS_MOUNT}"]
        else:
            cds_flags = [f"-XX:SharedArchiveFile={CDS_ARCHIVE}", "-Xshare:on"]
            cds_mount = ["-v", f"{cds_dir}:{CDS_MOUNT}:ro"]
    flags = " ".join(combo["flags"] + cds_flags + INSTRUMENTATION_FLAGS)
    cmd = [
        "docker", "run", "-d", "--name", name, "--network", "host",
        f"--memory={cfg['memory']}", f"--memory-swap={cfg['memswap']}",
        f"--cpuset-cpus={cfg['cpuset_app']}",
        "-v", f"{run_dir}:/logs", *cds_mount,
    ]
    env = {**cfg.get("env", {}), **combo.get("env", {})}
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += ["-e", f"JDK_JAVA_OPTIONS={flags}", image]
    return cmd


# --- verification -----------------------------------------------------------

def gc_selected(gc_log_path):
    """The collector the JVM actually chose, from the gc,init "Using X" line."""
    if not gc_log_path.exists():
        return None
    for line in gc_log_path.read_text(errors="ignore").splitlines()[:200]:
        m = re.search(r"\]\s+Using (\w+)\s*$", line)
        if m:
            return m.group(1)
    return None


def verify_run(combo, cfg, base_url, gc_log_path):
    """Fail the run unless it is what the flags file claims: the collector the
    JVM actually selected, the seeded dataset, and the heap-pressure cache."""
    want = combo.get("expected_gc")
    got = gc_selected(gc_log_path)
    if want and got != GC_INIT_NAMES[want]:
        raise RuntimeError(f"expected GC {GC_INIT_NAMES[want]}, JVM selected {got}")
    owners = cfg["owner_count"]
    status, _ = http_get(f"{base_url}/api/owners/{owners}")
    if status != 200:
        raise RuntimeError(f"owner {owners} returned {status}: image lacks the seeded dataset")
    env = {**cfg.get("env", {}), **combo.get("env", {})}
    if int(env.get("HEAP_CACHE_SIZE", 0)) > 0:
        status, body = http_get(f"{base_url}/api/bench/cache/random")
        if status != 200 or not body:
            raise RuntimeError("HEAP_CACHE_SIZE set but the heap-pressure cache is empty")
    return got


# --- GC log -----------------------------------------------------------------

GC_LOG_TIME_RE = re.compile(r"^\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+[+-]\d{4})\]")
PAUSE_RE = re.compile(r"Pause .*?(?:(\d+)M->(\d+)M\((\d+)M\)\s+)?([\d.]+)ms\s*$")


def gc_line_epoch(line):
    m = GC_LOG_TIME_RE.match(line)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()


def parse_gc_log(gc_log_path, window=None):
    """Pauses, heap-after-GC and full/concurrent-cycle counts from unified GC
    logging. window=(start_epoch, end_epoch) keeps only events whose log line
    (written when the event ends) falls inside it."""
    out = {"pauses": [], "heap_after": [], "heap_after_full": [], "full": 0, "conc": 0}
    if not gc_log_path.exists():
        return out
    for line in gc_log_path.read_text(errors="ignore").splitlines():
        is_pause = "Pause" in line
        is_conc = "Concurrent Mark Cycle" in line and line.rstrip().endswith("ms")
        if not (is_pause or is_conc):
            continue
        if window is not None:
            t = gc_line_epoch(line)
            if t is None or not (window[0] <= t <= window[1]):
                continue
        if is_conc:
            out["conc"] += 1
            continue
        m = PAUSE_RE.search(line)
        if not m:
            continue  # gc,start line, no duration yet
        out["pauses"].append(float(m.group(4)))
        if m.group(2):
            out["heap_after"].append(int(m.group(2)))
        if "Pause Full" in line:
            out["full"] += 1
            if m.group(2):
                out["heap_after_full"].append(int(m.group(2)))
    return out


# --- load generator ---------------------------------------------------------

def run_k6(base_url, rate, duration, cfg, out_dir=None):
    """Runs loadtest.js. With out_dir, writes the summary and every request's
    raw timing (gzipped CSV) there, so percentiles can be recomputed exactly
    later instead of trusting k6's summary alone."""
    cmd = [
        "docker", "run", "--rm", "--network", "host",
        f"--cpuset-cpus={cfg['cpuset_loadgen']}",
        "-v", f"{BENCH_DIR}/loadtest.js:/loadtest.js",
        "-e", f"BASE_URL={base_url}",
        "-e", f"RATE={rate}",
        "-e", f"DURATION={duration}",
        "-e", f"OWNER_COUNT={cfg['owner_count']}",
        "-e", f"PRE_VUS={cfg.get('k6_pre_vus', 50)}",
        "-e", f"MAX_VUS={cfg.get('k6_max_vus', 200)}",
    ]
    if out_dir:
        # k6's image runs as a non-root user; run as our uid so it can write
        # into the bind-mounted run dir.
        cmd += ["-v", f"{out_dir}:/results", "--user", f"{os.getuid()}:{os.getgid()}"]
    cmd += ["grafana/k6:latest", "run",
            "--summary-trend-stats=avg,min,med,max,p(90),p(95),p(99),p(99.9)"]
    if out_dir:
        cmd += ["--summary-export=/results/k6_summary.json",
                "--out", "csv=/results/k6_raw.csv.gz"]
    cmd += ["/loadtest.js"]
    return sh(cmd)


# --- AppCDS priming ----------------------------------------------------------

def prime_cds_archives(combos, args, batch_dir):
    """Builds each appcds combo's archive with that combo's own flags, since
    CDS rejects an archive dumped under a different GC or header layout.
    Load before the stop puts the classes the benchmark exercises into the
    archive, not just the ones needed to start."""
    cfg = args.container_cfg
    for combo in (c for c in combos if c.get("appcds")):
        cds_dir = cds_dir_for(combo, batch_dir)
        cds_dir.mkdir(parents=True)
        run_dir = batch_dir / "runs" / f"_prime_{combo['name']}"
        run_dir.mkdir(parents=True)
        name = f"pc-prime-{combo['name']}"
        print(f"[prime] {combo['name']}: building AppCDS archive ...", flush=True)
        sh(["docker", "rm", "-f", name])
        res = sh(start_container_cmd(name, combo, run_dir, cfg, args.image, batch_dir, priming=True))
        if res.returncode != 0:
            sys.exit(f"[prime] {combo['name']}: docker run failed: {res.stderr}")
        try:
            base_url = f"http://localhost:{cfg['port']}/petclinic"
            if not wait_for_ready(f"{base_url}/api/vets", args.startup_timeout, name):
                sys.exit(f"[prime] {combo['name']}: never became ready")
            verify_run(combo, cfg, base_url, run_dir / "gc.log")
            run_k6(base_url, args.rate, args.prime_load, cfg)
        finally:
            # The archive is written during JVM exit: SIGTERM and wait
            # (docker stop), never SIGKILL (docker rm -f).
            sh(["docker", "stop", "--time", "60", name])
            (run_dir / "docker_logs.txt").write_text(sh(["docker", "logs", name]).stdout)
            sh(["docker", "rm", "-f", name])
        archive = cds_dir / "app.jsa"
        if not archive.exists() or archive.stat().st_size == 0:
            sys.exit(f"[prime] {combo['name']}: no archive at {archive}; see {run_dir}/docker_logs.txt")
        print(f"[prime] {combo['name']}: {archive.stat().st_size / 1e6:.1f} MB", flush=True)


# --- one run ----------------------------------------------------------------

def parse_startup_seconds(log_text):
    m = re.search(r"Started \S+ in ([\d.]+) seconds", log_text)
    return float(m.group(1)) if m else None


def one_run(combo, run_idx, block, args, batch_dir):
    cfg = args.container_cfg
    name = f"pc-{combo['name']}-{run_idx}"
    run_dir = batch_dir / "runs" / f"{combo['name']}_{run_idx}"
    if run_dir.exists() and any(run_dir.iterdir()):
        raise RuntimeError(f"{run_dir} already has files; refusing to overwrite another run")
    run_dir.mkdir(parents=True, exist_ok=True)

    sh(["docker", "rm", "-f", name])
    start_t = time.monotonic()
    run_res = sh(start_container_cmd(name, combo, run_dir, cfg, args.image, batch_dir))
    if run_res.returncode != 0:
        raise RuntimeError(f"docker run failed: {run_res.stderr}")
    container_id = run_res.stdout.strip()

    try:
        base_url = f"http://localhost:{cfg['port']}/petclinic"
        ready = wait_for_ready(f"{base_url}/api/vets", args.startup_timeout, name)
        wall_startup_s = time.monotonic() - start_t
        if not ready:
            raise RuntimeError("app never became ready within startup_timeout")
        log_startup_s = parse_startup_seconds(sh(["docker", "logs", name]).stdout)
        selected = verify_run(combo, cfg, base_url, run_dir / "gc.log")

        run_k6(base_url, args.rate, args.warmup, cfg)

        cgroup = Cgroup(container_id)
        env_before = host_env(args.cpus)
        cpu_before = cgroup.cpu_usage_usec()
        sampler = MemSampler(cgroup)
        sampler.start()
        measured_start = time.monotonic()
        # Wall-clock bounds to match the GC log's `time` decorator; host and
        # container share the kernel clock.
        window_start = time.time()
        run_k6(base_url, args.rate, args.measure, cfg, out_dir=run_dir)
        window_end = time.time()
        measured_wall_s = time.monotonic() - measured_start
        sampler.stop()
        sampler.join(timeout=5)
        cpu_after = cgroup.cpu_usage_usec()
        env_after = host_env(args.cpus)
        if env_after != env_before:
            raise RuntimeError(f"host settings changed during the run: {env_before} -> {env_after}")

        summary_path = run_dir / "k6_summary.json"
        k6 = json.loads(summary_path.read_text()).get("metrics", {}) if summary_path.exists() else {}
        dur = k6.get("http_req_duration", {})

        gc_log = run_dir / "gc.log"
        win = parse_gc_log(gc_log, window=(window_start, window_end))
        whole = parse_gc_log(gc_log)
        pauses = sorted(win["pauses"])
        cpu_delta = (cpu_after - cpu_before) if None not in (cpu_before, cpu_after) else None

        return {
            "combo": combo["name"], "run_idx": run_idx, "block": block,
            "image_id": args.image_id, "gc_selected": selected,
            "appcds": bool(combo.get("appcds")),
            "window_start_epoch": round(window_start, 3), "window_end_epoch": round(window_end, 3),
            "startup_log_s": log_startup_s,
            "startup_wall_s": round(wall_startup_s, 3),
            "measured_wall_s": round(measured_wall_s, 3),
            "cpu_delta_usec": cpu_delta,
            "cpu_pct_of_pinned": (
                round(100 * cpu_delta / (measured_wall_s * 1_000_000 * cfg["app_core_count"]), 2)
                if cpu_delta is not None else None),
            "mem_max_bytes": max(sampler.current, default=None),
            "mem_anon_max_bytes": max(sampler.anon, default=None),
            "gc_pause_count": len(pauses),
            "gc_pause_count_all": len(whole["pauses"]),
            "gc_pause_max_ms": pauses[-1] if pauses else None,
            "gc_pause_max_all_ms": max(whole["pauses"], default=None),
            "gc_full_count": win["full"],
            "gc_full_count_all": whole["full"],
            "gc_conc_cycle_count": win["conc"],
            "gc_pause_p50_ms": percentile(pauses, 50),
            "gc_pause_p99_ms": percentile(pauses, 99),
            "gc_pause_p999_ms": percentile(pauses, 99.9),
            # Heap after any GC includes old-generation garbage on young GCs;
            # the minimum after a Full GC is the closest log-derived figure to
            # the live set, when a Full GC happened in the window.
            "heap_after_gc_median_mb": (
                sorted(win["heap_after"])[len(win["heap_after"]) // 2] if win["heap_after"] else None),
            "heap_after_full_gc_min_mb": min(win["heap_after_full"], default=None),
            "http_p50_ms": dur.get("med"),
            "http_p90_ms": dur.get("p(90)"),
            "http_p99_ms": dur.get("p(99)"),
            "http_p999_ms": dur.get("p(99.9)"),
            "http_max_ms": dur.get("max"),
            "http_reqs": k6.get("http_reqs", {}).get("count"),
            "http_req_failed_rate": k6.get("http_req_failed", {}).get("value"),
            "k6_dropped_iterations": k6.get("dropped_iterations", {}).get("count", 0),
            "k6_vus_max": k6.get("vus_max", {}).get("max"),
            **env_after,
        }
    finally:
        (run_dir / "docker_logs.txt").write_text(sh(["docker", "logs", name]).stdout)
        sh(["docker", "rm", "-f", name])


# --- main -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--flags", required=True)
    ap.add_argument("--image", default=IMAGE)
    ap.add_argument("--repeats", type=int, default=10)
    ap.add_argument("--rate", type=int, default=40, help="req/s, open-loop constant arrival")
    ap.add_argument("--warmup", default="5m")
    ap.add_argument("--measure", default="30m")
    ap.add_argument("--prime-load", default="2m",
                    help="load applied to each AppCDS priming run before its graceful stop")
    ap.add_argument("--startup-timeout", type=float, default=120)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", required=True,
                    help="results CSV; its stem names the batch folder that holds raw per-run files")
    ap.add_argument("--only", nargs="*", help="restrict to these combo names (for smoke-testing)")
    ap.add_argument("--allow-governor", action="store_true",
                    help="run even if the app/loadgen CPUs are not on the performance governor")
    args = ap.parse_args()

    cfg = json.loads(Path(args.flags).read_text())
    args.container_cfg = cfg["container"]
    args.cpus = [int(c) for c in
                 f"{cfg['container']['cpuset_app']},{cfg['container']['cpuset_loadgen']}".split(",")]
    args.container_cfg["app_core_count"] = len(cfg["container"]["cpuset_app"].split(","))

    args.image_id = image_id(args.image)
    if not args.image_id:
        sys.exit(f"image {args.image} not found; build it with bench/Dockerfile")

    env = host_env(args.cpus)
    if env["governor"] != "performance" and not args.allow_governor:
        sys.exit(f"CPUs {args.cpus} are on governor '{env['governor']}', not 'performance'. "
                 f"Fix it or pass --allow-governor.")
    if env["smt_active"] == "1":
        print("WARNING: SMT is on; the app and load generator share physical cores with "
              "other logical CPUs. See README for isolating them.", file=sys.stderr)

    # With --network host, a stray container on this port makes every health
    # check pass against the wrong process.
    port = args.container_cfg["port"]
    if http_get(f"http://localhost:{port}/petclinic/api/vets", timeout=2)[0] is not None:
        sys.exit(f"Port {port} already has something answering; remove it (`docker ps`) first.")

    combos = cfg["combos"]
    if args.only:
        combos = [c for c in combos if c["name"] in args.only]
        if not combos:
            sys.exit(f"no combo matched --only {args.only}")

    out_path = Path(args.out).resolve()  # absolute: used in docker -v bind mounts
    batch = out_path.stem
    batch_dir = out_path.parent / batch
    if out_path.exists() or batch_dir.exists():
        sys.exit(f"{out_path} or {batch_dir} already exists; pick a new --out for a new batch")
    batch_dir.mkdir(parents=True)
    (batch_dir / "flags.json").write_text(Path(args.flags).read_text())
    (batch_dir / "invocation.json").write_text(json.dumps(
        {"argv": sys.argv, "image": args.image, "image_id": args.image_id, **env,
         "started": datetime.now().astimezone().isoformat()}, indent=2))

    prime_cds_archives(combos, args, batch_dir)

    rng = random.Random(args.seed)
    plan = []
    for block in range(args.repeats):
        order = list(combos)
        rng.shuffle(order)
        plan += [(c, block, block) for c in order]

    print(f"{len(plan)} runs planned ({len(combos)} combos x {args.repeats} blocks), "
          f"each ~startup + {args.warmup} warmup + {args.measure} measured")

    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ROW_FIELDS)
        writer.writeheader()
        for n, (combo, run_idx, block) in enumerate(plan, 1):
            print(f"[{n}/{len(plan)}] block={block} combo={combo['name']} ...", flush=True)
            try:
                row = one_run(combo, run_idx, block, args, batch_dir)
            except Exception as e:
                print(f"  FAILED: {e}", file=sys.stderr)
                row = {"combo": combo["name"], "run_idx": run_idx, "block": block, "error": str(e)}
            row["batch"] = batch
            writer.writerow(row)
            f.flush()
            print(f"  -> {row}")

    print(f"done. results in {out_path}, raw files in {batch_dir}")


if __name__ == "__main__":
    main()
