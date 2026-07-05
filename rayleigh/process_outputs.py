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
    fixed = list((exp.get("fixed") or {}).keys())      # constants are params, not metrics
    if design.get("kind") == "conditions":
        keys = list(fixed)
        for c in design.get("conditions") or []:
            for k in c:
                if k not in keys:
                    keys.append(k)
        return keys
    return fixed + [k for k in (design.get("axes") or {}).keys() if k not in fixed]


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
        # No title/caption baked into the image — the PNG/SVG is graphics only; the caption is
        # carried as text by the report ("Figure N. …") and findings.json.
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


def _methods_lines(exp: dict) -> list:
    """Human-readable Method bullets: design, fixed params, replications, metric(s)."""
    design = exp.get("design") or {}
    lines = []
    kind = design.get("kind", "sweep")
    axes = design.get("axes") or {}
    if axes:
        lines.append("Design: " + kind + " over " + "; ".join(
            f"{k} ∈ {{{', '.join(str(x) for x in v)}}}" for k, v in axes.items()))
    elif design.get("conditions"):
        lines.append(f"Design: {kind} · {len(design['conditions'])} named conditions")
    else:
        lines.append(f"Design: {kind}")
    fixed = exp.get("fixed") or {}
    if fixed:
        lines.append("Fixed parameters: " + ", ".join(f"{k} = {v}" for k, v in fixed.items()))
    seeds = exp.get("seeds", design.get("seeds"))
    if seeds:
        lines.append(f"Replications: {seeds} seeds per cell")
    metric = exp.get("metric") or {}
    if metric.get("name"):
        reduce = str(metric.get("reduce", "")).strip()
        lines.append(f"Primary metric — {metric['name']}" + (f": {reduce}" if reduce else ""))
    for sm in (metric.get("secondary") or []):
        if sm.get("name"):
            r = str(sm.get("reduce", "")).strip()
            lines.append(f"Secondary — {sm['name']}" + (f": {r}" if r else ""))
    return lines


def _metric_summary_lines(exp: dict, df, param_cols: list, metric_cols: list) -> list:
    """Per-metric range and where the extremes sit over the swept axes — the quantitative
    backbone of the Results prose. Preregistered metric first."""
    if df is None or df.empty or not metric_cols:
        return []
    primary = (exp.get("metric") or {}).get("name")
    ordered = ([primary] if primary in metric_cols else []) + \
              [m for m in metric_cols if m != primary]
    # locate extremes over the axes that actually VARY (a fixed/constant param adds no signal)
    gcols = [c for c in param_cols if df[c].nunique() > 1] or param_cols

    def _loc(idx) -> str:
        vals = idx if isinstance(idx, tuple) else (idx,)
        return ", ".join(f"{k}={v}" for k, v in zip(gcols, vals))

    lines = []
    for m in ordered[:8]:
        try:
            s = df.groupby(gcols)[m].mean() if gcols else df[m]
            mn, mx = float(s.min()), float(s.max())
        except Exception:                                # noqa: BLE001
            continue
        span = f"{m}: {mn:.3g} to {mx:.3g}"
        if gcols and hasattr(s, "idxmin") and len(s) > 1 and mn != mx:
            span += f"  (lowest at {_loc(s.idxmin())}; highest at {_loc(s.idxmax())})"
        lines.append(span)
    return lines


def _unrendered_outputs(exp: dict) -> list:
    """Preregistered outputs rayleigh can't draw itself (artifact / stat / …), so the report
    names them rather than letting a planned deliverable silently vanish."""
    out = []
    for o in exp.get("outputs") or []:
        if o.get("kind") in ("figure", "table"):
            continue
        out.append((o.get("kind") or "output", o.get("name") or "",
                    str(o.get("caption") or "").strip()))
    return out


def _code_sha_from_provenance(results: Path) -> str | None:
    """Best-effort code SHA for the provenance footer: read the first cell's .prov.json."""
    data = results / "data"
    if not data.is_dir():
        return None
    for p in data.rglob("*.prov.json"):
        try:
            return json.loads(p.read_text()).get("code_sha")
        except Exception:                                # noqa: BLE001
            return None
    return None


def _build_docx(path: Path, project, cycle, brief, items, author,
                generated: datetime, code_sha: str | None) -> bool:
    """The human deliverable: a full, self-contained report — title page, an executive summary
    across experiments, then one complete section per experiment (question, method, the
    preregistration, results prose + metric ranges, embedded numbered figures/tables, and any
    planned artifacts rayleigh couldn't draw), closing with provenance."""
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Inches, Pt
    except ImportError:
        return False

    def italic(text, size=None):
        p = doc.add_paragraph()
        r = p.add_run(text)
        r.italic = True
        if size:
            r.font.size = Pt(size)
        return p

    def bullet(text):
        doc.add_paragraph(text, style="List Bullet")

    doc = Document()
    doc.core_properties.author = author

    # ── Title page ──────────────────────────────────────────────────────────────────
    doc.add_heading(f"{project}", 0)
    st = doc.add_paragraph()
    st.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = st.add_run(f"Results — research cycle {cycle}")
    r.bold = True
    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub.add_run(f"Generated {generated:%Y-%m-%d}").italic = True
    if brief:
        doc.add_heading("Overview", level=1)
        doc.add_paragraph(brief.strip())

    # ── Executive summary across experiments ────────────────────────────────────────
    if len(items) > 1:
        doc.add_heading("Summary of experiments", level=1)
        t = doc.add_table(rows=1, cols=4)
        t.style = "Table Grid"
        for j, h in enumerate(("ID", "Experiment", "Mode", "Coverage")):
            run = t.rows[0].cells[j].paragraphs[0].add_run(h)
            run.bold = True
        for it in items:
            exp = it["exp"]
            cells = t.add_row().cells
            cells[0].text = str(exp["id"])
            cells[1].text = str(exp.get("title", ""))
            cells[2].text = str(exp.get("mode", "confirmatory"))
            cells[3].text = f"{it['n_data']}/{it['n_cells']} cells"

    # ── One full section per experiment ─────────────────────────────────────────────
    fig_no = tab_no = 0
    for it in items:
        exp = it["exp"]
        doc.add_page_break()
        doc.add_heading(f"{exp['id']} — {exp.get('title', '')}", level=1)
        italic(f"{str(exp.get('mode', 'confirmatory')).capitalize()} · "
               f"{it['n_data']}/{it['n_cells']} cells with data")

        if exp.get("question"):
            doc.add_heading("Question", level=2)
            doc.add_paragraph(str(exp["question"]).strip())

        method = _methods_lines(exp)
        if method:
            doc.add_heading("Method", level=2)
            for line in method:
                bullet(line)

        expected = exp.get("expected_direction")
        if str(exp.get("mode", "confirmatory")) == "confirmatory" and expected:
            doc.add_heading("Preregistered expectation", level=2)
            doc.add_paragraph(str(expected).strip())
        elif str(exp.get("mode")) == "exploratory":
            italic("Exploratory experiment — not preregistered; findings are hypothesis-generating.")

        doc.add_heading("Results", level=2)
        f = doc.add_paragraph()
        f.add_run("Finding: ").bold = True
        f.add_run(it["finding"])
        for line in _metric_summary_lines(exp, it.get("df"), it.get("param_cols") or [],
                                          it.get("metric_cols") or []):
            bullet(line)

        for primary, _all, cap in it["figures"]:
            fig_no += 1
            try:
                doc.add_picture(str(primary), width=Inches(6.0))   # PNG — docx can't embed SVG
                doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
            except Exception as e:                       # noqa: BLE001
                doc.add_paragraph(f"[figure {fig_no} could not be embedded: {e}]")
            cap_p = doc.add_paragraph()
            cr = cap_p.add_run(f"Figure {fig_no}. {cap}")
            cr.italic = True
            cr.font.size = Pt(9)

        for k, (piv, cap) in enumerate(it["tables"]):
            tab_no += 1
            t = doc.add_table(rows=1, cols=len(piv.columns) + 1)
            t.style = "Table Grid"
            hdr = t.rows[0].cells
            hdr[0].paragraphs[0].add_run(str(piv.index.name or "")).bold = True
            for j, c in enumerate(piv.columns):
                hdr[j + 1].paragraphs[0].add_run(str(c)).bold = True
            for idx, row in piv.iterrows():
                cells = t.add_row().cells
                cells[0].text = str(idx)
                for j, v in enumerate(row):
                    cells[j + 1].text = _fmt(v)
            cap_p = doc.add_paragraph()
            cr = cap_p.add_run(f"Table {tab_no}. {cap}")
            cr.italic = True
            cr.font.size = Pt(9)

        planned = _unrendered_outputs(exp)
        if planned:
            doc.add_heading("Planned outputs (produced outside this report)", level=2)
            for kind, name, cap in planned:
                label = f"{kind}" + (f" — {name}" if name else "")
                bullet(f"{label}: {cap}" if cap else label)

    # ── Synthesis (collated, not adjudicated) ───────────────────────────────────────
    doc.add_page_break()
    doc.add_heading("Synthesis", level=1)
    n = len(items)
    conf = sum(1 for it in items if str(it["exp"].get("mode", "confirmatory")) == "confirmatory")
    tot_data = sum(it["n_data"] for it in items)
    tot_cells = sum(it["n_cells"] for it in items)
    doc.add_paragraph(
        f"This cycle comprises {n} experiment{'s' if n != 1 else ''} "
        f"({conf} confirmatory, {n - conf} exploratory), {tot_data}/{tot_cells} cells with data. "
        f"Each finding below is stated against its own preregistration; the findings are brought "
        f"together here but not adjudicated across experiments.")
    doc.add_heading("Findings at a glance", level=2)
    for it in items:
        exp = it["exp"]
        p = doc.add_paragraph(style="List Bullet")
        p.add_run(f"{exp['id']} — {exp.get('title', '')}: ").bold = True
        p.add_run(it["finding"])
    deps = [(str(it["exp"]["id"]), it["exp"].get("depends_on"))
            for it in items if it["exp"].get("depends_on")]
    if deps:
        doc.add_paragraph("Design dependencies: "
                          + "; ".join(f"{a} builds on {b}" for a, b in deps) + ".")
    italic("Cross-experiment interpretation — reconciling these findings into a single account — "
           "belongs to the author's write-up (or raconteur); rayleigh reports, it does not conclude.")

    # ── Provenance ──────────────────────────────────────────────────────────────────
    doc.add_page_break()
    doc.add_heading("Provenance", level=1)
    bullet(f"Generated by rayleigh {__version__} on {generated:%Y-%m-%d %H:%M UTC}.")
    if code_sha and code_sha != "unknown":
        bullet(f"Analysis code at commit {code_sha}.")
    bullet("Figures also written as SVG (vector) alongside the embedded PNGs; per-cell numbers "
           "in results/tables/*.csv and results/findings.json.")
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
                          "tables": [], "table_files": [], "n_data": 0, "n_cells": n_cells,
                          "df": df, "param_cols": param_cols, "metric_cols": metric_cols})
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
                      "n_data": len(df), "n_cells": n_cells,
                      "df": df, "param_cols": param_cols, "metric_cols": metric_cols})
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
    if _build_docx(results / docx_name, project, cycle, brief, items, cfg.author_name,
                   datetime.now(timezone.utc), _code_sha_from_provenance(results)):
        log(f"wrote results/{docx_name}")
    else:
        log("python-docx unavailable — wrote RESULTS.md only "
            "(pip install python-docx for the .docx deliverable)")
    return 0
