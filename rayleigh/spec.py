"""Resolve the ACTIVE experiment spec.

The preregistered spec is `results/designdocs/experiments.yaml`. A `rayleigh review` may
write a REVISED spec as `experiments_2.yaml` (then `_3`, …) — a complete, standalone copy
with the human-approved changes merged, leaving the original as the preregistration of
record (the diff between them is the durable record of what the review changed).

The ACTIVE spec is the highest-numbered `experiments_<N>.yaml`, or `experiments.yaml` when
none exists. Every consumer — conduct_exp, process_outputs, queue, review — loads the active
spec, so a re-run queued off a review automatically picks up the revision without anyone
editing the canonical filename.
"""

import re
from pathlib import Path

# experiments.yaml (version 1) or experiments_<N>.yaml (version N).
_SPEC_RE = re.compile(r"^experiments(?:_(\d+))?\.yaml$")


def spec_version(path: Path) -> int:
    """Revision number encoded in a spec filename: experiments.yaml -> 1,
    experiments_<N>.yaml -> N, anything else -> 0 (not a spec file)."""
    m = _SPEC_RE.match(path.name)
    if not m:
        return 0
    return int(m.group(1)) if m.group(1) else 1


def active_spec_path(designdocs: Path) -> Path:
    """Highest-numbered experiments_<N>.yaml under `designdocs`, else experiments.yaml.

    Returns the base `experiments.yaml` path (which may not exist) when nothing matches, so
    callers keep their own "no spec — run init" error message."""
    base = designdocs / "experiments.yaml"
    candidates = [p for p in designdocs.glob("experiments*.yaml")
                  if _SPEC_RE.match(p.name) and p.is_file()]
    if not candidates:
        return base
    return max(candidates, key=spec_version)


def next_spec_path(designdocs: Path) -> Path:
    """The filename the NEXT revision should take: experiments_<active+1>.yaml.
    (A review that writes a revised in-cycle spec names it this.)"""
    n = spec_version(active_spec_path(designdocs))
    return designdocs / f"experiments_{max(n, 1) + 1}.yaml"
