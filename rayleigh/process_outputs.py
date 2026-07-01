"""`rayleigh process_outputs` — reduce cell data into an experiment's preregistered
outputs, findings, and the datestamped .docx write-up.

For each experiment: re-expand its cells (the same expansion conduct_exp ran), load each
cell's output into a tidy table, aggregate over seeds, render the outputs planned during
`init` (figures via matplotlib, tables via pivot), and state an honest finding — the
observed summary next to the preregistered `expected_direction`, honoring
`mode: confirmatory | exploratory`. No auto-verdict gate; honesty lives in the wording.

Reading a cell's output uses a loader:
  - default: JSON dict of scalars, or a parquet/csv (numeric column means);
  - override: `code.output_adapter.load: "module:callable"` — callable(output_path) -> dict.

Deliverable: `results/{cycle}_{project}_results_{ra}.docx` (python-docx; degrades to a
Markdown RESULTS.md if python-docx is unavailable), plus figures in `results/figures/`.
"""

import importlib
import json
import re
import sys
from datetime import date
from pathlib import Path

import yaml

from rayleigh import __version__
from rayleigh.conduct_exp import expand_cells, _resolve_output, _cellkey  # reuse cell logic


def log(msg: str) -> None:
    print(f"[rayleigh process_outputs] {msg}", flush=True)


DEFAULT_TEMPLATE = "data/{experiment}/{cellkey}_seed{seed}.parquet"


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


def _default_load(path: str) -> dict:
    """Load one cell's scalar metrics with no project-specific adapter."""
    p = Path(path)
    if p.suffix == ".json":
        d = json.loads(p.read_text())
        if not isinstance(d, dict):
            return {}
        return {k: v for k, v in _flatten(d).items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)}
    if p.suffix in (".parquet", ".pq", ".csv"):
        import pandas as pd
        df = pd.read_csv(p) if p.suffix == ".csv" else pd.read_parquet(p)
        num = df.select_dtypes("number")
        return {c: float(num[c].mean()) for c in num.columns}
    raise ValueError(f"no default loader for '{p.suffix}' — set code.output_adapter.load")


def _get_loader(spec: dict, code_dir: Path):
    ep = ((spec.get("code") or {}).get("output_adapter") or {}).get("load")
    if not ep:
        return _default_load
    if str(code_dir) not in sys.path:
        sys.path.insert(0, str(code_dir))
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
    template = adapter.get("output_template") or DEFAULT_TEMPLATE
    rows, missing = [], 0
    for cell in expand_cells(exp, adapter):
        out = _resolve_output(template, results, eid, cell)
        if not out.exists():
            missing += 1
            continue
        try:
            metrics = loader(str(out))
        except Exception as e:                       # noqa: BLE001
            log(f"  {eid}: could not load {out.name}: {type(e).__name__}: {e}")
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
    """Render one planned figure to results/figures/. Returns (path, caption) or None."""
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
            val = spec.get("value") or y
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
        path = figdir / f"{eid}_{idx}_{_slug(caption)}.png"
        fig.savefig(path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        return path, caption
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
        if o.get("kind") == "figure" and o.get("x") and o.get("y"):
            x, y = o["x"], o["y"]
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
        for path, cap in it["figures"]:
            rel = path.relative_to(results)
            L.append(f"![{cap}]({rel})\n\n*{cap}*\n")
        for piv, cap in it["tables"]:
            L.append(_pivot_to_md(piv) + f"\n\n*{cap}*\n")
    L.append(f"\n---\n*Generated by rayleigh {__version__}.*")
    return "\n".join(L)


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
        for fig_path, cap in it["figures"]:
            doc.add_picture(str(fig_path), width=Inches(5.5))
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
    loader = _get_loader(spec, code_dir)

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
            items.append({"exp": exp, "finding": "No data collected yet.",
                          "figures": [], "tables": [], "n_data": 0, "n_cells": n_cells})
            continue
        figs, tables = [], []
        for i, o in enumerate(exp.get("outputs") or []):
            if o.get("kind") == "figure":
                r = render_figure(df, o, eid, i, figdir)
                if r:
                    figs.append(r)
            elif o.get("kind") == "table":
                r = render_table(df, o, eid)
                if r:
                    tables.append(r)
        log(f"{eid}: {len(df)}/{n_cells} cells · {len(figs)} figure(s), {len(tables)} table(s)")
        items.append({"exp": exp, "finding": finding_text(exp, df),
                      "figures": figs, "tables": tables, "n_data": len(df), "n_cells": n_cells})
    if dry:
        return 0

    (results / "RESULTS.md").write_text(_build_markdown(project, cycle, brief, items, results))
    log(f"wrote {(results / 'RESULTS.md').relative_to(root)}")

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
