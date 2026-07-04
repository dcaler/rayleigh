"""`rayleigh process_outputs` — reduce cell data into an experiment's preregistered
outputs, findings, and the datestamped .docx write-up.

For each experiment: re-expand its cells (the same expansion conduct_exp ran), load each
cell's output into a tidy table, aggregate over seeds, render the outputs planned during
`init` (figures via matplotlib, tables via pivot), and state an honest finding — the
observed summary next to the preregistered `expected_direction`, honoring
`mode: confirmatory | exploratory`. No auto-verdict gate; honesty lives in the wording.

Reading a cell's output(s) uses a loader (a cell may write several artifacts):
  - default: each artifact as a JSON dict of scalars, or parquet/csv numeric column means,
    merged (keys prefixed by artifact name when there are several);
  - override: `code.output_adapter.load: "module:callable"` —
    callable(outputs: dict[name, path]) -> {metric: value}.

Deliverables, two audiences:
  - for raconteur (its load_results ingests results/ *.md / *.json / *.csv): `RESULTS.md`
    (prose), `findings.json` (structured per-experiment: prereg + observed finding + artifact
    pointers), and `tables/*.csv` (the aggregated numbers). This is rayleigh's reason to exist.
  - for the human: `results/{cycle}_{project}_results_{ra}.docx` (the ra/DCR review cycle;
    python-docx, degrades to RESULTS.md if unavailable).

Every figure is rendered to `results/figures/` in both PNG (embeds in the .docx / markdown
preview) and SVG (vector, for publication); findings.json lists all formats per figure.
"""

import importlib
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from rayleigh import __version__
from rayleigh.conduct_exp import expand_cells, resolve_cell_outputs, add_import_paths  # reuse cell logic


def log(msg: str) -> None:
    print(f"[rayleigh process_outputs] {msg}", flush=True)


# Every figure is emitted in each format: PNG (embeds in the .docx / RESULTS.md preview) and
# SVG (vector, for publication). findings.json lists all of them per figure.
FIGURE_FORMATS = ("png", "svg")


# --------------------------------------------------------------------- loading cells
def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def _load_one(path: Path) -> dict:
    """Scalar metrics from one artifact file (JSON dict, or numeric column means of parquet/csv)."""
    if path.suffix == ".json":
        d = json.loads(path.read_text())
        if not isinstance(d, dict):
            return {}
        return {k: v for k, v in _flatten(d).items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)}
    if path.suffix in (".parquet", ".pq", ".csv"):
        import pandas as pd
        df = pd.read_csv(path) if path.suffix == ".csv" else pd.read_parquet(path)
        num = df.select_dtypes("number")
        return {c: float(num[c].mean()) for c in num.columns}
    raise ValueError(f"no default loader for '{path.suffix}' — set code.output_adapter.load")


def _default_load(outputs: dict) -> dict:
    """Merge one cell's scalar metrics across its artifact(s) (outputs = {name: path}).
    With several artifacts, keys are prefixed by name (`summary.rmse`) so they don't collide."""
    multi = len(outputs) > 1
    merged = {}
    for name, path in outputs.items():
        for k, v in _load_one(Path(path)).items():
            merged[f"{name}.{k}" if multi else k] = v
    return merged


def _get_loader(spec: dict, code_dir: Path, results: Path):
    ep = ((spec.get("code") or {}).get("output_adapter") or {}).get("load")
    if not ep:
        return _default_load
    # the loader shim may live in code/ OR results/ (same as the run-adapter) — search both
    add_import_paths(code_dir, results)
    mod_name, _, fn_name = ep.partition(":")
    return getattr(importlib.import_module(mod_name), fn_name)


def _param_keys(exp: dict) -> list:
    design = exp.get("design") or {}
    if design.get("kind") == "conditions":
        keys = []
        for c in design.get("conditions") or []:
            for k in c:
                if k not in keys:
                    keys.append(k)
        return keys
    return list((design.get("axes") or {}).keys())


def build_table(exp: dict, adapter: dict, results: Path, loader):
    """Tidy table: one row per cell with output = {**params, seed, **metrics}."""
    import pandas as pd
    eid = str(exp["id"])
    rows, missing = [], 0
    for cell in expand_cells(exp, adapter):
        outputs = resolve_cell_outputs(adapter, results, eid, cell)
        if not all(p.exists() for p in outputs.values()):
            missing += 1
            continue
        try:
            metrics = loader({n: str(p) for n, p in outputs.items()})
        except Exception as e:                       # noqa: BLE001
            log(f"  {eid}: could not load cell {cell['params']} seed={cell['seed']}: "
                f"{type(e).__name__}: {e}")
            metrics = {}
        rows.append({**cell["params"], "seed": cell["seed"], **metrics})
    df = pd.DataFrame(rows)
    param_cols = [k for k in _param_keys(exp) if k in df.columns]
    metric_cols = [c for c in df.columns if c not in param_cols and c != "seed"]
    return df, param_cols, metric_cols, missing


# --------------------------------------------------------------------- rendering
def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", str(s)).strip("-").lower()[:50] or "out"


def render_figure(df, spec: dict, eid: str, idx: int, figdir: Path):
    """Render one planned figure to results/figures/, in every configured format.
    Returns (primary_path, [all_paths], caption) or None. The primary (PNG) embeds in the
    .docx and RESULTS.md; the vector formats (SVG) are for publication."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ftype = spec.get("type", "line")
    x, y = spec.get("x"), spec.get("y")
    caption = spec.get("caption") or f"{y} vs {x}"
    try:
        fig, ax = plt.subplots(figsize=(6, 4))
        if ftype in ("line", "scatter"):
            if x not in df.columns or y not in df.columns:
                log(f"  {eid} fig {idx}: x='{x}'/y='{y}' not in data — skipped")
                plt.close(fig)
                return None
            facet = spec.get("facet")
            groups = df.groupby(facet) if facet and facet in df.columns else [(None, df)]
            for fval, sub in groups:
                label = f"{facet}={fval}" if facet else None
                if ftype == "line":
                    g = sub.groupby(x)[y].agg(["mean", "std"]).reset_index().sort_values(x)
                    ax.errorbar(g[x], g["mean"], yerr=g["std"].fillna(0),
                                marker="o", capsize=3, label=label)
                else:
                    ax.scatter(sub[x], sub[y], alpha=0.6, label=label)
            ax.set_xlabel(x); ax.set_ylabel(y)
            if spec.get("facet"):
                ax.legend(title=spec["facet"])
        elif ftype == "bar":
            g = df.groupby(x)[y].mean().reset_index()
            ax.bar(g[x].astype(str), g[y]); ax.set_xlabel(x); ax.set_ylabel(y)
        elif ftype == "heatmap":
            # A heatmap's colour axis is its THIRD field: `z` (conventional) or `value`.
            # It must be a metric, not one of the plane's axes — pivoting with values==index
            # is what raises the cryptic "Grouper for '<axis>' not 1-dimensional".
            val = spec.get("z") or spec.get("value")
            if not val or val in (x, y):
                log(f"  {eid} fig {idx}: heatmap needs a `z:` (or `value:`) metric distinct "
                    f"from x='{x}'/y='{y}' — got {val!r}; skipped")
                plt.close(fig)
                return None
            if val not in df.columns:
                log(f"  {eid} fig {idx}: heatmap z='{val}' not in data — skipped")
                plt.close(fig)
                return None
            piv = df.pivot_table(index=y, columns=x, values=val, aggfunc="mean")
            im = ax.imshow(piv.values, aspect="auto", origin="lower")
            ax.set_xticks(range(len(piv.columns))); ax.set_xticklabels(piv.columns)
            ax.set_yticks(range(len(piv.index))); ax.set_yticklabels(piv.index)
            ax.set_xlabel(x); ax.set_ylabel(y); fig.colorbar(im, ax=ax, label=val)
        else:
            log(f"  {eid} fig {idx}: unknown figure type '{ftype}' — skipped")
            plt.close(fig)
            return None
        ax.set_title(caption, fontsize=9)
        fig.tight_layout()
        stem = f"{eid}_{idx}_{_slug(caption)}"
        paths = []
        for fmt in FIGURE_FORMATS:                       # PNG (docx/preview) + SVG (vector)
            p = figdir / f"{stem}.{fmt}"
            fig.savefig(p, dpi=120, bbox_inches="tight")
            paths.append(p)
        plt.close(fig)
        primary = next((p for p in paths if p.suffix == ".png"), paths[0])
        return primary, paths, caption
    except Exception as e:                           # noqa: BLE001
        log(f"  {eid} fig {idx}: render failed ({type(e).__name__}: {e}) — skipped")
        return None


def render_table(df, spec: dict, eid: str):
    """Pivot one planned table. Returns (pivot_df, caption) or None."""
    rows, cols, cell = spec.get("rows"), spec.get("cols"), spec.get("cell")
    caption = spec.get("caption") or f"{cell} by {rows} × {cols}"
    try:
        piv = df.pivot_table(index=rows, columns=cols, values=cell, aggfunc="mean")
        return piv, caption
    except Exception as e:                           # noqa: BLE001
        log(f"  {eid} table: pivot failed ({type(e).__name__}: {e}) — skipped")
        return None


def _observed_summary(exp: dict, df) -> str:
    outs = exp.get("outputs") or []
    x = y = None
    for o in outs:
        if o.get("kind") != "figure" or not o.get("x"):
            continue
        # summarize the plotted METRIC vs x: a heatmap's metric is `z`/`value`, otherwise `y`.
        x = o["x"]
        y = o.get("z") or o.get("value") or o.get("y")
        break
    if not (x and y) or x not in df.columns or y not in df.columns:
        return "data collected; no single x/y trend to summarize automatically"
    try:
        g = df.groupby(x)[y].mean().sort_index()
        lo, hi = g.index[0], g.index[-1]
        a, b = float(g.iloc[0]), float(g.iloc[-1])
        trend = "rises" if b > a else "falls" if b < a else "is flat"
        return f"{y} {trend} from {a:.3g} at {x}={lo} to {b:.3g} at {x}={hi}"
    except Exception:                                # noqa: BLE001
        return "data collected; trend not auto-summarizable"


def finding_text(exp: dict, df) -> str:
    """An honest finding: observed summary next to the prereg, honoring mode. No verdict gate."""
    mode = exp.get("mode", "confirmatory")
    obs = _observed_summary(exp, df)
    if mode == "exploratory":
        return f"Exploratory (not preregistered). Observed: {obs}."
    expected = exp.get("expected_direction")
    prefix = f"Expected (preregistered): {expected}. " if expected else ""
    return f"{prefix}Observed: {obs}."


# --------------------------------------------------------------------- assembly
def _fmt(v) -> str:
    return f"{v:.3g}" if isinstance(v, (int, float)) else str(v)


def _pivot_to_md(piv) -> str:
    cols = [str(piv.index.name or "")] + [str(c) for c in piv.columns]
    lines = ["| " + " | ".join(cols) + " |",
             "|" + "|".join("---" for _ in cols) + "|"]
    for idx, row in piv.iterrows():
        lines.append("| " + " | ".join([str(idx)] + [_fmt(v) for v in row]) + " |")
    return "\n".join(lines)


def _build_markdown(project, cycle, brief, items, results: Path) -> str:
    L = [f"# {project} — Results (cycle {cycle})", "", brief, ""]
    for it in items:
        exp = it["exp"]
        L.append(f"## {exp['id']} — {exp.get('title', '')}")
        L.append(f"*Mode: {exp.get('mode', 'confirmatory')}* · "
                 f"{it['n_data']}/{it['n_cells']} cells with data\n")
        if exp.get("question"):
            L.append(f"**Question:** {exp['question']}\n")
        L.append(f"**Finding:** {it['finding']}\n")
        for primary, _all, cap in it["figures"]:
            rel = primary.relative_to(results)   # PNG embeds in the markdown preview
            L.append(f"![{cap}]({rel})\n\n*{cap}*\n")
        for piv, cap in it["tables"]:
            L.append(_pivot_to_md(piv) + f"\n\n*{cap}*\n")
    L.append(f"\n---\n*Generated by rayleigh {__version__}.*")
    return "\n".join(L)


def _build_findings(project, cycle, brief, items, results: Path) -> str:
    """Machine-readable per-experiment findings for the raconteur hand-off.

    rayleigh's reason to exist is to feed raconteur; its load_results() ingests results/*.json
    and *.csv. This is the structured layer (the .docx is the human deliverable) — the
    preregistration, the observed finding, and pointers to the figure/table artifacts.
    """
    doc = {
        "project": project, "cycle": cycle, "brief": brief,
        "rayleigh_version": __version__,
        "generated": datetime.now(timezone.utc).isoformat(),
        "experiments": [],
    }
    for it in items:
        exp = it["exp"]
        metric = exp.get("metric") or {}
        doc["experiments"].append({
            "id": str(exp["id"]),
            "title": exp.get("title", ""),
            "mode": exp.get("mode", "confirmatory"),
            "question": exp.get("question", ""),
            "metric": {"name": metric.get("name"), "reduce": metric.get("reduce")},
            "expected_direction": exp.get("expected_direction", ""),
            "finding": it["finding"],
            "cells": {"with_data": it["n_data"], "total": it["n_cells"]},
            "figures": [{"path": str(primary.relative_to(results)),
                         "formats": {p.suffix.lstrip("."): str(p.relative_to(results))
                                     for p in all_paths},
                         "caption": c}
                        for primary, all_paths, c in it["figures"]],
            "tables": [{"path": str(p.relative_to(results)), "caption": c}
                       for p, c in it.get("table_files", [])],
        })
    return json.dumps(doc, indent=2)


def _build_docx(path: Path, project, cycle, brief, items, author) -> bool:
    try:
        from docx import Document
        from docx.shared import Inches
    except ImportError:
        return False
    doc = Document()
    doc.core_properties.author = author
    doc.add_heading(f"{project} — Results (cycle {cycle})", 0)
    if brief:
        doc.add_paragraph(brief)
    for it in items:
        exp = it["exp"]
        doc.add_heading(f"{exp['id']} — {exp.get('title', '')}", level=1)
        doc.add_paragraph(f"Mode: {exp.get('mode', 'confirmatory')} · "
                          f"{it['n_data']}/{it['n_cells']} cells with data").italic = True
        if exp.get("question"):
            doc.add_paragraph(f"Question: {exp['question']}")
        f = doc.add_paragraph()
        f.add_run("Finding: ").bold = True
        f.add_run(it["finding"])
        for primary, _all, cap in it["figures"]:
            doc.add_picture(str(primary), width=Inches(5.5))   # PNG — docx can't embed SVG
            doc.add_paragraph(cap).italic = True
        for piv, cap in it["tables"]:
            t = doc.add_table(rows=1, cols=len(piv.columns) + 1)
            t.style = "Table Grid"
            hdr = t.rows[0].cells
            hdr[0].text = str(piv.index.name or "")
            for j, c in enumerate(piv.columns):
                hdr[j + 1].text = str(c)
            for idx, row in piv.iterrows():
                cells = t.add_row().cells
                cells[0].text = str(idx)
                for j, v in enumerate(row):
                    cells[j + 1].text = _fmt(v)
            doc.add_paragraph(cap).italic = True
    doc.save(str(path))
    return True


# --------------------------------------------------------------------- command
def run_process_outputs(args) -> int:
    root = Path(args.dir).resolve() if getattr(args, "dir", None) else Path.cwd()
    results = root / "results"
    spec_path = results / "designdocs" / "experiments.yaml"
    if not spec_path.is_file():
        log(f"no {spec_path} — run `rayleigh init` first")
        return 1
    spec = yaml.safe_load(spec_path.read_text()) or {}
    exps = spec.get("experiments") or []
    if not exps:
        log("experiments.yaml has no experiments — author them in `rayleigh init`")
        return 1
    if getattr(args, "experiment", None):
        exps = [e for e in exps if str(e.get("id")) == args.experiment]
        if not exps:
            log(f"experiment '{args.experiment}' not found")
            return 1

    meta = {}
    ry = results / "rayleigh.yaml"
    if ry.is_file():
        meta = yaml.safe_load(ry.read_text()) or {}
    code = spec.get("code") or {}
    adapter = code.get("run_adapter") or {}
    code_dir = (results / (code.get("path") or "../code")).resolve()
    cycle = str(spec.get("cycle") or meta.get("cycle") or date.today().strftime("%y%m%d"))
    project = str(spec.get("project") or meta.get("project") or "project")
    brief = str(spec.get("brief") or meta.get("brief") or "")

    from rayleigh.config import load_config
    cfg = load_config()
    figdir = results / "figures"
    figdir.mkdir(parents=True, exist_ok=True)
    tabledir = results / "tables"
    loader = _get_loader(spec, code_dir, results)

    dry = getattr(args, "dry_run", False)
    items = []
    for exp in exps:
        eid = str(exp["id"])
        df, param_cols, metric_cols, missing = build_table(exp, adapter, results, loader)
        n_cells = len(df) + missing
        planned = [o.get("kind") for o in exp.get("outputs") or []]
        if dry:
            log(f"{eid}: {len(df)}/{n_cells} cells have data · outputs planned: {planned}")
            continue
        if df.empty:
            log(f"{eid}: no cell data yet — run `rayleigh conduct_exp {eid}` first")
            items.append({"exp": exp, "finding": "No data collected yet.", "figures": [],
                          "tables": [], "table_files": [], "n_data": 0, "n_cells": n_cells})
            continue
        figs, tables, table_files = [], [], []
        for i, o in enumerate(exp.get("outputs") or []):
            if o.get("kind") == "figure":
                r = render_figure(df, o, eid, i, figdir)
                if r:
                    figs.append(r)   # (primary_png, [all_format_paths], caption)
            elif o.get("kind") == "table":
                r = render_table(df, o, eid)
                if r:
                    piv, cap = r
                    tables.append(r)
                    tabledir.mkdir(parents=True, exist_ok=True)  # CSV so raconteur gets the numbers
                    csv_path = tabledir / f"{eid}_{i}_{_slug(cap)}.csv"
                    piv.to_csv(csv_path)
                    table_files.append((csv_path, cap))
            else:
                # A preregistered output rayleigh can't render itself (e.g. `artifact`, `stat`).
                # Don't drop it silently — say so, so a planned deliverable never just vanishes.
                kind = o.get("kind") or "(no kind)"
                name = o.get("name") or o.get("caption") or ""
                log(f"  {eid} out {i}: output kind '{kind}' not rendered by process_outputs"
                    f"{f' ({name[:50]})' if name else ''} — produce it from the stored data "
                    f"(e.g. an adapter helper), then reference it in the write-up")
        log(f"{eid}: {len(df)}/{n_cells} cells · {len(figs)} figure(s), {len(tables)} table(s)")
        items.append({"exp": exp, "finding": finding_text(exp, df), "figures": figs,
                      "tables": tables, "table_files": table_files,
                      "n_data": len(df), "n_cells": n_cells})
    if dry:
        return 0

    (results / "RESULTS.md").write_text(_build_markdown(project, cycle, brief, items, results))
    log(f"wrote {(results / 'RESULTS.md').relative_to(root)}")

    # Structured hand-off for raconteur (its load_results ingests results/*.json + *.csv).
    (results / "findings.json").write_text(_build_findings(project, cycle, brief, items, results))
    log(f"wrote {(results / 'findings.json').relative_to(root)}"
        + (f" + {len([f for it in items for f in it.get('table_files', [])])} table CSV(s)"
           if any(it.get("table_files") for it in items) else ""))

    if getattr(args, "no_docx", False):
        log("--no-docx: skipped the .docx (RESULTS.md written)")
        return 0
    docx_name = f"{cycle}_{project}_results_{cfg.tool_initials}.docx"
    if _build_docx(results / docx_name, project, cycle, brief, items, cfg.author_name):
        log(f"wrote results/{docx_name}")
    else:
        log("python-docx unavailable — wrote RESULTS.md only "
            "(pip install python-docx for the .docx deliverable)")
    return 0
