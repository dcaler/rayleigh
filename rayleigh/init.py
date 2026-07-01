"""`rayleigh init` — scaffold results/, open/roll a research cycle, and launch the
interactive design (preregistration) session.

Run from the PROJECT ROOT (the shared working dir, alongside code/, and any paper/ or
litReview/). rayleigh scaffolds and works entirely inside results/; the root and code/
are left alone. `init` merges what raster splits into `init` + `plan`: rayleigh's scaffold
is trivial (a working folder, not a pushed repo), so one verb both lays down results/ and
launches the Claude design session.
"""

import json
import re
import shutil
import subprocess
from datetime import date
from importlib.resources import files
from pathlib import Path

import yaml

from rayleigh.config import Config, load_config

DESIGN_PROMPT = (
    "You are running the `rayleigh init` design session — an interactive research-design "
    "conversation, not a form-filler. Read results/designdocs/PLANNING.md and follow it. First "
    "read results/designdocs/PRIORS.md — the index of what the earlier ra* tools left (the "
    "raster-built code/, the rabbitHole litReview/, the raconteur paper/) — and the artifacts it "
    "points to. Then run the INTAKE with me: the brief in results/rayleigh.yaml may be thin or "
    "empty, so (grounded in the priors) ask me what I want to find out this cycle and draw it out "
    "in discussion. From that + the priors, propose a starting experiment design, refine it with "
    "me, and write the finalized brief into results/rayleigh.yaml plus "
    "results/designdocs/experiments.yaml (incl. the run_adapter from code/) and EXPERIMENTS.md. "
    "Start by reading PLANNING.md and PRIORS.md, then talk to me."
)


def log(msg: str) -> None:
    print(f"[rayleigh init] {msg}", flush=True)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower()) or "package"


def project_name_from_dir(dirname: str) -> str:
    """Guess a project name the way the ra* family does: strip a leading {YYMMDD}_ (or
    {YYYYMMDD}_) datestamp prefix. e.g. '260623_rayleigh' -> 'rayleigh'."""
    return re.sub(r"^\d{6}(?:\d\d)?_", "", dirname) or dirname


def detect_package(code_dir: Path, fallback: str) -> str:
    """Find the import package under code/: a child dir with an __init__.py. Prefer one
    matching the slug fallback; else the first; else the fallback slug."""
    if not code_dir.is_dir():
        return fallback
    pkgs = sorted(p.name for p in code_dir.iterdir()
                  if p.is_dir() and (p / "__init__.py").is_file()
                  and not p.name.startswith((".", "_")) and p.name != "tests")
    if fallback in pkgs:
        return fallback
    return pkgs[0] if pkgs else fallback


def render(template_name: str, ctx: dict) -> str:
    text = (files("rayleigh") / "templates" / template_name).read_text()
    for key, val in ctx.items():
        text = text.replace("{{" + key + "}}", str(val))
    return text


def archive_cycle(results: Path, prior_cycle: str) -> None:
    """--new-cycle: move the prior cycle's designdocs/ and data/ into archive/<cycle>/."""
    dest = results / "archive" / (prior_cycle or "unknown")
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("designdocs", "data"):
        src = results / name
        if src.is_dir() and any(src.iterdir()):
            target = dest / name
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(src), str(target))
            log(f"archived {name}/ -> {target.relative_to(results.parent)}")


# ----------------------------------------------- prior ra* artifacts (design seed)
# By the time rayleigh runs, earlier ra* tools have usually left rich context in the
# project. `init` indexes it so the design session can PROPOSE a starting experiment set
# instead of a blank skeleton. (group -> [(glob, what-it-gives-you)])
PRIOR_SOURCES = [
    ("Model / codebase (raster)", [
        ("code/raster.yaml", "build config — the project brief + package"),
        ("code/README.md", "what the codebase is"),
        ("code/designdocs/DESIGN.md", "the model's design + architecture"),
        ("code/designdocs/tasks.yaml", "the build spec — modules, parameters, interfaces"),
        ("code/designdocs/PROGRESS.md", "what was actually built"),
        ("code/planningDocs/*.md", "planning/build notes (IMPLEMENTATION_PLAN, build_log, …)"),
        ("code/configs/**/*.yaml", "parameter configs — candidate sweep axes + baselines"),
        ("code/**/CLAUDE.md", "codebase agent notes (invariants, known limits)"),
    ]),
    ("Literature (rabbitHole)", [
        ("litReview/*.yaml", "review config — topics + snowball seeds"),
        ("litReview/*.docx", "the literature review — expected directions, prior findings"),
        ("litReview/output/*.docx", "review outputs"),
    ]),
    ("Paper (raconteur)", [
        ("paper/*.md", "paper draft / venue analysis — which questions matter"),
        ("paper/*.yaml", "outline / venue config"),
    ]),
]


def discover_priors(root: Path):
    """Find prior ra* artifacts. Returns [(group, [(label, [relpaths]), ...]), ...],
    groups with no matches omitted."""
    out = []
    for group, patterns in PRIOR_SOURCES:
        items = []
        for pattern, label in patterns:
            matches = sorted(str(p.relative_to(root)) for p in root.glob(pattern)
                             if p.is_file())
            if matches:
                items.append((label, matches))
        if items:
            out.append((group, items))
    return out


def _derive_brief(root: Path) -> str:
    """Fall back to the raster build brief/description when no rayleigh brief is given —
    it's the closest statement of research intent already on disk."""
    ry = root / "code" / "raster.yaml"
    if not ry.is_file():
        return ""
    try:
        d = yaml.safe_load(ry.read_text()) or {}
    except Exception:
        return ""
    for k in ("brief", "description"):
        v = d.get(k)
        if isinstance(v, str) and v.strip() and "not provided" not in v and "to be generated" not in v:
            return v.strip()
    return ""


def render_priors_md(root: Path, priors, project: str, cycle: str) -> str:
    L = [f"# {project} — Prior artifacts (cycle {cycle})", "",
         "*Index written by `rayleigh init`. The earlier ra* tools (raster, rabbitHole,",
         "raconteur) left these in the project. Read them and PROPOSE a starting experimental",
         "design from them — see PLANNING.md — rather than starting from a blank skeleton.*", ""]
    if not priors:
        L.append("_No prior ra* artifacts found — design from the brief alone._")
        return "\n".join(L) + "\n"
    for group, items in priors:
        L.append(f"## {group}")
        for label, matches in items:
            shown = matches[:6]
            more = f"  (+{len(matches) - 6} more)" if len(matches) > 6 else ""
            if len(shown) == 1:
                L.append(f"- **{label}** — `{shown[0]}`")
            else:
                L.append(f"- **{label}** — {', '.join(f'`{m}`' for m in shown)}{more}")
        L.append("")
    return "\n".join(L) + "\n"


def launch_session(root: Path, no_launch: bool, model: str = "") -> int:
    playbook = root / "results" / "designdocs" / "PLANNING.md"

    def manual(reason: str) -> int:
        print(reason)
        print(f"  {playbook}")
        print("It reads the brief + code/ + project materials, then co-designs")
        print("experiments.yaml and EXPERIMENTS.md with you interactively.")
        return 0

    if no_launch:
        return manual("Open a Claude session in this folder and follow:")
    if shutil.which("claude") is None:
        return manual("`claude` is not on PATH — open a session yourself and follow:")
    # The design session is the strong-reasoning step; launch it on the configured model
    # (default Opus) rather than inheriting the CLI default. ("claude" is not a valid
    # --model alias — an older config default — so treat it as "use the CLI default".)
    use_model = model if model and model.lower() not in ("claude", "default") else ""
    cmd = ["claude"] + (["--model", use_model] if use_model else []) + [DESIGN_PROMPT]
    model = use_model
    print(f"[rayleigh init] launching an interactive Claude design session "
          f"({model or 'default'}) in {root} …")
    # Run from the project root so the session sees results/, code/, and the sibling
    # paper/ and litReview/. Inherits this terminal's stdio (fully interactive).
    return subprocess.run(cmd, cwd=str(root)).returncode


def run_init(args) -> int:
    cfg = load_config()
    root = Path(args.dir).resolve() if args.dir else Path.cwd()
    results = root / "results"
    designdocs = results / "designdocs"
    existing = results / "rayleigh.yaml"

    prior = {}
    if existing.is_file():
        try:
            prior = yaml.safe_load(existing.read_text()) or {}
        except Exception:
            prior = {}

    log(f"project root: {root}")
    today = date.today().strftime("%y%m%d")
    prior_cycle = str(prior.get("cycle") or "")

    if getattr(args, "new_cycle", False) and results.exists():
        archive_cycle(results, prior_cycle)
        cycle = today
        prior = {}                       # fresh cycle: don't inherit the archived spec
    else:
        cycle = prior_cycle or today

    # No interactive prompting here — the intake ("what do you want to find out?") is the
    # intellectual work, so the launched Claude session does it, grounded in the priors it
    # reads. init only resolves deterministic defaults; the session refines them with the user.
    name = (args.name or prior.get("project") or project_name_from_dir(root.name)).strip()
    brief = (args.brief or prior.get("brief") or _derive_brief(root) or "").strip()
    if brief:
        log(f"starting brief: {brief[:70]}{'…' if len(brief) > 70 else ''}")
    else:
        log("no brief yet — the design session will elicit it from you + the priors")

    code_dir = root / "code"
    package = detect_package(code_dir, slugify(name))
    code_path = (prior.get("code", {}) or {}).get("path") or "../code"

    ctx = {
        "PROJECT": name,
        "PACKAGE": package,
        "CYCLE": cycle,
        "CODE_PATH": code_path,
        "BRIEF": brief or "(not provided at init — clarify with the user during the session)",
        "BRIEF_YAML": json.dumps(brief or "(not provided at init)"),
        "AUTHOR": cfg.author_name,
        "TOOL_INITIALS": cfg.tool_initials,
        "USER_INITIALS": cfg.user_initials,
        "TRUNDLR_API": cfg.trundlr_api,
        "GPU_RES": cfg.gpu_resource,
        "CPU": cfg.cpu_resource,
        "DATE": date.today().isoformat(),
    }

    # ---- scaffold results/ (idempotent; never clobber authored design docs) ----
    designdocs.mkdir(parents=True, exist_ok=True)
    (results / "data").mkdir(exist_ok=True)
    (results / "figures").mkdir(exist_ok=True)

    def write(path: Path, template: str, protect: bool = False):
        if protect and path.exists() and path.read_text().strip():
            log(f"kept existing {path.relative_to(root)} (not overwritten)")
            return
        path.write_text(render(template, ctx))
        log(f"wrote {path.relative_to(root)}")

    write(results / "rayleigh.yaml", "rayleigh.yaml.tmpl")
    write(results / ".gitignore", "gitignore.tmpl")
    write(designdocs / "PLANNING.md", "PLANNING.md.tmpl")           # refreshed each run
    write(designdocs / "EXPERIMENTS.md", "EXPERIMENTS.md.tmpl", protect=True)
    write(designdocs / "experiments.yaml", "experiments.yaml.tmpl", protect=True)
    write(designdocs / "PROGRESS.md", "PROGRESS.md.tmpl", protect=True)

    # Index the prior ra* artifacts so the design session can propose from them (refreshed each run).
    priors = discover_priors(root)
    (designdocs / "PRIORS.md").write_text(render_priors_md(root, priors, name, cycle))
    n_priors = sum(len(matches) for _, items in priors for _, matches in items)
    log(f"wrote results/designdocs/PRIORS.md ({n_priors} prior artifact(s) indexed)")

    log(f"cycle {cycle} · package under test: {package} ({code_path})")
    log("done.")
    print()
    print(f"  Scaffolded results/ for {name} (cycle {cycle}) in {results}")
    # results/ is a working folder, not a repo — no git init here (unlike raster).
    return launch_session(root, getattr(args, "no_launch", False), model=cfg.design_model)
