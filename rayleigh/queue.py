"""`rayleigh queue` — linearize experiments.yaml into a trundlr chain and submit it.

For running at scale off your laptop. The chain is flat and single-parent (trundlr keys
`depends_on` to one task): one `rayleigh conduct_exp <E>` node per experiment, then a final
`rayleigh process_outputs` node. A failure breaks everything downstream (trundlr
dependency_broken). Each conduct node still fans its cells out locally (its own
ProcessPoolExecutor) on the machine trundlr assigns it — so the coarse experiment chain
rides trundlr while the cell fan-out stays local.

Local runs need none of this: `rayleigh conduct_exp` + `rayleigh process_outputs` on your
own machine cover small/medium experiments.
"""

import os
import re
from pathlib import Path

import yaml

from rayleigh import trundlr
from rayleigh.config import load_config

DEFAULT_CONDUCT_HOURS = 1.0
DEFAULT_PROCESS_HOURS = 0.5


def log(msg: str) -> None:
    print(f"[rayleigh queue] {msg}", flush=True)


def _cache_project_id(results: Path, pid: int) -> None:
    """Write the resolved numeric id back into results/rayleigh.yaml (text-replace so
    comments survive), so the next `rayleigh queue` is a direct submit with no name lookup."""
    ry = results / "rayleigh.yaml"
    if not ry.is_file():
        return
    text = ry.read_text()
    new = re.sub(r"(?m)^(\s*project_id:\s*)[^#\n]*?(\s*(?:#.*)?)$", rf"\g<1>{pid}\g<2>",
                 text, count=1)
    if new != text:
        ry.write_text(new)
        log(f"cached project id {pid} in results/rayleigh.yaml")


def linearize(spec: dict, exec_cmd: str, compute, cpu) -> list:
    """Flat single-parent chain: conduct_exp per experiment, then a final process_outputs."""
    chain = []
    for exp in spec.get("experiments", []) or []:
        eid = str(exp["id"])
        hours = float(exp.get("budget_hours", DEFAULT_CONDUCT_HOURS))
        chain.append({
            "id": eid,
            "title": f"rayleigh: conduct_exp {eid}",
            "description": exp.get("title", "") or f"conduct experiment {eid}",
            "command": f"{exec_cmd} conduct_exp {eid}",
            "resources": [compute],
            "duration": hours,
        })
    if chain:                          # only add the write-up if there is something to run
        chain.append({
            "id": "process_outputs",
            "title": "rayleigh: process_outputs",
            "description": "reduce cell data -> preregistered outputs + the .docx write-up",
            "command": f"{exec_cmd} process_outputs",
            "resources": [cpu],
            "duration": DEFAULT_PROCESS_HOURS,
        })
    return chain


def run_queue(args) -> int:
    root = Path(args.dir).resolve() if getattr(args, "dir", None) else Path.cwd()
    results = root / "results"
    spec_path = results / "designdocs" / "experiments.yaml"
    if not spec_path.is_file():
        log(f"no {spec_path} — run `rayleigh init` first")
        return 1
    spec = yaml.safe_load(spec_path.read_text()) or {}
    if not spec.get("experiments"):
        log("experiments.yaml has no experiments — author them in `rayleigh init` first")
        return 1

    meta = {}
    ry = results / "rayleigh.yaml"
    if ry.is_file():
        meta = yaml.safe_load(ry.read_text()) or {}
    tr = meta.get("trundlr", {}) or {}
    cfg = load_config()
    res = tr.get("resources", {}) or {}
    compute = res.get("compute", cfg.compute_resource)
    cpu = res.get("cpu", cfg.cpu_resource)
    api = tr.get("api_url") or cfg.trundlr_api

    exec_cmd = args.exec_cmd or os.environ.get("RAYLEIGH_EXEC_CMD", "rayleigh")
    chain = linearize(spec, exec_cmd, compute, cpu)

    if getattr(args, "dry_run", False):
        total = sum(c["duration"] for c in chain)
        log(f"{len(chain)} tasks (~{total:.2f}h), exec_cmd={exec_cmd!r}, api={api}:")
        for i, c in enumerate(chain):
            dep = chain[i - 1]["id"] if i else "—"
            resv = ",".join(map(str, c["resources"]))
            print(f"  {c['title']:34} res={resv:4}  {c['duration']:.2f}h  "
                  f"dep={dep:16} [{c['command']}]")
        return 0

    pid_raw = tr.get("project_id") or meta.get("project") or root.name
    if str(pid_raw).isdigit():
        pid = int(pid_raw)
    else:
        try:
            pid, created = trundlr.resolve_project_id(
                api, str(pid_raw), folder=str(root),
                description=str(spec.get("brief") or "")[:200] or None)
        except trundlr.TrundlrError as e:
            log(f"could not resolve trundlr project {pid_raw!r}: {e}")
            return 1
        log(f"{'created' if created else 'found'} trundlr project {pid_raw!r} -> id {pid}")
        _cache_project_id(results, pid)

    try:
        trundlr.set_project_directory(api, pid, str(root))
        log(f"set project {pid} directory -> {root}")
    except Exception as e:                       # noqa: BLE001
        log(f"warning: could not set project_directory: {e}")

    prev_id = None
    for c in chain:
        try:
            created = trundlr.create_task(api, {
                "title": c["title"],
                "description": c["description"],
                "command": c["command"],
                "project_id": pid,
                "resource_ids": c["resources"],
                "depends_on_id": prev_id,
                "duration": c["duration"],
                "status": "todo",
            })
        except trundlr.TrundlrError as e:
            where = f"after {prev_id}" if prev_id else "on the first task"
            log(f"FAILED creating {c['id']} ({where}): {e}")
            log(f"aborted — {'no' if not prev_id else 'a partial chain of'} tasks created; "
                "fix the cause and re-run.")
            return 1
        prev_id = created["id"]
        log(f"created #{prev_id:<4} {c['title']}")

    log(f"done — {len(chain)} tasks chained under project {pid}")
    return 0
