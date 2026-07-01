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
# reserved for later verbs; init uses an interactive `claude` session, not these.
design = "claude"            # interactive design/prereg session

[trundlr]
# reserved: the coarse experiment chain is deferred (local fan-out for now).
api_url = "http://100.87.86.57:8251"
"""


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "rayleigh" / "config.toml"


@dataclass
class Config:
    author_name: str = "rayleigh"
    tool_initials: str = "ra"
    user_initials: str = "DCR"
    design_model: str = "claude"
    trundlr_api: str = "http://100.87.86.57:8251"


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
    )
    cfg.trundlr_api = os.environ.get("RAYLEIGH_TRUNDLR_API", cfg.trundlr_api)
    return cfg
