"""Machine-level config for rayleigh — ~/.config/rayleigh/config.toml.

This is the PII boundary: personal/account details live here and never travel into
a project's committed files. Created with sensible defaults on first run.

rayleigh is light on machine config: `init` launches an interactive `claude` session
(no model config needed), and `conduct_exp` calls code/ directly. What lives here is
the author identity stamped into the .docx write-up plus the initials used by the
document-revision naming chain (tool = `ra`, human reviewer = e.g. `DCR`). Model /
trundlr fields are placeholders reserved for the later verbs.
"""

import os
from dataclasses import dataclass
from pathlib import Path

try:                                # stdlib on Python 3.11+
    import tomllib
except ModuleNotFoundError:         # 3.10 and older
    try:
        import tomli as tomllib    # type: ignore
    except ModuleNotFoundError:
        tomllib = None             # fall back to the tiny parser below


def _loads_toml(text: str) -> dict:
    """Parse rayleigh's own simple config.toml when no TOML lib is available.
    Handles `[section]`, `key = "str" | int | true/false`, comments, and inline
    comments — sufficient for the flat schema rayleigh writes (not general TOML)."""
    if tomllib is not None:
        return tomllib.loads(text)
    data: dict = {}
    section = data
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = data.setdefault(line[1:-1].strip(), {})
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip()
        if val and val[0] in "\"'":          # quoted string (ignore trailing comment)
            q = val[0]
            val = val[1:val.index(q, 1)]
        else:
            val = val.split("#", 1)[0].strip()
            if val.lower() in ("true", "false"):
                val = val.lower() == "true"
            else:
                try:
                    val = int(val)
                except ValueError:
                    pass
        section[key.strip()] = val
    return data


DEFAULT_CONFIG_TOML = """\
# rayleigh machine config. Personal details stay here — never committed into a project.

[author]
name          = "rayleigh"   # stamped into the .docx write-up metadata
tool_initials = "ra"         # trailing suffix on tool-authored files (revision chain)
user_initials = "DCR"        # the human reviewer's initials (the annotated-file suffix)

[models]
# The `rayleigh init` design session is the highest-reasoning, human-in-the-loop step
# (synthesize priors -> a preregistered design + a run-adapter shim), so it launches
# `claude` on the strong model. `opus` = latest Opus (Opus 4.8); use `fable` for the very
# hardest, or `sonnet` for a lighter/cheaper session. Passed as `claude --model <this>`.
design = "opus"

[trundlr]
# `rayleigh queue` offloads the coarse experiment chain here (each conduct_exp node still
# fans its cells out locally on its assigned machine). `init` picks conduct_exp's resource
# per project: CPU-bound sims -> cpu_resource; GPU-accelerated models -> gpu_resource.
api_url      = "http://100.87.86.57:8251"
gpu_resource = 2             # conduct_exp for GPU-accelerated models
cpu_resource = 3             # conduct_exp for CPU-bound sims, and always process_outputs
# human_resource = 1         # OPTIONAL — YOUR trundlr resource id. When `rayleigh review`
                             # queues the follow-on chain (conduct_exp/process_outputs), it
                             # ends with a command-less `review` task on this resource so the
                             # next review lands in your queue. Unset -> the chain still
                             # queues; rayleigh just prints "re-run review when it completes".
"""


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "rayleigh" / "config.toml"


@dataclass
class Config:
    author_name: str = "rayleigh"
    tool_initials: str = "ra"
    user_initials: str = "DCR"
    design_model: str = "opus"
    trundlr_api: str = "http://100.87.86.57:8251"
    gpu_resource: int = 2
    cpu_resource: int = 3
    human_resource: int = 0          # 0 = unset (no human review-gate task queued)


def load_config(create: bool = True) -> Config:
    """Load machine config, writing defaults on first run. Env vars override:
    RAYLEIGH_TRUNDLR_API."""
    p = config_path()
    if not p.exists():
        if create:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(DEFAULT_CONFIG_TOML)
        data = {}
    else:
        data = _loads_toml(p.read_text())

    a = data.get("author", {})
    m = data.get("models", {})
    t = data.get("trundlr", {})
    cfg = Config(
        author_name=a.get("name", Config.author_name),
        tool_initials=a.get("tool_initials", Config.tool_initials),
        user_initials=a.get("user_initials", Config.user_initials),
        design_model=m.get("design", Config.design_model),
        trundlr_api=t.get("api_url", Config.trundlr_api),
        gpu_resource=int(t.get("gpu_resource", Config.gpu_resource)),
        cpu_resource=int(t.get("cpu_resource", Config.cpu_resource)),
        human_resource=int(t.get("human_resource", Config.human_resource) or 0),
    )
    cfg.trundlr_api = os.environ.get("RAYLEIGH_TRUNDLR_API", cfg.trundlr_api)
    return cfg
