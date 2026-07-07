"""Generate + run the R analysis for one experiment.

rayleigh does not compute analytical products itself. It writes the tidy per-cell data as CSV and
a **base R + ggplot2** script (NO tidyverse) that produces every figure, table, summary statistic,
and regression, then runs it with Rscript. The script and data are durable, editable artifacts:

    Rscript results/analysis/<E>.R        # regenerates every product below, at any time

so the analytical outputs are reproducible independently of rayleigh. `process_outputs` is the
assembler — it embeds what the R script produced into the report.

Layout (all paths resolved by the script itself, so it runs from anywhere):
    results/analysis/data/<E>.csv     the tidy per-cell data (params · seed · metrics)
    results/analysis/<E>.R            the generated analysis script (edit + re-run freely)
    results/analysis/stats/           summary-stat and regression CSVs
    results/figures/                  <E>_<i>_<slug>.png (+ .eps vector)
    results/tables/                   <E>_<i>_<slug>.csv

Escape hatch: set `code.analysis_r: "path/to/custom.R"` (relative to results/) to run your own
script instead of the generated one; rayleigh collects outputs by the same filename convention.
"""

import re
import shutil
import subprocess
from pathlib import Path
from string import Template


def log(msg: str) -> None:
    print(f"[rayleigh process_outputs] {msg}", flush=True)


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", str(s)).strip("-").lower()[:50] or "out"


def _rs(s) -> str:
    """A single R string literal."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _rvec(names) -> str:
    return "c(" + ", ".join(_rs(n) for n in names) + ")" if names else "character(0)"


# NOTE: every R snippet below is written with `d[["col"]]` indexing and avoids the `$` operator,
# so string.Template's `$` placeholders never collide with R syntax.
_HEADER = Template("""\
# ============================================================================
# rayleigh - analysis for experiment $EID   (base R + ggplot2; NO tidyverse)
# Regenerate every product below at any time:   Rscript results/analysis/$EID.R
# This is YOUR analysis - edit it freely; rayleigh re-runs whatever is here.
# ============================================================================
suppressMessages(library(ggplot2))

# Resolve results/ from this script's own location (results/analysis/$EID.R), so it runs anywhere.
.a <- commandArgs(FALSE); .f <- sub("^--file=", "", .a[grep("^--file=", .a)])
ROOT <- if (length(.f)) normalizePath(file.path(dirname(.f[1]), "..")) else normalizePath(".")
DATA <- file.path(ROOT, "analysis", "data", "$EID.csv")
FIG  <- file.path(ROOT, "figures"); TAB <- file.path(ROOT, "tables")
STA  <- file.path(ROOT, "analysis", "stats")
for (.dd in c(FIG, TAB, STA)) dir.create(.dd, showWarnings = FALSE, recursive = TRUE)

d <- read.csv(DATA, stringsAsFactors = FALSE)
cat(sprintf("[%s] loaded %d rows x %d cols\\n", "$EID", nrow(d), ncol(d)))

# ---- summary statistics (per metric) ---------------------------------------
.mets <- $METRICS
if (length(.mets)) {
  .summ <- do.call(rbind, lapply(.mets, function(m) {
    v <- suppressWarnings(as.numeric(d[[m]])); v <- v[is.finite(v)]
    if (!length(v)) return(data.frame(metric=m, n=0, mean=NA, sd=NA, min=NA, max=NA))
    data.frame(metric=m, n=length(v), mean=mean(v), sd=sd(v), min=min(v), max=max(v))
  }))
} else .summ <- data.frame(metric=character(0), n=integer(0), mean=numeric(0),
                           sd=numeric(0), min=numeric(0), max=numeric(0))
write.csv(.summ, file.path(STA, "${EID}_summary.csv"), row.names = FALSE)
""")

_FIG_HEATMAP = Template("""
# ---- figure $I: heatmap ($Z over $X x $Y) ----
tryCatch({
  agg <- aggregate(list(val=d[["$Z"]]), by=list(cx=d[["$X"]], cy=d[["$Y"]]),
                   FUN=function(z) mean(z, na.rm=TRUE))
  p <- ggplot(agg, aes(factor(cx), factor(cy), fill=val)) + geom_tile() +
       scale_fill_viridis_c() + labs(x=$RX, y=$RY, fill=$RZ) + theme_minimal()
  .save2(p, "$PNG", "$PDF")
}, error=function(e) message("rayleigh: figure $I (heatmap) failed: ", conditionMessage(e)))
""")

_FIG_LINE = Template("""
# ---- figure $I: line ($Y vs $X$FACET_NOTE) ----
tryCatch({
  by <- list(cx=d[["$X"]]$FACET_BY)
  m <- aggregate(list(val=d[["$Y"]]), by=by, FUN=function(z) mean(z, na.rm=TRUE))
  s <- aggregate(list(sd=d[["$Y"]]),  by=by, FUN=function(z) sd(z, na.rm=TRUE))
  m[["sd"]] <- ifelse(is.na(s[["sd"]]), 0, s[["sd"]])
  p <- ggplot(m, aes(cx, val$FACET_AES)) + geom_line() + geom_point() +
       geom_errorbar(aes(ymin=val-sd, ymax=val+sd), width=0) +
       labs(x=$RX, y=$RY$FACET_LAB) + theme_minimal()
  .save2(p, "$PNG", "$PDF")
}, error=function(e) message("rayleigh: figure $I (line) failed: ", conditionMessage(e)))
""")

_FIG_BAR = Template("""
# ---- figure $I: bar ($Y by $X) ----
tryCatch({
  agg <- aggregate(list(val=d[["$Y"]]), by=list(cx=d[["$X"]]), FUN=function(z) mean(z, na.rm=TRUE))
  p <- ggplot(agg, aes(factor(cx), val)) + geom_col() + labs(x=$RX, y=$RY) + theme_minimal()
  .save2(p, "$PNG", "$PDF")
}, error=function(e) message("rayleigh: figure $I (bar) failed: ", conditionMessage(e)))
""")

_FIG_SCATTER = Template("""
# ---- figure $I: scatter ($Y vs $X) ----
tryCatch({
  p <- ggplot(d, aes(d[["$X"]], d[["$Y"]])) + geom_point(alpha=0.6) +
       labs(x=$RX, y=$RY) + theme_minimal()
  .save2(p, "$PNG", "$PDF")
}, error=function(e) message("rayleigh: figure $I (scatter) failed: ", conditionMessage(e)))
""")

_TABLE = Template("""
# ---- table $I: $CELL by $ROWS x $COLS ----
tryCatch({
  tb <- tapply(d[["$CELL"]], list(d[["$ROWS"]], d[["$COLS"]]), function(z) mean(z, na.rm=TRUE))
  write.csv(tb, file.path(TAB, "$CSV"))
}, error=function(e) message("rayleigh: table $I failed: ", conditionMessage(e)))
""")

_REGRESSION = Template("""
# ---- regression $I: $FORMULA ----
tryCatch({
  m <- $FIT($FORMULA, data=d$FAMILY)
  co <- as.data.frame(coef(summary(m)))
  co <- cbind(term=rownames(co), co)
  write.csv(co, file.path(STA, "$CSV"), row.names=FALSE)
}, error=function(e) message("rayleigh: regression $I failed: ", conditionMessage(e)))
""")

# ── continuous-design helpers (LOESS surfaces, contours, bootstrap bands) ─────────────
# A base-R + ggplot2 toolkit for `sobol`/continuous experiments: fit a LOESS surface/curve to
# the scattered draws, predict on a grid, and render phase diagrams, difference maps, and
# smoothed lines with bootstrap CIs. Raw string (normal R `$`), injected once per script;
# `__PARAMCOLS__` is the parameter-column vector (for auto-locating a categorical selector).
_HELPERS = r"""
# ---- rayleigh continuous-analysis helpers (base R + ggplot2) ----------------
.PARAMCOLS <- __PARAMCOLS__
.num <- function(x) suppressWarnings(as.numeric(x))
.rng <- function(x){ x <- x[is.finite(x)]; if (length(x) < 2) c(0, 1) else range(x) }
# the parameter column that holds a given categorical value (e.g. which col has "BCERT")
.selcol <- function(df, val){
  for (cc in .PARAMCOLS) if (cc %in% names(df) && val %in% unique(as.character(df[[cc]]))) return(cc)
  NA_character_
}
# LOESS surface val ~ x*y predicted on an ngrid x ngrid box over the observed range
.loess_grid <- function(df, xc, yc, zc, ngrid=55, span=0.75){
  s <- data.frame(cx=.num(df[[xc]]), cy=.num(df[[yc]]), val=.num(df[[zc]]))
  s <- s[is.finite(s$cx) & is.finite(s$cy) & is.finite(s$val), ]
  if (nrow(s) < 8) stop("too few finite points for a surface")
  fit <- tryCatch(loess(val ~ cx * cy, data=s, span=span, degree=1,
                        control=loess.control(surface="direct")),
                  error=function(e) loess(val ~ cx + cy, data=s, span=span, degree=1,
                        control=loess.control(surface="direct")))
  gx <- seq(.rng(s$cx)[1], .rng(s$cx)[2], length.out=ngrid)
  gy <- seq(.rng(s$cy)[1], .rng(s$cy)[2], length.out=ngrid)
  g <- expand.grid(cx=gx, cy=gy); g$val <- as.numeric(predict(fit, g)); g
}
# bootstrap contour lines (resample rows -> refit -> extract contours) for a CI envelope
.boot_contours <- function(df, xc, yc, zc, levels, B=30, ngrid=45, span=0.75){
  out <- list()
  for (b in seq_len(B)){
    g <- tryCatch(.loess_grid(df[sample(nrow(df), replace=TRUE), , drop=FALSE], xc, yc, zc, ngrid, span),
                  error=function(e) NULL)
    if (is.null(g)) next
    gx <- sort(unique(g$cx)); gy <- sort(unique(g$cy))
    z <- matrix(g$val, nrow=length(gx))
    for (cl in contourLines(gx, gy, z, levels=levels))
      out[[length(out)+1]] <- data.frame(cx=cl$x, cy=cl$y, grp=paste0(b, "_", length(out)))
  }
  if (length(out)) do.call(rbind, out) else NULL
}
# save a figure as PNG (report embed) + EPS (vector companion). Prefer cairo_ps for the EPS so
# semi-transparency (bootstrap CI envelopes/ribbons) survives; else ggsave's built-in EPS device.
.save2 <- function(p, png, vec, width=6.2, height=4.4){
  ggsave(file.path(FIG, png), p, width=width, height=height, dpi=120)
  if (isTRUE(capabilities("cairo")))
    ggsave(file.path(FIG, vec), p, width=width, height=height, device=grDevices::cairo_ps)
  else
    ggsave(file.path(FIG, vec), p, width=width, height=height)   # .eps -> ggsave's postscript EPS
}
# phase diagram: LOESS surface of `zc` over (xc, yc); optional scenario filter, contours (+boot CI), diverging scale
.save_surface <- function(df, xc, yc, zc, selval, contours, diverging, bootB, png, pdf, lx, ly, lz){
  d2 <- df
  if (nzchar(selval)){ sc <- .selcol(df, selval); if (!is.na(sc)) d2 <- df[as.character(df[[sc]]) == selval, , drop=FALSE] }
  g <- .loess_grid(d2, xc, yc, zc)
  p <- ggplot(g, aes(cx, cy, fill=val)) + geom_raster(interpolate=TRUE)
  p <- p + (if (isTRUE(diverging)) scale_fill_gradient2(low="#2166ac", mid="#f7f7f7", high="#b2182b", midpoint=0)
            else scale_fill_viridis_c())
  if (length(contours)){
    if (bootB > 0){ bc <- .boot_contours(d2, xc, yc, zc, contours)
      if (!is.null(bc)) p <- p + geom_path(data=bc, aes(cx, cy, group=grp), inherit.aes=FALSE,
                                            colour="black", alpha=0.10, linewidth=0.3) }
    p <- p + geom_contour(aes(z=val), breaks=contours, colour="black", linewidth=0.5)
  }
  p <- p + labs(x=lx, y=ly, fill=lz) + theme_minimal()
  .save2(p, png, pdf)
}
# difference phase map: LOESS surface(labA) - surface(labB) of `metric` over (xc, yc), diverging, zero/contour lines
.save_diff <- function(df, xc, yc, metric, labA, labB, contours, bootB, png, pdf, lx, ly, lz){
  sc <- .selcol(df, labA); if (is.na(sc)) stop(paste("no parameter column holds", labA))
  gA <- .loess_grid(df[as.character(df[[sc]]) == labA, , drop=FALSE], xc, yc, metric)
  gB <- .loess_grid(df[as.character(df[[sc]]) == labB, , drop=FALSE], xc, yc, metric)
  g <- gA; g$val <- gA$val - gB$val
  p <- ggplot(g, aes(cx, cy, fill=val)) + geom_raster(interpolate=TRUE) +
       scale_fill_gradient2(low="#2166ac", mid="#f7f7f7", high="#b2182b", midpoint=0)
  if (length(contours)) p <- p + geom_contour(aes(z=val), breaks=contours, colour="black", linewidth=0.5)
  p <- p + labs(x=lx, y=ly, fill=lz) + theme_minimal()
  .save2(p, png, pdf)
}
# smoothed line of `yc` vs `xc` (per `series`), with a bootstrap CI band and optional hlines
.save_line <- function(df, xc, yc, series, hlines, bootB, png, pdf, lx, ly, ls){
  mk <- function(sub){
    s <- data.frame(cx=.num(sub[[xc]]), val=.num(sub[[yc]]))
    s <- s[is.finite(s$cx) & is.finite(s$val), ]
    if (nrow(s) < 5) stop("too few points for a line")
    gx <- seq(.rng(s$cx)[1], .rng(s$cx)[2], length.out=120)
    fit <- loess(val ~ cx, data=s, span=0.75, degree=2)
    pr <- as.numeric(predict(fit, data.frame(cx=gx)))
    lo <- rep(NA_real_, length(gx)); hi <- lo
    if (bootB > 0){
      M <- matrix(NA_real_, bootB, length(gx))
      for (b in seq_len(bootB)){
        fb <- tryCatch(loess(val ~ cx, data=s[sample(nrow(s), replace=TRUE), , drop=FALSE], span=0.75, degree=2),
                       error=function(e) NULL)
        if (!is.null(fb)) M[b, ] <- as.numeric(predict(fb, data.frame(cx=gx)))
      }
      lo <- apply(M, 2, quantile, 0.05, na.rm=TRUE); hi <- apply(M, 2, quantile, 0.95, na.rm=TRUE)
    }
    data.frame(cx=gx, val=pr, lo=lo, hi=hi)
  }
  if (nzchar(series) && series %in% names(df)){
    parts <- list()
    for (lv in unique(as.character(df[[series]]))){
      gg <- tryCatch(mk(df[as.character(df[[series]]) == lv, , drop=FALSE]), error=function(e) NULL)
      if (!is.null(gg)){ gg$grp <- lv; parts[[length(parts)+1]] <- gg }
    }
    g <- do.call(rbind, parts)
    p <- ggplot(g, aes(cx, val, colour=grp, fill=grp))
    if (bootB > 0) p <- p + geom_ribbon(aes(ymin=lo, ymax=hi), alpha=0.15, colour=NA)
    p <- p + geom_line(linewidth=0.7) + labs(colour=ls, fill=ls)
  } else {
    g <- mk(df); p <- ggplot(g, aes(cx, val))
    if (bootB > 0) p <- p + geom_ribbon(aes(ymin=lo, ymax=hi), alpha=0.15)
    p <- p + geom_line(linewidth=0.7)
  }
  if (length(hlines)) p <- p + geom_hline(yintercept=hlines, linetype="dashed", colour="grey40")
  .save2(p + labs(x=lx, y=ly) + theme_minimal(), png, pdf)
}
"""

_FIG_SURFACE = Template("""
# ---- figure $I: LOESS phase diagram ($Z over $X x $Y) ----
tryCatch(.save_surface(d, "$X", "$Y", "$Z", "$SEL", $CONTOURS, $DIVERGING, $BOOTB,
                       "$PNG", "$PDF", $RX, $RY, $RZ),
  error=function(e) message("rayleigh: figure $I (surface) failed: ", conditionMessage(e)))
""")

_FIG_DIFF = Template("""
# ---- figure $I: difference phase map ($METRIC: $LABA - $LABB over $X x $Y) ----
tryCatch(.save_diff(d, "$X", "$Y", "$METRIC", "$LABA", "$LABB", $CONTOURS, $BOOTB,
                    "$PNG", "$PDF", $RX, $RY, $RZ),
  error=function(e) message("rayleigh: figure $I (diff) failed: ", conditionMessage(e)))
""")

_FIG_LINE_SMOOTH = Template("""
# ---- figure $I: LOESS line ($Y vs $X$SERIES_NOTE) ----
tryCatch(.save_line(d, "$X", "$Y", "$SERIES", $HLINES, $BOOTB, "$PNG", "$PDF", $RX, $RY, $RS),
  error=function(e) message("rayleigh: figure $I (line) failed: ", conditionMessage(e)))
""")


def _rnums(xs) -> str:
    """R numeric vector literal from a list of numbers (empty -> numeric(0))."""
    xs = xs or []
    return "c(" + ", ".join(repr(float(x)) for x in xs) + ")" if xs else "numeric(0)"


_DIFF_RE = re.compile(r"\s*(\w+)\s*\[\s*([^\]]+?)\s*\]\s*-\s*\w+\s*\[\s*([^\]]+?)\s*\]\s*$")


def _figure_block(o, i, x, y, png, pdf):
    ftype = o.get("type", "line")
    rx, ry = _rs(x or ""), _rs(y or "")
    common = dict(I=i, X=x, Y=y, RX=rx, RY=ry, PNG=png, PDF=pdf)
    smooth = str(o.get("smooth") or "").lower()
    bootB = 30 if str(o.get("contour_ci") or o.get("band") or "").lower().startswith("boot") else 0

    if ftype == "heatmap_diff":                          # difference of two scenario surfaces
        m = _DIFF_RE.match(str(o.get("z") or ""))
        if not m:
            return None, f"heatmap_diff needs z like 'metric[A] - metric[B]' (got {o.get('z')!r})"
        metric, a, b = m.group(1), m.group(2), m.group(3)
        return _FIG_DIFF.substitute(METRIC=metric, LABA=a, LABB=b, CONTOURS=_rnums(o.get("contours")),
                                    BOOTB=bootB, RZ=_rs(f"{metric} ({a}-{b})"), **common), None
    if ftype == "heatmap":
        z = o.get("z") or o.get("value")
        if not z or z in (x, y):
            return None, f"heatmap needs a `z:` metric distinct from x/y (got {z!r})"
        if smooth == "loess" or o.get("contours") or o.get("z_scenario") or o.get("diverging"):
            return _FIG_SURFACE.substitute(
                Z=z, RZ=_rs(z), SEL=str(o.get("z_scenario") or ""), CONTOURS=_rnums(o.get("contours")),
                DIVERGING="TRUE" if o.get("diverging") else "FALSE", BOOTB=bootB, **common), None
        return _FIG_HEATMAP.substitute(Z=z, RZ=_rs(z), **common), None
    if ftype == "line" and (smooth == "loess" or o.get("band") or o.get("series")):
        series = o.get("series") or ""
        return _FIG_LINE_SMOOTH.substitute(
            SERIES=series, SERIES_NOTE=(f", by {series}" if series else ""),
            HLINES=_rnums(o.get("hlines")), BOOTB=bootB, RS=_rs(series), **common), None
    if ftype in ("line", "point", "scatter"):
        if ftype == "line":
            facet = o.get("facet")
            fnote = f" faceted by {facet}" if facet else ""
            fby = f", cf=d[[\"{facet}\"]]" if facet else ""
            faes = ", colour=factor(cf)" if facet else ""
            flab = f", colour={_rs(facet)}" if facet else ""
            return _FIG_LINE.substitute(FACET_NOTE=fnote, FACET_BY=fby, FACET_AES=faes,
                                        FACET_LAB=flab, **common), None
        return _FIG_SCATTER.substitute(**common), None
    if ftype == "bar":
        return _FIG_BAR.substitute(**common), None
    return None, f"unknown figure type '{ftype}'"


def generate_script(eid: str, exp: dict, param_cols: list, metric_cols: list, results: Path):
    """Return (script_text, manifest). manifest = ordered list of produced artifacts:
    {kind, idx, caption, name, primary: Path|None, files: [Path], note}."""
    figdir, tabdir, stadir = results / "figures", results / "tables", results / "analysis" / "stats"
    text = _HEADER.substitute(EID=eid, METRICS=_rvec(metric_cols))
    text += _HELPERS.replace("__PARAMCOLS__", _rvec(param_cols))    # continuous-surface toolkit
    known = set(param_cols) | set(metric_cols)
    manifest = [{"kind": "summary", "idx": None,
                 "caption": "Summary statistics (per metric, computed in R)", "name": "summary",
                 "primary": stadir / f"{eid}_summary.csv", "files": [stadir / f"{eid}_summary.csv"]}]

    for i, o in enumerate(exp.get("outputs") or []):
        kind = o.get("kind")
        cap = str(o.get("caption") or "").strip()
        if kind == "figure":
            stem = f"{eid}_{i}_{_slug(cap or o.get('type', 'fig'))}"
            png, pdf = f"{stem}.png", f"{stem}.eps"
            block, err = _figure_block(o, i, o.get("x"), o.get("y"), png, pdf)
            if err:
                manifest.append({"kind": "skipped", "idx": i, "caption": cap, "name": "",
                                 "primary": None, "files": [], "note": err})
                continue
            text += block
            manifest.append({"kind": "figure", "idx": i, "caption": cap or f"figure {i}", "name": "",
                             "primary": figdir / png, "files": [figdir / png, figdir / pdf]})
        elif kind == "table":
            refs = {"cell": o.get("cell"), "rows": o.get("rows"), "cols": o.get("cols")}
            bad = {k: v for k, v in refs.items()          # a ref must be a single existing column
                   if v is not None and not (isinstance(v, str) and v in known)}
            if bad:                                       # references a non-column (e.g. a derived slice)
                badstr = ", ".join(f"{k}={v!r}" for k, v in bad.items())
                cols = ", ".join(sorted(known)) or "none"
                manifest.append({"kind": "skipped", "idx": i, "caption": cap, "name": "",
                                 "primary": None, "files": [],
                                 "note": f"table {badstr} is not a data column; needs a code.analysis_r "
                                         f"script (known columns: {cols})"})
                continue
            stem = f"{eid}_{i}_{_slug(cap or 'table')}"
            csv = f"{stem}.csv"
            text += _TABLE.substitute(I=i, CELL=o.get("cell"), ROWS=o.get("rows"),
                                      COLS=o.get("cols"), CSV=csv)
            manifest.append({"kind": "table", "idx": i, "caption": cap or f"table {i}", "name": "",
                             "primary": tabdir / csv, "files": [tabdir / csv]})
        elif kind == "regression":
            formula = o.get("formula")
            name = o.get("name") or f"reg{i}"
            if not formula:
                manifest.append({"kind": "skipped", "idx": i, "caption": cap, "name": name,
                                 "primary": None, "files": [], "note": "regression needs a `formula:`"})
                continue
            stem = f"{eid}_{i}_{_slug(name)}_regression"
            csv = f"{stem}.csv"
            fam = o.get("family")
            fit = "glm" if fam else "lm"
            fam_arg = f", family={fam}" if fam else ""
            text += _REGRESSION.substitute(I=i, FORMULA=formula, FIT=fit, FAMILY=fam_arg, CSV=csv)
            manifest.append({"kind": "regression", "idx": i,
                             "caption": cap or f"Regression: {formula}", "name": name,
                             "primary": stadir / csv, "files": [stadir / csv]})
        elif kind == "comparison":
            # cross-dataset overlay (e.g. this cycle vs a prior one) — inherently not derivable
            # from this experiment's cells alone; author it in a code.analysis_r script.
            manifest.append({"kind": "unrendered", "idx": i, "caption": cap,
                             "name": o.get("name") or o.get("against") or "",
                             "primary": None, "files": [],
                             "note": "comparison (needs a code.analysis_r script — cross-dataset)"})
        else:
            # stat / artifact — rayleigh can't derive it from cell data; the report names it.
            manifest.append({"kind": "unrendered", "idx": i, "caption": cap,
                             "name": o.get("name") or "", "primary": None, "files": [],
                             "note": kind or "(no kind)"})
    return text, manifest


def _run(script_path: Path, results: Path):
    """Run the R script. Returns (ran, ok, combined_log)."""
    if shutil.which("Rscript") is None:
        return False, False, "Rscript not on PATH"
    try:
        r = subprocess.run(["Rscript", str(script_path)], cwd=str(results),
                           capture_output=True, text=True, timeout=1800)
        out = (r.stdout or "") + (r.stderr or "")
        return True, (r.returncode == 0), out.strip()
    except Exception as e:                               # noqa: BLE001
        return True, False, f"{type(e).__name__}: {e}"


def analyze(eid: str, exp: dict, df, param_cols: list, metric_cols: list,
            results: Path, custom_r: str | None = None) -> dict:
    """Write the tidy data + the R script, run it, and collect what it produced.

    Returns {figures, tables, table_files, summary_df, script, data, ran, ok, log, unrendered}.
    `figures` = [(png, [png,eps], caption)]; `tables` = [(DataFrame, caption)] (preregistered
    tables, regressions, and the summary stats — all computed in R); `table_files` = [(csv, cap)].
    """
    import pandas as pd
    andir = results / "analysis"
    (andir / "data").mkdir(parents=True, exist_ok=True)
    (andir / "stats").mkdir(parents=True, exist_ok=True)
    (results / "figures").mkdir(parents=True, exist_ok=True)
    (results / "tables").mkdir(parents=True, exist_ok=True)

    data_path = andir / "data" / f"{eid}.csv"
    df.to_csv(data_path, index=False)                    # the tidy per-cell record R reads

    script_path = andir / f"{eid}.R"
    if custom_r:
        src = (results / custom_r).resolve()
        if not src.is_file():
            return {"figures": [], "tables": [], "table_files": [], "summary_df": None,
                    "script": None, "data": data_path, "ran": False, "ok": False,
                    "log": f"analysis_r '{custom_r}' not found", "unrendered": []}
        manifest = []                                    # custom script: collect by filename glob
        if src != script_path:
            shutil.copyfile(src, script_path)
    else:
        text, manifest = generate_script(eid, exp, param_cols, metric_cols, results)
        script_path.write_text(text)

    ran, ok, rlog = _run(script_path, results)

    figures, tables, table_files, unrendered = [], [], [], []

    def _read_table(csv: Path):
        try:
            return pd.read_csv(csv, index_col=0)
        except Exception:                                # noqa: BLE001
            return None

    if custom_r:                                         # convention-based collection
        for png in sorted((results / "figures").glob(f"{eid}_*.png")):
            figures.append((png, [png, png.with_suffix(".eps")], png.stem))
        for csv in sorted((results / "tables").glob(f"{eid}_*.csv")):
            t = _read_table(csv)
            if t is not None:
                tables.append((t, csv.stem)); table_files.append((csv, csv.stem))
        summary_df = None
    else:
        for m in manifest:
            if m["kind"] == "figure" and m["primary"].exists():
                figures.append((m["primary"], [f for f in m["files"] if f.exists()], m["caption"]))
            elif m["kind"] in ("table", "regression", "summary") and m["primary"].exists():
                t = _read_table(m["primary"])
                if t is not None:
                    tables.append((t, m["caption"])); table_files.append((m["primary"], m["caption"]))
            elif m["kind"] == "unrendered":
                unrendered.append((m["note"], m["name"], m["caption"]))
            elif m["kind"] == "skipped":
                log(f"  {eid} out {m['idx']}: {m['note']} — skipped")
        sfile = andir / "stats" / f"{eid}_summary.csv"
        summary_df = _read_table(sfile) if sfile.exists() else None

    return {"figures": figures, "tables": tables, "table_files": table_files,
            "summary_df": summary_df, "script": script_path, "data": data_path,
            "ran": ran, "ok": ok, "log": rlog, "unrendered": unrendered}
