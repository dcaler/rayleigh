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
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml

from rayleigh import __version__

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


# ---- memory guard: MEASURE one cell, then size the pool + cap each worker to fit RAM -------
# The design session's per-run RAM figure is an estimate; an estimate that's wrong by a few x
# and fanned out over a wide pool is exactly how a run OOMs the machine. So conduct_exp does not
# trust the estimate: it runs the first cell alone, measures its real peak footprint, sizes the
# pool from that vs. actually-available RAM, and puts a hard RLIMIT_AS ceiling under each worker
# so a runaway cell dies with MemoryError instead of taking the box into swap. This must hold on
# ANY machine/experiment — the guarantee is enforced here, not assumed from the spec.
_MEM_SAFETY = 0.75      # commit at most this fraction of *available* RAM to the whole pool
_HEADROOM = 1.5         # let a cell grow to this multiple of its measured pilot peak


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
                    machine_workers: int, resource_kind: str) -> tuple[int, int, str]:
    """From the measured pilot footprint, decide (workers, per-worker RLIMIT_AS bytes, report).
    workers is chosen so the whole pool's RSS fits the RAM budget; the AS ceiling is the OS-level
    backstop against a single cell running away. resource=gpu keeps the RSS sizing but sets no AS
    limit (CUDA reserves huge virtual ranges that a limit would false-trip)."""
    avail = _available_ram_gb()
    gpu = str(resource_kind).lower() == "gpu"
    if avail is None:                        # can't see RAM: don't pretend to guarantee anything
        return machine_workers, 0, (f"RAM unreadable — kept workers={machine_workers}, no cap "
                                    f"(watch this run for swap)")
    budget = avail * _MEM_SAFETY
    per_cell = max(peak_rss_gb, 0.01)
    by_mem = max(1, int(budget // (per_cell * _HEADROOM)))
    workers = max(1, _pow2_floor(min(machine_workers, by_mem)))
    ceiling_gb = max(peak_vsize_gb, peak_rss_gb) * _HEADROOM
    if workers == 1:
        ceiling_gb = max(ceiling_gb, budget)       # a lone big cell may use the whole budget
    as_bytes = 0 if gpu else int(ceiling_gb * (1024 ** 3))
    report = (f"pilot peak {peak_rss_gb:.1f} GiB RSS / {peak_vsize_gb:.1f} GiB virt · "
              f"{avail:.0f} GiB avail, budget {budget:.0f} GiB ({int(_MEM_SAFETY * 100)}%) → "
              f"workers {machine_workers}→{workers}")
    report += (" · resource=gpu, host-RAM sized, no address-space cap" if gpu
               else f" · per-worker cap {ceiling_gb:.1f} GiB (RLIMIT_AS)")
    if workers < machine_workers:
        report += "  [reduced to fit RAM]"
    return workers, as_bytes, report


def _limit_address_space(as_bytes: int) -> None:
    """ProcessPoolExecutor initializer: cap this worker's virtual memory. A cell that exceeds it
    gets MemoryError/ENOMEM (and any subprocess it spawns inherits the limit) instead of swapping
    the whole machine. No-op when unset or unsupported."""
    if not as_bytes or as_bytes <= 0:
        return
    try:
        import resource as R
        _soft, hard = R.getrlimit(R.RLIMIT_AS)
        R.setrlimit(R.RLIMIT_AS, (int(as_bytes), hard))
    except (ImportError, ValueError, OSError):
        pass


def add_import_paths(*dirs) -> None:
    """Make an `import`-kind adapter resolvable: put its candidate homes on sys.path.
    The run-adapter shim (and the output_adapter loader) may live in code/ OR results/, so
    both are searched. Shared by conduct_exp and process_outputs."""
    for d in dirs:
        if d and str(d) not in sys.path:
            sys.path.insert(0, str(d))


# --------------------------------------------------------------------- spec + cells
def _load_spec(results: Path) -> dict:
    spec_path = results / "designdocs" / "experiments.yaml"
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
    """The parameter side of the design (before seeds), as a list of param dicts."""
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
        f"design.kind '{kind}' is not yet supported by conduct_exp — use 'sweep' or "
        "'conditions' (oat/ablation are planned).")


def expand_cells(exp: dict, adapter: dict) -> list[dict]:
    """Expand an experiment into cells: [{params, seed}, ...].

    `fixed:` (a dict of constant params) is merged into every cell — the way to pin a
    parameter across a whole experiment that isn't a swept axis (e.g. an arm selector
    `mode: monkey` shared by every cell of a grid). A swept axis of the same name wins."""
    design = exp.get("design") or {}
    combos = _param_combos(design)
    fixed = exp.get("fixed") or {}
    if fixed and not isinstance(fixed, dict):
        raise ValueError(f"experiment {exp.get('id')}: `fixed` must be a mapping, got {fixed!r}")
    if fixed:
        combos = [{**fixed, **combo} for combo in combos]     # swept axis overrides a fixed key
    seeds = exp.get("seeds", design.get("seeds", 1))
    try:
        n_seeds = int(seeds)
    except (TypeError, ValueError):
        raise ValueError(f"experiment {exp.get('id')}: `seeds` must be an integer, got {seeds!r}")
    return [{"params": p, "seed": s} for p in combos for s in range(n_seeds)]


def resolve_cell_outputs(adapter: dict, results: Path, eid: str, cell: dict) -> dict:
    """Resolve a cell's output artifact path(s) → {name: Path}.

    Two forms in run_adapter (a run may write several files, e.g. a trajectory + a summary):
      - single: `output_template: "…"`                    -> {"output": Path}
      - multi:  `outputs: {timeseries: "…", summary: "…"}` -> {"timeseries": Path, "summary": Path}
    """
    outs = adapter.get("outputs")
    templates = dict(outs) if outs else {"output": adapter.get("output_template") or DEFAULT_TEMPLATE}
    key = _cellkey(cell["params"])
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
def _execute_cell(job: dict) -> dict:
    """Run one cell. Top-level so it is picklable for ProcessPoolExecutor.
    A cell may write several named artifacts (job["outputs"] = {name: path})."""
    outs = {n: Path(p) for n, p in job["outputs"].items()}
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
                   outputs={n: str(p) for n, p in outs.items()})
            else:                                # single output_template -> callable(params, seed, output)
                fn(params=job["params"], seed=job["seed"], output=str(primary))
        elif job["kind"] == "subprocess":
            cfg_path = Path(job["config_path"])
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(json.dumps({**job["params"], "seed": job["seed"]}, indent=2))
            subs = {"config": str(cfg_path), "out": str(primary), "seed": job["seed"],
                    **job["params"], **{f"out_{n}": str(p) for n, p in outs.items()}}
            cmd = job["command"].format(**subs)
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               cwd=job.get("cwd"))
            if r.returncode != 0:
                raise RuntimeError((r.stderr or r.stdout).strip()[:400] or f"exit {r.returncode}")
        else:
            raise ValueError(f"unknown run_adapter kind '{job['kind']}' (use import|subprocess)")

        missing = [str(p) for p in outs.values() if not p.exists()]
        if missing:
            raise RuntimeError(f"entrypoint returned but wrote no output at {', '.join(missing)}")
        _write_provenance(job, outs, primary)
        return {"output": str(primary), "status": "done", "error": None}
    except Exception as e:                       # noqa: BLE001 — report, don't crash the pool
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
    run_jobs = jobs

    # MEASURE before fanning out: run the first cell alone, size the pool from its real footprint,
    # and derive the per-worker RAM ceiling. This is what keeps a wide pool from OOM-ing the box.
    if mem_guard:
        peak_rss, peak_vsize, pilot_res = _measure_pilot(jobs[0])
        results_out.append(pilot_res)
        run_jobs = jobs[1:]
        workers, as_ceiling, size_report = _size_by_memory(
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
        # ceiling lands on a *worker*, never on rayleigh's own process.
        with ProcessPoolExecutor(max_workers=max(1, workers),
                                 initializer=_limit_address_space,
                                 initargs=(as_ceiling,)) as ex:
            futs = [ex.submit(_execute_cell, j) for j in run_jobs]
            for f in as_completed(futs):
                results_out.append(f.result())

    failed = [r for r in results_out if r["status"] == "failed"]
    ran = len(results_out) - len(failed)
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
