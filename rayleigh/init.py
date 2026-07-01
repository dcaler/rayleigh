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
import sys
from datetime import date
from importlib.resources import files
from pathlib import Path

import yaml

from rayleigh.config import Config, load_config

DESIGN_PROMPT = (
    "You are running the `rayleigh init` design session. Read results/designdocs/PLANNING.md "
    "and follow it: absorb the research brief (results/rayleigh.yaml `brief:`) and the project's "
    "materials (code/, and any paper/ and litReview/ one level up), read code/ to establish the "
    "run_adapter, then co-design this cycle's preregistered experiments with me — writing "
    "results/designdocs/experiments.yaml and EXPERIMENTS.md. Start by reading PLANNING.md."
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


def ask(prompt: str, default=None, preset=None) -> str:
    """Prompt unless a preset (CLI arg) is given. Non-interactive -> default."""
    if preset is not None:
        return preset
    suffix = f" [{default}]" if default not in (None, "") else ""
    if not sys.stdin.isatty():
        return "" if default is None else str(default)
    try:
        resp = input(f"  {prompt}{suffix}: ").strip()
    except EOFError:
        resp = ""
    return resp or ("" if default is None else str(default))


def ask_longform(prompt: str, preset=None) -> str:
    """Read a multi-line, free-form answer (the research brief). A preset short-circuits;
    non-interactive -> empty. Interactively, end with Ctrl-D on a blank line."""
    if preset is not None:
        return preset
    if not sys.stdin.isatty():
        return ""
    print(f"  {prompt}")
    print("  (write as much as you like — the more the design session has to work with, the")
    print("   better; finish with Ctrl-D on a blank line)")
    lines = []
    try:
        while True:
            lines.append(input())
    except EOFError:
        pass
    return "\n".join(lines).strip()


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


def launch_session(root: Path, no_launch: bool) -> int:
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
    print(f"[rayleigh init] launching an interactive Claude design session in {root} …")
    # Run from the project root so the session sees results/, code/, and the sibling
    # paper/ and litReview/. Inherits this terminal's stdio (fully interactive).
    return subprocess.run(["claude", DESIGN_PROMPT], cwd=str(root)).returncode


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

    name = ask("Project name",
               default=prior.get("project") or project_name_from_dir(root.name),
               preset=args.name)
    brief = ask_longform("What do you want to find out this cycle?", preset=args.brief).strip()
    if not brief:
        brief = (prior.get("brief") or "").strip()

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

    log(f"cycle {cycle} · package under test: {package} ({code_path})")
    log("done.")
    print()
    print(f"  Scaffolded results/ for {name} (cycle {cycle}) in {results}")
    # results/ is a working folder, not a repo — no git init here (unlike raster).
    return launch_session(root, getattr(args, "no_launch", False))
