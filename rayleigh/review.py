"""`rayleigh review` — the HUMAN-LED review gate that closes a cycle after process_outputs.

The pipeline is init -> conduct_exp -> process_outputs -> review. process_outputs produces the
report; `review` is the deliberate step where a HUMAN decides whether that report is actually the
analysis intended — and, if not, which layer diverged and what to do about it. Like `init`, it is
an interactive human+Claude session (Claude facilitates and lays out the evidence; the human
judges and records the verdict). It is not an auto-verdict: rayleigh reports, it does not conclude.

The record of the review is results/designdocs/REVIEW.md — the human's per-experiment verdict
(accept / fix-presentation / fix-analysis / re-conduct / re-init) and the next action.

FOLD-FORWARD (parseNplan-style). Deciding is the human's job; carrying the decision out is
rayleigh's. When the interactive session ends, `review` reads the verdicts back out of REVIEW.md
and QUEUES the follow-on work into trundlr as a dependency chain — exactly like rabbitHole's
parseNplan queues gather/collect/revise/comment. Each verdict maps to a pipeline of rayleigh
verbs; runner steps (conduct_exp, process_outputs) carry a `rayleigh <verb>` command the trundlr
runner executes when its dependency clears; the terminal `review` is a command-less HUMAN gate that
waits in your queue. So the human re-does nothing — the runner re-runs the work and you re-enter
only at the next review. The revised spec the chain runs against is the review's experiments_<N>.yaml
(the active spec), so the re-run automatically picks up the human-approved changes.
"""

import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import yaml

from rayleigh import trundlr
from rayleigh.config import load_config
from rayleigh.spec import active_spec_path, spec_version

# The review checklist — the layers to walk, outermost (cheapest to fix) first. Kept in sync with
# what the session puts in front of the human; also written into the REVIEW.md scaffold.
CHECKLIST = [
    "Measuring the intended thing? (metric name + reduce, and figure axes, vs the question)",
    "Covered the declared grid? (coverage; any missing cells or empty axis levels)",
    "Numbers alive & plausible? (metric ranges; anything constant / saturated / all-NaN; "
    "is the optimum where the mechanism says it should be)",
    "Raw spot-check: open a cell where you have a strong prior — does the stored value match?",
    "Tool mis-presented (a fix) vs. reality disagrees with the hypothesis (a real finding)?",
]

REVIEW_PROMPT = (
    "You are running the `rayleigh review` session — a HUMAN-LED review of the results the human "
    "just produced with `rayleigh process_outputs`. You FACILITATE; the human judges and decides. "
    "Do not rubber-stamp, and do not adjudicate the results yourself — rayleigh reports, it does "
    "not conclude. "
    "START NARROW: read ONLY the results document first — results/RESULTS.md (or, if you prefer the "
    "formatted version, the results/*_results_*.docx). That report is the spine of the review; it "
    "already carries each experiment's question, preregistered metric, and expected_direction, so it "
    "is enough to begin. Do NOT bulk-load everything else up front. Pull in the deeper artifacts "
    "ONLY when a specific check in front of me needs them, one experiment at a time: "
    "results/designdocs/experiments.yaml for the EXACT preregistered spec, results/findings.json for "
    "a computed value, results/figures|tables/ for a plot under discussion, and the raw cell data "
    "under results/data/<E>/ for a spot-check. Read the minimum that answers the question at hand. "
    "Then walk me, experiment by experiment, through results/designdocs/REVIEW.md — for each one "
    "put concrete evidence in front of me and let ME reach the verdict: (1) is it measuring the "
    "intended thing (metric + reduce, figure axes vs the question)? (2) did it cover the declared "
    "grid (coverage; missing cells or whole axis levels with no data)? (3) are the numbers alive "
    "and plausible (metric ranges; anything constant, saturated at a bound, or all-NaN; is the "
    "optimum where the mechanism says it should be)? (4) spot-check a raw cell where I have a "
    "strong prior — does the stored value match? "
    "Crucially, help me tell TWO different things apart: the tool MIS-PRESENTED the data (a fix — "
    "wrong output/metric -> re-run process_outputs; wrong design/data -> re-run conduct_exp) versus "
    "REALITY DISAGREES WITH MY HYPOTHESIS (a real finding, NOT a bug — we do not rewrite the "
    "preregistration or touch the data to make a result look better). "
    "Record MY calls into results/designdocs/REVIEW.md: per experiment a verdict (accept / "
    "fix-presentation / fix-analysis / re-conduct / re-init), the reason, and the concrete next "
    "action. "
    "Four hard rules: (1) HUMAN-LED — never record a verdict I did not give, and never sign off "
    "for me; (2) NEVER edit experiments.yaml or the cell data to improve a result — propose spec "
    "changes and let me decide; (3) REVIEW REPORTS, IT DOES NOT PRODUCE ARTIFACTS — you may READ a "
    "raw cell to check a value (ephemeral, read-only), but do NOT write any script, figure, table, "
    "or fix into results/. Regenerating analytical products is `process_outputs`' job (it runs the "
    "R engine); changing the design or data is `conduct_exp`/`init`'s — the verdict I give routes "
    "to the right verb, which regenerates them reproducibly. Doing the fix here would be the next "
    "step's work, done tool-led and unreproducibly. (4) never state an unverified claim as fact. "
    "Start by reading only RESULTS.md, then walk me through REVIEW.md — reaching for experiments.yaml, "
    "findings.json, the figures, or the cell data only as each check calls for it."
)


def log(msg: str) -> None:
    print(f"[rayleigh review] {msg}", flush=True)


def _scaffold_review_md(results: Path, spec: dict, cycle: str, project: str) -> Path:
    """Create results/designdocs/REVIEW.md if absent — the concrete artifact the human+Claude
    session fills in. Never clobbers an existing review (it is the human's record)."""
    p = results / "designdocs" / "REVIEW.md"
    if p.exists():
        return p
    L = [f"# {project} — Results review (cycle {cycle})", "",
         "*Human-led review of the process_outputs report. rayleigh lays out the evidence; you "
         "record the verdict and the next action. Filled in during `rayleigh review`.*", "",
         "## Checklist (walk each experiment, outermost layer first)"]
    L += [f"- [ ] {c}" for c in CHECKLIST]
    L.append("")
    for e in spec.get("experiments") or []:
        L += [f"## {e.get('id')} — {e.get('title', '')}", "",
              "**Evidence reviewed:** _(coverage · metric health · raw spot-check — fill during review)_",
              "",
              "**Verdict:** _accept / fix-presentation / fix-analysis / re-conduct / re-init_", "",
              "**Reason:**", "",
              "**Next action:**", ""]
    p.write_text("\n".join(L) + "\n")
    log(f"scaffolded {p.relative_to(results.parent)}")
    return p


def _has_report(results: Path) -> bool:
    """review only makes sense once process_outputs has produced a report."""
    return (results / "RESULTS.md").exists() or bool(list(results.glob("*_results_*.docx")))


# ── fold-forward: verdicts -> a queued trundlr chain (parseNplan-style) ──────────────
VERDICTS = ("accept", "fix-presentation", "fix-analysis", "re-conduct", "re-init")
# Which verdicts need the report regenerated (a `process_outputs` node in the chain).
_NEEDS_PROCESS = {"fix-presentation", "fix-analysis", "re-conduct"}


def parse_verdicts(review_md: Path, eids) -> dict:
    """Read REVIEW.md -> {experiment_id: verdict|None}. Under each `## <EID> — …` heading,
    take the first canonical `**Verdict:** <token>` line. An unfilled scaffold placeholder
    (it still holds ' / ' between the options) reads as None."""
    eidset = {str(e) for e in eids}
    out = {e: None for e in eidset}
    text = review_md.read_text() if review_md.is_file() else ""
    head = re.compile(r"^##\s+([A-Za-z0-9_.\-]+)\b")
    vline = re.compile(r"^\*\*Verdict:\*\*\s*(.+?)\s*$")
    cur = None
    for line in text.splitlines():
        m = head.match(line)
        if m:
            cur = m.group(1) if m.group(1) in eidset else None
            continue
        if cur and out.get(cur) is None:
            vm = vline.match(line)
            if vm:
                raw = vm.group(1).strip()
                if "/" in raw:                       # still the scaffold placeholder
                    continue
                bare = raw.strip("_*`. ")
                token = bare.lower().split()[0] if bare else ""
                if token in VERDICTS:
                    out[cur] = token
    return out


def plan_chain(verdicts: dict, spec: dict, exec_cmd: str, resources: dict,
               human_resource: int) -> list:
    """Flat single-parent trundlr chain from the verdicts (trundlr keys depends_on to one
    task): a `conduct_exp <E>` node per re-conduct experiment, then one `process_outputs`,
    then a command-less `review` human gate. accept/re-init contribute no nodes."""
    exps = {str(e.get("id")): e for e in spec.get("experiments") or []}
    reconduct = sorted(e for e, v in verdicts.items() if v == "re-conduct")
    needs_process = any(v in _NEEDS_PROCESS for v in verdicts.values())
    cpu = resources["cpu"]
    chain = []
    for eid in reconduct:
        exp = exps.get(eid, {})
        kind = str(exp.get("resource", "cpu")).lower()
        kind = kind if kind in resources else "cpu"
        chain.append({
            "id": f"conduct_exp {eid}", "title": f"rayleigh: conduct_exp {eid}",
            "description": f"re-conduct {eid} against the revised spec",
            "command": f"{exec_cmd} conduct_exp {eid}",
            "resources": [resources[kind]], "resource_kind": kind,
            "duration": float(exp.get("budget_hours", 1.0)), "human": False})
    if needs_process:
        chain.append({
            "id": "process_outputs", "title": "rayleigh: process_outputs",
            "description": "regenerate the report from the revised spec",
            "command": f"{exec_cmd} process_outputs",
            "resources": [cpu], "resource_kind": "cpu", "duration": 0.5, "human": False})
        if human_resource:
            chain.append({
                "id": "review", "title": "rayleigh: review",
                "description": "human gate — review the regenerated report",
                "command": None, "resources": [human_resource], "resource_kind": "human",
                "duration": 0.25, "human": True})
    return chain


def _print_plan(chain: list) -> None:
    total = sum(c["duration"] for c in chain)
    log(f"fold-forward chain — {len(chain)} task(s), ~{total:.2f}h:")
    for i, c in enumerate(chain):
        dep = chain[i - 1]["title"] if i else "—"
        who = "you" if c.get("human") else "runner"
        cmd = c["command"] or "(no command — waits for you)"
        print(f"  {c['title']:30} [{who:6}] {c['resource_kind']:6} {c['duration']:.2f}h  "
              f"dep={dep:24} {cmd}")


def _submit_chain(api: str, pid_raw, root: Path, brief: str, chain: list) -> int:
    """Submit the chain to trundlr as a dependency chain (mirrors `rayleigh queue`)."""
    if str(pid_raw).isdigit():
        pid = int(pid_raw)
    else:
        try:
            pid, created = trundlr.resolve_project_id(
                api, str(pid_raw), folder=str(root), description=(brief or "")[:200] or None)
        except trundlr.TrundlrError as e:
            log(f"could not resolve trundlr project {pid_raw!r}: {e}")
            return 1
        log(f"{'created' if created else 'found'} trundlr project {pid_raw!r} -> id {pid}")
    try:
        trundlr.set_project_directory(api, pid, str(root))
    except Exception as e:                            # noqa: BLE001
        log(f"warning: could not set project_directory: {e}")
    prev_id = None
    for c in chain:
        body = {"title": c["title"], "description": c["description"], "project_id": pid,
                "resource_ids": c["resources"], "depends_on_id": prev_id,
                "duration": c["duration"], "status": "todo"}
        if c["command"]:                              # human gate carries no command
            body["command"] = c["command"]
        try:
            created = trundlr.create_task(api, body)
        except trundlr.TrundlrError as e:
            log(f"FAILED creating {c['id']}: {e} — aborted (partial chain may exist).")
            return 1
        prev_id = created["id"]
        tag = "you" if c.get("human") else "runner"
        log(f"created #{prev_id:<4} {c['title']} [{tag}]"
            + (f" dep #{body['depends_on_id']}" if body["depends_on_id"] else ""))
    log(f"done — queued {len(chain)} task(s) under project {pid}")
    return 0


def fold_forward(root: Path, cfg, args) -> int:
    """After the review session, read the verdicts and queue the follow-on chain."""
    results = root / "results"
    designdocs = results / "designdocs"
    review_md = designdocs / "REVIEW.md"
    spec_path = active_spec_path(designdocs)          # picks up the review's experiments_<N>.yaml
    spec = yaml.safe_load(spec_path.read_text()) if spec_path.is_file() else {}
    eids = [str(e.get("id")) for e in spec.get("experiments") or []]
    verdicts = parse_verdicts(review_md, eids)

    log(f"active spec: {spec_path.name} (v{spec_version(spec_path)})")
    for eid in eids:
        log(f"  {eid}: {verdicts.get(eid) or '— (no verdict recorded)'}")

    missing = [e for e in eids if verdicts.get(e) is None]
    if missing:
        log(f"verdict not recorded for {', '.join(missing)} — finish REVIEW.md, then re-run "
            "`rayleigh review` to queue the follow-on. Nothing queued.")
        return 0
    if any(v == "re-init" for v in verdicts.values()):
        reinit = [e for e, v in verdicts.items() if v == "re-init"]
        log(f"re-init verdict on {', '.join(sorted(reinit))} — that is a new cycle, not an "
            "in-cycle revision. Run `rayleigh init --new-cycle` (it ingests REVIEW.md). "
            "Not mixing it into the in-cycle chain.")
        # fall through: any non-re-init actionable verdicts still get queued below.

    api, resources, pid_raw = _trundlr_meta(results, root, cfg)
    chain = plan_chain(verdicts, spec, args.exec_cmd or "rayleigh", resources, cfg.human_resource)
    if not chain:
        if all(v == "accept" for v in verdicts.values()):
            log("all experiments accepted — cycle closes. The report is the deliverable.")
        return 0
    if cfg.human_resource == 0:
        log("(no [trundlr] human_resource set — no review gate queued; re-run `rayleigh review` "
            "once the chain completes.)")
    _print_plan(chain)
    if getattr(args, "no_queue", False):
        log("--no-queue: plan only, nothing submitted.")
        return 0
    if not getattr(args, "yes", False):
        try:
            ans = input("[rayleigh review] submit this chain to trundlr? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            log("not submitted.")
            return 0
    return _submit_chain(api, pid_raw, root, str(spec.get("brief") or ""), chain)


def _trundlr_meta(results: Path, root: Path, cfg):
    """Resolve trundlr api/resources/project from results/rayleigh.yaml, cfg fallbacks."""
    meta = {}
    ry = results / "rayleigh.yaml"
    if ry.is_file():
        meta = yaml.safe_load(ry.read_text()) or {}
    tr = meta.get("trundlr", {}) or {}
    res = tr.get("resources", {}) or {}
    resources = {"gpu": res.get("gpu", cfg.gpu_resource), "cpu": res.get("cpu", cfg.cpu_resource)}
    api = tr.get("api_url") or cfg.trundlr_api
    pid_raw = tr.get("project_id") or meta.get("project") or root.name
    return api, resources, pid_raw


def launch_review(root: Path, no_launch: bool, model: str = "") -> int:
    results = root / "results"
    review_md = results / "designdocs" / "REVIEW.md"

    def manual(reason: str) -> int:
        print(reason)
        print(f"  {review_md}")
        print("Work through the checklist against the report (RESULTS.md / the .docx / findings.json),")
        print("spot-check the cell data, and record your verdict + next action per experiment.")
        return 0

    if no_launch:
        return manual("Open a Claude session in this folder and run the review:")
    if shutil.which("claude") is None:
        return manual("`claude` is not on PATH — review manually:")
    # The review is a strong-reasoning, human-in-the-loop step — launch on the configured design
    # model (default Opus), same as `init`. ("claude"/"default" -> inherit the CLI default.)
    use_model = model if model and model.lower() not in ("claude", "default") else ""
    cmd = ["claude"] + (["--model", use_model] if use_model else []) + [REVIEW_PROMPT]
    print(f"[rayleigh review] launching an interactive Claude review session "
          f"({use_model or 'default'}) in {root} …")
    # Run from the project root so the session sees results/ (report + data) and code/.
    return subprocess.run(cmd, cwd=str(root)).returncode


def run_review(args) -> int:
    cfg = load_config()
    root = Path(args.dir).resolve() if getattr(args, "dir", None) else Path.cwd()
    results = root / "results"
    spec_path = active_spec_path(results / "designdocs")
    if not spec_path.is_file():
        log(f"no {spec_path} — run `rayleigh init` (then conduct_exp + process_outputs) first")
        return 1
    if not _has_report(results):
        log("no report yet — run `rayleigh process_outputs` first, then review it.")
        return 1
    spec = yaml.safe_load(spec_path.read_text()) or {}
    cycle = str(spec.get("cycle") or date.today().strftime("%y%m%d"))
    project = str(spec.get("project") or "project")
    (results / "designdocs").mkdir(parents=True, exist_ok=True)
    _scaffold_review_md(results, spec, cycle, project)
    rc = launch_review(root, getattr(args, "no_launch", False), model=cfg.design_model)
    # After the human's review session, read the verdicts back and queue the follow-on work.
    fold_forward(root, cfg, args)
    return rc
