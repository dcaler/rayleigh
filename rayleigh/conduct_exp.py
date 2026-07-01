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
import re
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml

from rayleigh import __version__

DEFAULT_WORKERS = 8
DEFAULT_TEMPLATE = "data/{experiment}/{cellkey}_seed{seed}.parquet"


def log(msg: str) -> None:
    print(f"[rayleigh conduct_exp] {msg}", flush=True)


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
    """Expand an experiment into cells: [{params, seed}, ...]."""
    design = exp.get("design") or {}
    combos = _param_combos(design)
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
    workers = getattr(args, "workers", 0) or exp.get("workers") or adapter.get("workers") or DEFAULT_WORKERS
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
        f"· kind={kind} · {nout} output(s)/cell · workers={workers}")

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
    if workers <= 1:
        results_out = [_execute_cell(j) for j in jobs]
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_execute_cell, j) for j in jobs]
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
