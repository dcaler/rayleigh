"""`rayleigh conduct_exp <E>` — run one experiment's cells against code/.

rayleigh reimplements none of the model. It expands an experiment's `design` into cells
(one parameter combo × one seed), then invokes code/'s single-run entrypoint per cell,
skipping cells whose output already exists (restartable) and stamping provenance.

The run_adapter (file-level `code.run_adapter` in experiments.yaml) says HOW to run one cell:

  kind: import
    entrypoint: "module:callable"
    CONTRACT: callable(params: dict, seed: int, output: str) -> None
      runs one cell and writes its result to `output`. `init` authors a thin shim in
      code/ (or results/) that adapts the real entrypoint to this signature.

  kind: subprocess
    command: "python -m pkg.cli run --config {config} --output {out}"
    CONTRACT: rayleigh writes {**params, "seed": seed} to a per-cell JSON config and runs
      the command, substituting {config}, {out}, {seed}, and any {param} placeholders.
"""

import itertools
import json
import multiprocessing as mp
import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime, timezone
from pathlib import Path

import yaml

from rayleigh import __version__
from rayleigh.spec import active_spec_path

DEFAULT_WORKERS = 8
DEFAULT_TEMPLATE = "data/{experiment}/{cellkey}_seed{seed}.parquet"


def log(msg: str) -> None:
    print(f"[rayleigh conduct_exp] {msg}", flush=True)


# --------------------------------------------------------------------- resource sizing
def _available_ram_gb() -> float | None:
    """Usable RAM in GiB, or None if it can't be read (non-Linux, restricted /proc)."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / (1024 * 1024)   # kB -> GiB
    except Exception:
        pass
    try:                                       # POSIX fallback: free pages x page size
        return (os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")) / (1024 ** 3)
    except (ValueError, OSError, AttributeError):
        return None


def _pow2_floor(n: int) -> int:
    """Largest power of two <= n (>=1). Snaps worker counts to a clean 1/2/4/8/16/32… ladder.
    Floor (not nearest) so normalizing never *raises* parallelism into a RAM wall — 28 -> 16."""
    return 1 << (n.bit_length() - 1) if n >= 1 else 1


def _plan_workers(requested: int) -> tuple[int, str]:
    """Normalize a requested worker count to a sane pool for THIS machine and describe it.

    Two mechanical guards rayleigh can enforce without knowing per-cell RAM:
      * cap at core count (a pool wider than cores only adds contention), and
      * snap to a power of two (8/16/32…) — arbitrary counts like 28 buy nothing and
        the floor keeps the change on the safe side of memory.
    Per-run RAM is the experiment author's call (PLANNING.md); we surface what we can see
    (cores, available RAM) and flag a thin <1 GiB/worker margin rather than clamp silently."""
    cores = os.cpu_count() or requested
    effective = max(1, _pow2_floor(min(requested, cores)))
    ram = _available_ram_gb()
    ram_str = f"{ram:.0f} GiB avail" if ram is not None else "RAM unknown"
    parts = [f"{cores} cores", ram_str]
    if effective != requested:
        parts.append(f"workers {requested} -> {effective} (<= cores, snapped to power of two)")
    else:
        parts.append(f"workers {effective}")
    if ram is not None and effective > ram:            # fewer than ~1 GiB per worker
        parts.append(f"⚠ <1 GiB/worker — watch for swap/OOM (see PLANNING.md sizing)")
    return effective, " · ".join(parts)


# ---- memory guard: MEASURE, SIZE the pool, then WATCH real RAM at runtime -------------------
# The design session's per-run RAM figure is an estimate; an estimate that's wrong by a few x and
# fanned out over a wide pool is exactly how a run OOMs the machine. So conduct_exp does not trust
# the estimate. THREE layers, each covering the previous one's blind spot:
#   1. MEASURE — run the first cell alone, sample its real peak RSS, and size the pool so the
#      whole pool's expected RSS fits a fraction of available RAM.
#   2. WATCH — because one pilot cell can under-predict heavier cells (a heterogeneous grid),
#      a daemon thread samples the *system's* MemAvailable during the run and, if it falls
#      toward the OOM line, SIGKILLs the largest worker to relieve pressure BEFORE the kernel
#      OOM-killer (or swap-death) takes the box. The sacrificed cell is restartable (its output
#      never landed), and the pool is rebuilt with fewer workers to finish the rest.
#   3. RLIMIT_AS backstop — a *generous, runaway-only* address-space ceiling so a single insane
#      allocation dies with a clean per-cell MemoryError (pool intact). It is NOT sized tight to
#      the pilot: VmSize massively over-reserves vs. resident memory (numpy/BLAS/glibc arenas),
#      so a tight virtual cap false-trips normal cells — real RAM pressure is layer 2's job.
# The guarantee — a run CAN NOT take the box into OOM — is enforced here at runtime, on measured
# *resident* memory, not assumed from the spec or from a virtual-size estimate.
_MEM_SAFETY = 0.75          # commit at most this fraction of *available* RAM to the whole pool
_HEADROOM = 1.5             # size workers assuming a cell may grow to this multiple of pilot RSS
_RUNAWAY_AS_MULT = 4.0      # RLIMIT_AS ceiling = this * pilot VmSize (floored at avail RAM): only
                            #   a cell whose *virtual* size dwarfs the machine is a true runaway
_MEM_FLOOR_FRAC = 0.08      # watchdog trips when MemAvailable drops below this fraction of the
_MEM_FLOOR_MIN_GB = 2.0     #   RAM seen at start (or this absolute floor, whichever is larger)
_WATCH_POLL_S = 0.5         # how often the watchdog samples MemAvailable


def _proc_mem_bytes(pid: int) -> tuple[int, int]:
    """(VmRSS, VmSize) in bytes for one pid, (0, 0) if it's gone."""
    rss = vsize = 0
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
                elif line.startswith("VmSize:"):
                    vsize = int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return rss, vsize


def _proc_tree(root_pid: int) -> list[int]:
    """`root_pid` plus all its descendants, via /proc ppid links (a cell may fork children)."""
    kids: dict[int, list[int]] = {}
    try:
        entries = [e for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return [root_pid]
    for e in entries:
        try:
            with open(f"/proc/{e}/stat") as fh:
                data = fh.read()
            ppid = int(data.rsplit(")", 1)[1].split()[1])   # field 4, after the (comm) group
        except (OSError, IndexError, ValueError):
            continue
        kids.setdefault(ppid, []).append(int(e))
    out, stack = [], [root_pid]
    while stack:
        p = stack.pop()
        out.append(p)
        stack.extend(kids.get(p, []))
    return out


def _tree_mem_bytes(root_pid: int) -> tuple[int, int]:
    """(sum RSS over the tree, max VmSize of any one process in it). RSS drives how many cells
    fit in physical RAM; the largest single VmSize drives the per-worker address-space ceiling."""
    rss_sum = vsize_max = 0
    for p in _proc_tree(root_pid):
        r, v = _proc_mem_bytes(p)
        rss_sum += r
        vsize_max = max(vsize_max, v)
    return rss_sum, vsize_max


def _pilot_entry(job: dict, conn) -> None:
    conn.send(_execute_cell(job))
    conn.close()


def _measure_pilot(job: dict) -> tuple[float, float, dict]:
    """Run ONE cell in a child process and sample its process-tree footprint to peak.
    Returns (peak_tree_RSS_GiB, peak_single_VmSize_GiB, cell_result). The cell's output is
    real work — kept, not thrown away."""
    ctx = mp.get_context("fork")            # fork: child inherits sys.path; pid is sampleable
    parent_conn, child_conn = ctx.Pipe()
    proc = ctx.Process(target=_pilot_entry, args=(job, child_conn))
    proc.start()
    peak_rss = peak_vsize = 0
    while proc.is_alive():
        rss, vsize = _tree_mem_bytes(proc.pid)
        peak_rss, peak_vsize = max(peak_rss, rss), max(peak_vsize, vsize)
        time.sleep(0.1)
    proc.join()
    if parent_conn.poll():
        result = parent_conn.recv()
    else:                                    # child died without reporting (e.g. OOM-killed)
        primary = next(iter(job["outputs"].values()))
        result = {"output": str(primary), "status": "failed",
                  "error": "pilot cell produced no result (killed before returning?)"}
    return peak_rss / (1024 ** 3), peak_vsize / (1024 ** 3), result


def _size_by_memory(peak_rss_gb: float, peak_vsize_gb: float,
                    machine_workers: int, resource_kind: str) -> tuple[int, int, float, str]:
    """From the measured pilot footprint, decide (workers, RLIMIT_AS bytes, watchdog floor GiB,
    report). workers is chosen so the whole pool's *resident* memory fits the RAM budget; the AS
    ceiling is a generous, runaway-ONLY backstop (see module notes — a tight virtual cap false-
    trips normal numpy/BLAS cells); the watchdog floor is the MemAvailable level below which the
    runtime watchdog kills a worker. resource=gpu keeps RSS sizing but sets no AS cap (CUDA
    reserves huge virtual ranges a cap would false-trip)."""
    avail = _available_ram_gb()
    gpu = str(resource_kind).lower() == "gpu"
    if avail is None:                        # can't see RAM: don't pretend to guarantee anything
        return machine_workers, 0, 0.0, (f"RAM unreadable — kept workers={machine_workers}, no "
                                         f"cap/watchdog (watch this run for swap)")
    budget = avail * _MEM_SAFETY
    per_cell = max(peak_rss_gb, 0.01)
    by_mem = max(1, int(budget // (per_cell * _HEADROOM)))
    workers = max(1, _pow2_floor(min(machine_workers, by_mem)))
    # Runaway-only AS ceiling: only a cell whose VIRTUAL size dwarfs the machine is a real
    # runaway. Floored at the whole available RAM so normal virtual-arena inflation never trips it.
    ceiling_gb = max(peak_vsize_gb * _RUNAWAY_AS_MULT, avail)
    as_bytes = 0 if gpu else int(ceiling_gb * (1024 ** 3))
    floor_gb = max(avail * _MEM_FLOOR_FRAC, _MEM_FLOOR_MIN_GB)
    report = (f"pilot peak {peak_rss_gb:.1f} GiB RSS / {peak_vsize_gb:.1f} GiB virt · "
              f"{avail:.0f} GiB avail, budget {budget:.0f} GiB ({int(_MEM_SAFETY * 100)}%) → "
              f"workers {machine_workers}→{workers}")
    report += (" · resource=gpu, RSS-sized, no address-space cap" if gpu
               else f" · RLIMIT_AS {ceiling_gb:.0f} GiB (runaway backstop) · "
                    f"watchdog kills at MemAvailable < {floor_gb:.1f} GiB")
    if workers < machine_workers:
        report += "  [reduced to fit RAM]"
    return workers, as_bytes, floor_gb, report


def _limit_address_space(as_bytes: int) -> None:
    """ProcessPoolExecutor initializer: cap this worker's virtual memory as a runaway backstop.
    A cell that exceeds it gets MemoryError/ENOMEM (and any subprocess it spawns inherits the
    limit) instead of swapping the whole machine. Deliberately generous — real RAM pressure is
    the watchdog's job, not this cap's. No-op when unset or unsupported."""
    if not as_bytes or as_bytes <= 0:
        return
    try:
        import resource as R
        _soft, hard = R.getrlimit(R.RLIMIT_AS)
        R.setrlimit(R.RLIMIT_AS, (int(as_bytes), hard))
    except (ImportError, ValueError, OSError):
        pass


def _kill_tree(root_pid: int) -> None:
    """SIGKILL a worker and every process it spawned (leaf-first)."""
    for pid in reversed(_proc_tree(root_pid)):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _rss_watchdog(executor: ProcessPoolExecutor, floor_gb: float,
                  stop_evt: threading.Event, killed: list) -> None:
    """Daemon thread: the RUNTIME box-protector. Samples the system's MemAvailable; if it falls
    below `floor_gb`, SIGKILLs the single largest worker subtree to relieve pressure before the
    kernel OOM-killer (or swap-death) hits. Records the kill so the caller rebuilds the pool with
    fewer workers and retries the sacrificed cell (its output never landed → restartable).

    Cheap in the common case: it only reads /proc/meminfo each tick and does the (heavier)
    per-worker tree walk when memory is already at the floor."""
    while not stop_evt.wait(_WATCH_POLL_S):
        avail = _available_ram_gb()
        if avail is None or avail >= floor_gb:
            continue
        procs = list(getattr(executor, "_processes", {}) or {})   # worker root pids
        if not procs:
            continue
        victim = max(procs, key=lambda pid: _tree_mem_bytes(pid)[0])
        rss_gb = _tree_mem_bytes(victim)[0] / (1024 ** 3)
        _kill_tree(victim)
        killed.append(victim)
        log(f"memory watchdog: MemAvailable {avail:.1f} GiB < floor {floor_gb:.1f} GiB — killed "
            f"worker pid {victim} (~{rss_gb:.1f} GiB RSS) to protect the box; its cell will retry.")
        # give the OS a moment to reclaim before the next sample, so we don't over-kill one dip
        if stop_evt.wait(_WATCH_POLL_S):
            return


def add_import_paths(*dirs) -> None:
    """Make an `import`-kind adapter resolvable: put its candidate homes on sys.path.
    The run-adapter shim (and the output_adapter loader) may live in code/ OR results/, so
    both are searched. Shared by conduct_exp and process_outputs."""
    for d in dirs:
        if d and str(d) not in sys.path:
            sys.path.insert(0, str(d))


# --------------------------------------------------------------------- spec + cells
def _load_spec(results: Path) -> dict:
    spec_path = active_spec_path(results / "designdocs")
    if not spec_path.is_file():
        raise FileNotFoundError(
            f"no {spec_path} — run `rayleigh init` and design experiments first")
    spec = yaml.safe_load(spec_path.read_text()) or {}
    if not spec.get("experiments"):
        raise ValueError(f"{spec_path} has no `experiments:` — author them in `rayleigh init`")
    return spec


def _find_experiment(spec: dict, eid: str) -> dict:
    for exp in spec["experiments"]:
        if str(exp.get("id")) == eid:
            return exp
    ids = ", ".join(str(e.get("id")) for e in spec["experiments"])
    raise KeyError(f"experiment '{eid}' not found. Known ids: {ids}")


def _sanitize(v) -> str:
    return re.sub(r"[^A-Za-z0-9.+-]", "", str(v))


def _cellkey(params: dict) -> str:
    """Deterministic, filename-safe slug for a parameter combo."""
    if not params:
        return "cell"
    return "_".join(f"{k}-{_sanitize(v)}" for k, v in sorted(params.items()))


def _param_combos(design: dict) -> list[dict]:
    """The DISCRETE parameter side of the design (before seeds), as a list of param dicts.
    (Continuous `sobol` designs are expanded separately by `_sobol_combos`.)"""
    kind = design.get("kind", "sweep")
    if kind == "sweep":
        axes = design.get("axes") or {}
        if not axes:
            return [{}]
        keys = list(axes)
        return [dict(zip(keys, combo)) for combo in itertools.product(*(axes[k] for k in keys))]
    if kind == "conditions":
        conds = design.get("conditions") or []
        return [dict(c) for c in conds]
    raise NotImplementedError(
        f"design.kind '{kind}' is not yet supported by conduct_exp — use 'sweep', "
        "'conditions', or 'sobol' (oat/ablation are planned).")


def _sobol_points(n_draws: int, dims: list, scramble: bool = True, seed: int = 0) -> list[dict]:
    """`n_draws` points in the continuous box `dims` = [(name, (lo, hi)), …], as param dicts.

    Scrambled Sobol (randomized quasi-Monte-Carlo — low-discrepancy, space-filling) via scipy
    when present; a numpy Latin-Hypercube fallback otherwise. Both are DETERMINISTIC in `seed`,
    so conduct_exp and process_outputs regenerate the identical point set (the cell<->params map
    must be stable across runs)."""
    d = len(dims)
    los = [b[0] for _, b in dims]
    his = [b[1] for _, b in dims]
    try:
        import warnings
        from scipy.stats import qmc
        with warnings.catch_warnings():                  # n_draws need not be a power of two
            warnings.simplefilter("ignore")
            unit = qmc.Sobol(d=d, scramble=scramble, seed=seed).random(n_draws)
            scaled = qmc.scale(unit, los, his)
        return [{dims[j][0]: float(scaled[i][j]) for j in range(d)} for i in range(n_draws)]
    except Exception as e:                               # noqa: BLE001  (scipy absent / mismatch)
        import numpy as np
        log(f"scipy Sobol unavailable ({type(e).__name__}) — using a numpy Latin-Hypercube fallback")
        rng = np.random.default_rng(seed)
        u = (np.arange(n_draws)[:, None] + rng.random((n_draws, d))) / n_draws   # stratified
        for j in range(d):
            rng.shuffle(u[:, j])
        return [{dims[j][0]: float(los[j] + u[i][j] * (his[j] - los[j])) for j in range(d)}
                for i in range(n_draws)]


def _sobol_combos(design: dict) -> list[tuple]:
    """Expand a `kind: sobol` design to [(params, cellkey_tag), …]: `n_draws` continuous points
    (shared across categorical arms, so draw j is comparable across arms) x the Cartesian product
    of `categorical:`. The cellkey is `<categorical>_dNNNN` — the draw index keeps continuous
    cells unique and short (the exact float params live in the tidy data, regenerated identically)."""
    cont = design.get("continuous") or {}
    if not cont:
        raise ValueError("sobol design needs a `continuous:` box, e.g. {size_frac: {min: 0, max: 1}}")
    dims = []
    for name, b in cont.items():
        if isinstance(b, dict):
            lo, hi = b.get("min"), b.get("max")
        elif isinstance(b, (list, tuple)) and len(b) == 2:
            lo, hi = b
        else:
            raise ValueError(f"continuous param {name!r} needs {{min, max}} or [lo, hi], got {b!r}")
        dims.append((name, (float(lo), float(hi))))
    n_draws = int(design.get("n_draws", 256))
    scramble = str(design.get("sampler", "sobol_scrambled")).lower() != "sobol"   # 'sobol' -> unscrambled
    pts = _sobol_points(n_draws, dims, scramble=scramble, seed=int(design.get("sampler_seed", 0)))
    cats = design.get("categorical") or {}
    if cats:
        keys = list(cats)
        cat_combos = [dict(zip(keys, c)) for c in itertools.product(*(cats[k] for k in keys))]
    else:
        cat_combos = [{}]
    out = []
    for cat in cat_combos:
        ck = _cellkey(cat)
        for j, pt in enumerate(pts):
            tag = (f"{ck}_" if ck != "cell" else "") + f"d{j:04d}"
            out.append(({**cat, **pt}, tag))
    return out


def expand_cells(exp: dict, adapter: dict) -> list[dict]:
    """Expand an experiment into cells: [{params, seed, cellkey}, ...].

    `fixed:` (a dict of constant params) is merged into every cell — the way to pin a
    parameter across a whole experiment that isn't a swept axis (e.g. an arm selector
    `mode: monkey` shared by every cell of a grid). A swept axis of the same name wins."""
    design = exp.get("design") or {}
    if str(design.get("kind", "sweep")) == "sobol":
        combos = _sobol_combos(design)                    # [(params, keytag), …]
    else:
        combos = [(p, None) for p in _param_combos(design)]
    # `fixed:` constants may sit at the experiment level or inside `design:` — accept both
    # (design-level is a natural home for a design constant). Experiment-level wins on a clash;
    # a swept/drawn param wins over either.
    fixed = {}
    for src in (design.get("fixed"), exp.get("fixed")):
        if src is None:
            continue
        if not isinstance(src, dict):
            raise ValueError(f"experiment {exp.get('id')}: `fixed` must be a mapping, got {src!r}")
        fixed.update(src)
    seeds = exp.get("seeds", design.get("seeds", 1))
    try:
        n_seeds = int(seeds)
    except (TypeError, ValueError):
        raise ValueError(f"experiment {exp.get('id')}: `seeds` must be an integer, got {seeds!r}")
    cells = []
    for params, keytag in combos:
        p = {**fixed, **params} if fixed else dict(params)   # swept/drawn param overrides a fixed key
        key = keytag or _cellkey(p)
        for s in range(n_seeds):
            cells.append({"params": p, "seed": s, "cellkey": key})
    return cells


def resolve_cell_outputs(adapter: dict, results: Path, eid: str, cell: dict) -> dict:
    """Resolve a cell's output artifact path(s) → {name: Path}.

    Two forms in run_adapter (a run may write several files, e.g. a trajectory + a summary):
      - single: `output_template: "…"`                    -> {"output": Path}
      - multi:  `outputs: {timeseries: "…", summary: "…"}` -> {"timeseries": Path, "summary": Path}
    """
    outs = adapter.get("outputs")
    templates = dict(outs) if outs else {"output": adapter.get("output_template") or DEFAULT_TEMPLATE}
    key = cell.get("cellkey") or _cellkey(cell["params"])
    resolved = {}
    for name, tmpl in templates.items():
        try:
            rel = str(tmpl).format(experiment=eid, cellkey=key, seed=cell["seed"], **cell["params"])
        except KeyError as e:
            raise KeyError(
                f"output template for '{name}' references {e}, not a param/known field. "
                f"Available: experiment, cellkey, seed, {', '.join(cell['params']) or '(no params)'}")
        resolved[name] = (results / rel).resolve()
    return resolved


# --------------------------------------------------------------------- execution
def _partial(final: Path) -> Path:
    """The temp path a cell writes to before its atomic rename to `final`. Keeps `final`'s
    extension so extension-sniffing writers/loaders still see the right type."""
    return final.with_name(f"{final.stem}.partial{final.suffix}")


def _execute_cell(job: dict) -> dict:
    """Run one cell. Top-level so it is picklable for ProcessPoolExecutor.
    A cell may write several named artifacts (job["outputs"] = {name: path}).

    ATOMIC: the adapter writes to `.partial` temp paths; only after ALL of them exist does
    rayleigh rename them into place. So a cell killed mid-write (e.g. by the memory watchdog)
    leaves only orphan `.partial` files — the final path never appears, so restart re-runs the
    cell and process_outputs never ingests a truncated file. A cell is 'done' iff its finals exist."""
    outs = {n: Path(p) for n, p in job["outputs"].items()}
    tmps = {n: _partial(p) for n, p in outs.items()}
    primary = next(iter(outs.values()))
    for p in outs.values():
        p.parent.mkdir(parents=True, exist_ok=True)
    try:
        if job["kind"] == "import":
            # The adapter shim may live in code/ OR results/ (both documented). Put both on
            # sys.path so `import <shim>` resolves wherever the design session authored it.
            add_import_paths(job["code_dir"], job.get("results_dir"))
            mod_name, _, fn_name = job["entrypoint"].partition(":")
            if not fn_name:
                raise ValueError(f"entrypoint '{job['entrypoint']}' must be 'module:callable'")
            import importlib
            fn = getattr(importlib.import_module(mod_name), fn_name)
            if job["multi"]:                     # outputs: mapping -> callable(params, seed, outputs)
                fn(params=job["params"], seed=job["seed"],
                   outputs={n: str(p) for n, p in tmps.items()})
            else:                                # single output_template -> callable(params, seed, output)
                fn(params=job["params"], seed=job["seed"], output=str(tmps["output"]
                   if "output" in tmps else next(iter(tmps.values()))))
        elif job["kind"] == "subprocess":
            cfg_path = Path(job["config_path"])
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(json.dumps({**job["params"], "seed": job["seed"]}, indent=2))
            primary_tmp = next(iter(tmps.values()))
            subs = {"config": str(cfg_path), "out": str(primary_tmp), "seed": job["seed"],
                    **job["params"], **{f"out_{n}": str(p) for n, p in tmps.items()}}
            cmd = job["command"].format(**subs)
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               cwd=job.get("cwd"))
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout).strip()[:400] or f"exit {r.returncode}")
        else:
            raise ValueError(f"unknown run_adapter kind '{job['kind']}' (use import|subprocess)")

        missing = [str(t) for t in tmps.values() if not t.exists()]
        if missing:
            raise RuntimeError(f"entrypoint returned but wrote no output at {', '.join(missing)}")
        for n, tmp in tmps.items():              # publish atomically (same dir -> rename is atomic)
            os.replace(tmp, outs[n])
        _write_provenance(job, outs, primary)
        return {"output": str(primary), "status": "done", "error": None}
    except Exception as e:                       # noqa: BLE001 — report, don't crash the pool
        for t in tmps.values():                  # don't leave partials from a failed run lying around
            try:
                t.unlink()
            except OSError:
                pass
        return {"output": str(primary), "status": "failed", "error": f"{type(e).__name__}: {e}"}


def _write_provenance(job: dict, outs: dict, primary: Path) -> None:
    prov = {
        "experiment": job["experiment"],
        "params": job["params"],
        "seed": job["seed"],
        "outputs": {n: str(p) for n, p in outs.items()},
        "code_sha": job["code_sha"],
        "rayleigh_version": __version__,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    prov["entrypoint" if job["kind"] == "import" else "command"] = (
        job.get("entrypoint") or job.get("command"))
    primary.with_suffix(primary.suffix + ".prov.json").write_text(json.dumps(prov, indent=2))


def _code_sha(code_dir: Path) -> str:
    try:
        r = subprocess.run(["git", "-C", str(code_dir), "rev-parse", "HEAD"],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


# --------------------------------------------------------------------- PROGRESS.md
def _update_progress(results: Path, eid: str, mark: str) -> None:
    """Best-effort: set the experiment's row status in PROGRESS.md to `mark` (✅/🟦/❌).
    Skips quietly if the table structure doesn't match (it's an authored doc — never corrupt it)."""
    p = results / "designdocs" / "PROGRESS.md"
    if not p.is_file():
        return
    lines = p.read_text().splitlines()
    header_idx = next((i for i, l in enumerate(lines)
                       if "Conducted" in l and l.lstrip().startswith("|")), None)
    if header_idx is None:
        return
    cols = [c.strip() for c in lines[header_idx].strip().strip("|").split("|")]
    try:
        cond_col = cols.index("Conducted")
    except ValueError:
        return
    for i in range(header_idx + 2, len(lines)):        # skip header + separator
        row = lines[i]
        if not row.lstrip().startswith("|"):
            continue
        cells = row.strip().strip("|").split("|")
        if len(cells) <= cond_col or cells[0].strip() != eid:
            continue
        cells[cond_col] = f" {mark} "
        lines[i] = "|" + "|".join(cells) + "|"
        p.write_text("\n".join(lines) + "\n")
        log(f"PROGRESS.md: {eid} → {mark}")
        return


# --------------------------------------------------------------------- command
def run_conduct_exp(args) -> int:
    root = Path(args.dir).resolve() if getattr(args, "dir", None) else Path.cwd()
    results = root / "results"
    try:
        spec = _load_spec(results)
        exp = _find_experiment(spec, args.experiment)
    except (FileNotFoundError, ValueError, KeyError) as e:
        log(str(e))
        return 1

    eid = str(exp["id"])
    code = spec.get("code") or {}
    adapter = code.get("run_adapter") or {}
    kind = adapter.get("kind", "import")
    multi = bool(adapter.get("outputs"))     # mapping form -> callable(params, seed, outputs)
    requested = getattr(args, "workers", 0) or exp.get("workers") or adapter.get("workers") or DEFAULT_WORKERS
    workers, worker_report = _plan_workers(requested)
    code_dir = (results / (code.get("path") or "../code")).resolve()

    try:
        cells = expand_cells(exp, adapter)
    except (NotImplementedError, ValueError) as e:
        log(str(e))
        return 1

    # resolve output artifact(s) per cell; a cell is done only when ALL its outputs exist
    try:
        for c in cells:
            c["outputs"] = resolve_cell_outputs(adapter, results, eid, c)
    except KeyError as e:
        log(str(e))
        return 1
    todo = [c for c in cells if not all(p.exists() for p in c["outputs"].values())]
    done_already = len(cells) - len(todo)

    nout = len(cells[0]["outputs"]) if cells else 1
    log(f"experiment {eid}: {len(cells)} cells ({done_already} already done, {len(todo)} to run) "
        f"· kind={kind} · {nout} output(s)/cell")
    log(f"resources: {worker_report}")

    if getattr(args, "dry_run", False):
        for c in cells[:20]:
            state = "done" if all(p.exists() for p in c["outputs"].values()) else "TODO"
            names = ", ".join(p.name for p in c["outputs"].values())
            log(f"  [{state}] {c['params']} seed={c['seed']} -> {names}")
        if len(cells) > 20:
            log(f"  … and {len(cells) - 20} more")
        return 0

    if getattr(args, "limit", 0):
        todo = todo[: args.limit]
        log(f"--limit: running {len(todo)} cells this pass")
    if not todo:
        log("nothing to run — all cells already have output.")
        return 0

    sha = _code_sha(code_dir)
    jobs = [{
        "experiment": eid, "kind": kind, "multi": multi, "params": c["params"], "seed": c["seed"],
        "outputs": {n: str(p) for n, p in c["outputs"].items()},
        "code_dir": str(code_dir), "results_dir": str(results), "code_sha": sha,
        "cwd": str(root),   # subprocess commands run from the project root
        "entrypoint": adapter.get("entrypoint"), "command": adapter.get("command"),
        "config_path": str(results / "data" / eid / "_configs"
                           / f"{_cellkey(c['params'])}_seed{c['seed']}.json"),
    } for c in todo]

    results_out = []
    mem_guard = not getattr(args, "no_mem_guard", False)
    resource_kind = exp.get("resource", "cpu")
    as_ceiling = 0
    watch_floor = 0.0
    run_jobs = jobs

    # MEASURE before fanning out: run the first cell alone, size the pool from its real footprint,
    # and derive the runaway backstop + watchdog floor. This is what keeps a wide pool from OOM-ing
    # the box (see the memory-guard module notes for the three layers).
    if mem_guard:
        peak_rss, peak_vsize, pilot_res = _measure_pilot(jobs[0])
        results_out.append(pilot_res)
        run_jobs = jobs[1:]
        workers, as_ceiling, watch_floor, size_report = _size_by_memory(
            peak_rss, peak_vsize, workers, resource_kind)
        log(f"memory guard: {size_report}")
        if pilot_res["status"] == "failed":
            log(f"  ⚠ pilot cell failed — {pilot_res['error']}")
    else:
        log("memory guard: DISABLED (--no-mem-guard) — pool sized by --workers only, no RAM ceiling")

    if not run_jobs:
        pass
    elif not mem_guard and workers <= 1:
        results_out += [_execute_cell(j) for j in run_jobs]     # in-process: easiest to debug
    else:
        # Route even the serial (workers==1) guarded case through the pool so the RLIMIT_AS
        # ceiling lands on a *worker*, never on rayleigh's own process. The watchdog may SIGKILL a
        # worker under memory pressure — that breaks the pool — so we run in a loop: whatever cells
        # didn't land (restartable by output-existence) are retried with a HALVED pool until they
        # finish or a single cell can't fit even alone.
        remaining = run_jobs
        cur_workers = max(1, workers)
        attempt = 0
        gpu = str(resource_kind).lower() == "gpu"
        while remaining:
            attempt += 1
            before = len(remaining)
            stop_evt = threading.Event()
            killed: list = []
            with ProcessPoolExecutor(max_workers=cur_workers,
                                     initializer=_limit_address_space,
                                     initargs=(as_ceiling,)) as ex:
                watcher = None
                if mem_guard and watch_floor > 0 and not gpu:
                    watcher = threading.Thread(
                        target=_rss_watchdog, args=(ex, watch_floor, stop_evt, killed), daemon=True)
                    watcher.start()
                futs = [ex.submit(_execute_cell, j) for j in remaining]
                try:
                    for f in as_completed(futs):
                        results_out.append(f.result())
                except BrokenProcessPool:
                    log("memory watchdog broke the pool after a kill — completed cells are saved; "
                        "rebuilding to finish the rest.")
                finally:
                    stop_evt.set()
                    if watcher is not None:
                        watcher.join(timeout=2)
            # Restartable: recompute what still lacks output. Killed / interrupted cells reappear.
            remaining = [j for j in remaining
                         if not all(Path(p).exists() for p in j["outputs"].values())]
            if killed and cur_workers > 1:
                cur_workers = max(1, cur_workers // 2)     # relieve pressure on the retry
                log(f"memory watchdog: retrying {len(remaining)} cell(s) with {cur_workers} worker(s).")
            elif remaining and len(remaining) == before:
                # No progress this pass and we can't narrow further — one cell won't fit. Stop
                # rather than loop forever; mark the rest failed so the run reports honestly.
                log(f"memory guard: {len(remaining)} cell(s) can't fit even at {cur_workers} "
                    f"worker(s) — giving up on them (see --no-mem-guard to override).")
                for j in remaining:
                    primary = next(iter(j["outputs"].values()))
                    results_out.append({"output": str(primary), "status": "failed",
                                        "error": "MemoryError: cell exceeds RAM budget under the guard"})
                break

    # Accounting is EXISTENCE-based (a cell succeeded iff its outputs are on disk): a watchdog kill
    # can drop a completed future's result before we read it, but the data is what's true.
    guarded_jobs = jobs if not mem_guard else jobs[1:]
    errors = {r["output"]: r.get("error") for r in results_out if r["status"] == "failed"}
    ran, failed = 0, []
    checklist = list(guarded_jobs)
    if mem_guard:
        checklist = [jobs[0]] + checklist            # include the pilot cell
    for j in checklist:
        primary = str(next(iter(j["outputs"].values())))
        if all(Path(p).exists() for p in j["outputs"].values()):
            ran += 1
        else:
            failed.append({"output": primary,
                           "error": errors.get(primary, "no output (killed or errored)")})
    log(f"done: {ran} succeeded, {len(failed)} failed, {done_already} skipped (pre-existing).")
    for r in failed[:10]:
        log(f"  FAIL {Path(r['output']).name}: {r['error']}")

    status = {
        "experiment": eid, "cells_total": len(cells), "succeeded_this_run": ran,
        "failed_this_run": len(failed), "skipped_preexisting": done_already,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    status_path = results / "data" / eid / "_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status, indent=2))

    # ❌ only on a real failure; ✅ when every cell has data; 🟦 for a clean partial pass
    # (e.g. --limit) that isn't complete yet — a partial success must not read as failed.
    if failed:
        mark = "❌"
    elif (done_already + ran) == len(cells):
        mark = "✅"
    else:
        mark = "🟦"
    _update_progress(results, eid, mark)
    return 0 if not failed else 1
