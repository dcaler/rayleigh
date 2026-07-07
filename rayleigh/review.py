"""`rayleigh review` — the HUMAN-LED review gate that closes a cycle after process_outputs.

The pipeline is init -> conduct_exp -> process_outputs -> review. process_outputs produces the
report; `review` is the deliberate step where a HUMAN decides whether that report is actually the
analysis intended — and, if not, which layer diverged and what to do about it. Like `init`, it is
an interactive human+Claude session (Claude facilitates and lays out the evidence; the human
judges and records the verdict). It is not an auto-verdict: rayleigh reports, it does not conclude.

The record of the review is results/designdocs/REVIEW.md — the human's per-experiment verdict
(accept / fix-presentation / fix-analysis / re-conduct / re-init) and the next action.
"""

import shutil
import subprocess
from datetime import date
from pathlib import Path

import yaml

from rayleigh.config import load_config

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
    "Three hard rules: (1) HUMAN-LED — never record a verdict I did not give, and never sign off "
    "for me; (2) NEVER edit experiments.yaml or the cell data to improve a result — propose spec "
    "changes and let me decide; (3) never state an unverified claim as fact. "
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
    spec_path = results / "designdocs" / "experiments.yaml"
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
    return launch_review(root, getattr(args, "no_launch", False), model=cfg.design_model)
