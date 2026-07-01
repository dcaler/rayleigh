# rayleigh

An **experiment designer & runner**. Point it at a codebase (usually a raster-built model
package under `code/`); rayleigh helps you *design preregistered experiments* against it,
*conduct* them, and *write up* the results with figures — delivered to `results/`.

> The fourth member of the `ra*` family, alongside
> [rabbitHole](https://github.com/dcaler/rabbithole) (literature review → `litReview/`),
> [raconteur](https://github.com/dcaler/raconteur) (paper drafting → `paper/`), and
> [raster](https://github.com/dcaler/raster) (test-driven code builder → `code/`).
> rayleigh reads their output when designing a cycle, and its `results/` feed back into
> raconteur's paper.

```
rayleigh init             ▸ scaffold results/, open/roll a research cycle, and run the
                            interactive design session → designdocs/experiments.yaml
rayleigh conduct_exp <E>  ▸ expand an experiment's design into cells and run them against
                            code/ (restartable, provenance)
rayleigh process_outputs  ▸ reduce data → the preregistered outputs → findings →
                            the datestamped .docx write-up
rayleigh queue            ▸ linearize experiments.yaml → a trundlr chain, for running
                            at scale off your laptop
```

rayleigh lives at `github.com/dcaler/rayleigh`. Each project it works on has its own
`code/` (the codebase under test) and gets a `results/` working folder.

---

## Install

```bash
git clone https://github.com/dcaler/rayleigh.git
cd rayleigh
pip install -e .
```

Requirements: Python ≥ 3.11, PyYAML. Optional: `claude` on PATH (for the interactive
design session).

## Machine setup

First run writes `~/.config/rayleigh/config.toml` (the PII boundary): the author identity
stamped into the `.docx` write-up and the initials used by the document-revision naming
chain (`ra` = tool, e.g. `DCR` = human reviewer). Personal details live only here and never
enter a project. It also sets `[models] design` — the model `init` launches the design
session on (default `opus`, since preregistering experiments + authoring the run-adapter is
the pipeline's highest-reasoning step; use `fable` for the hardest, `sonnet` for lighter).

## Core ideas

- **The experiment is the atomic unit** — one question + one design + one metric + planned
  outputs = one finding. A **sweep** is just the most common shape of an experiment's
  `design`; below it, the design expands to **cells** (one parameter combo × one seed),
  the parallel work items.
- **rayleigh does not reimplement the model.** `code/` owns "run one config → one output";
  the *sweep* (which cells to run) is rayleigh's reason to exist.
- **Preregistration is a conversation, not a gate.** `init` fixes each experiment's
  question, metric, expected direction, and planned outputs *before* data exists — with
  heavy human involvement. rayleigh never refuses to run and never auto-judges; honesty
  lives in **reporting** (`mode: confirmatory | exploratory`, support/refute stated plainly).
- **Iteration = sequential cycles.** A `{YYMMDD}` datestamp marks a research direction;
  `rayleigh init --new-cycle` opens a new one and archives the prior.

## Start a cycle

Run **from the project root** (alongside `code/`, and any `paper/` / `litReview/`):

```bash
cd ~/work/260623_myproject
rayleigh init                 # scaffold results/, index priors, launch the design session
rayleigh init --no-launch     # scaffold only; print the playbook path to drive it yourself
rayleigh init --new-cycle     # roll to a fresh {YYMMDD} cycle, archiving the prior one
```

`init` scaffolds `results/` (a working folder — *not* its own git repo):

```
results/
  rayleigh.yaml               # machinery: code/ location, cycle, brief (git-ignored)
  designdocs/
    PLANNING.md               # the design playbook the Claude session follows
    EXPERIMENTS.md            # human-facing design doc (authored in the session)
    experiments.yaml          # the executable spec (authored in the session)
    PROGRESS.md               # experiment status mirror
  data/                       # conduct_exp writes here
  figures/                    # process_outputs writes here
  archive/<cycle>/            # --new-cycle moves the prior cycle here
```

Then the interactive session reads `designdocs/PLANNING.md`, inspects `code/` to establish
the run-adapter, and preregisters this cycle's experiments with you into `experiments.yaml`.

### Designing from the prior ra* work

rayleigh rarely starts cold — the earlier tools have usually left a raster-built `code/`
(with `designdocs/` + `configs/`), a rabbitHole `litReview/`, and a raconteur `paper/`.
`init` **indexes all of it** into `designdocs/PRIORS.md` and, when you don't supply a brief,
derives one from `code/raster.yaml`. The design session opens `PRIORS.md` first and
**proposes a starting experiment set** from it — `code/configs/` suggest the sweep axes and
baselines, `litReview/` the expected directions, `paper/` which questions matter — which you
then refine, rather than filling a blank skeleton.

## Conduct experiments

Once `experiments.yaml` has experiments (from `init`):

```bash
rayleigh conduct_exp E1 --dry-run   # list the cells; run nothing
rayleigh conduct_exp E1             # expand the design → cells, run each against code/
rayleigh conduct_exp E1 --workers 32 --limit 8   # parallelism + smoke-test a few cells
```

`conduct_exp` expands an experiment's `design` (a `sweep` over axes, or named `conditions`)
into cells — one parameter combo × one seed — and invokes `code/` per the
`code.run_adapter` (either an in-process `import` entrypoint or a `subprocess` command). A
cell may write **one artifact** (`output_template:`) or **several** (`outputs: {timeseries: …,
summary: …}` — a run counts as done only when all exist; `process_outputs` merges them,
prefixing metric keys by name). It's **restartable** (skips cells whose outputs already
exist) and writes a provenance sidecar (`*.prov.json`: code git SHA, params, seed, rayleigh
version) per cell, plus a `data/<E>/_status.json` summary.

## Write up the results

Once cells have data (from `conduct_exp`):

```bash
rayleigh process_outputs --dry-run     # per experiment: data availability + planned outputs
rayleigh process_outputs               # render figures/tables → RESULTS.md + the .docx
rayleigh process_outputs --experiment E1 --no-docx   # one experiment; skip the .docx
```

`process_outputs` re-expands each experiment's cells, loads their outputs (JSON/parquet/csv
by default, or a `code.output_adapter.load` callable), aggregates over seeds, and renders the
outputs preregistered in `init` — figures (line/bar/scatter/heatmap) into `results/figures/`
and pivot tables — with an honest **finding** (observed summary next to the preregistered
`expected_direction`, labeled `confirmatory`/`exploratory`, no auto-verdict). It writes
`results/RESULTS.md` and the datestamped `results/{cycle}_{project}_results_ra.docx` (that
`.docx` enters your ra/DCR annotation cycle; degrades to Markdown-only if python-docx is absent).

## Run at scale (trundlr)

Local `conduct_exp` + `process_outputs` cover small/medium experiments. For big
(elephantRoom-size) sweeps you don't want pinning your laptop, `queue` offloads to trundlr:

```bash
rayleigh queue --dry-run     # preview the chain, submit nothing
rayleigh queue               # submit conduct_exp E1 → E2 → … → process_outputs
```

It builds a flat, single-parent chain — one `conduct_exp` node per experiment, then a final
`process_outputs` — and each `conduct_exp` node still fans its cells out locally (its own
`ProcessPoolExecutor`) on the machine trundlr assigns it. So the coarse experiment chain
rides trundlr while the cell fan-out stays local.

Each experiment carries its own scheduling knobs, set during `init`:

- **`resource: cpu | gpu`** — which trundlr resource runs *this* experiment's `conduct_exp`
  (default `cpu`; most sims here are CPU-bound). `process_outputs` is always CPU.
- **`workers`** — how many cells run in parallel, sized to the target machine so it doesn't
  overload cores/RAM (`≤ cores`, and `workers × per-run RAM ≤ usable RAM`).
- **`budget_hours`** — the trundlr scheduling window.

The `trundlr:` block in `results/rayleigh.yaml` holds the resource ids (`{gpu, cpu}`) and
`project_id` (defaults from `~/.config/rayleigh/config.toml`); a name-form `project_id` is
resolved to a numeric id and cached on first `queue`.

## Status

All four verbs are implemented: `init` (design), `conduct_exp` (run), `process_outputs`
(write up), and `queue` (submit the chain to trundlr for scale). The full loop —
design → conduct → write up → `.docx` — works end to end, locally or on the cluster.
