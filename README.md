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
enter a project.

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
rayleigh init                 # scaffold results/, take the brief, launch the design session
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

## Conduct experiments

Once `experiments.yaml` has experiments (from `init`):

```bash
rayleigh conduct_exp E1 --dry-run   # list the cells; run nothing
rayleigh conduct_exp E1             # expand the design → cells, run each against code/
rayleigh conduct_exp E1 --workers 32 --limit 8   # parallelism + smoke-test a few cells
```

`conduct_exp` expands an experiment's `design` (a `sweep` over axes, or named `conditions`)
into cells — one parameter combo × one seed — and invokes `code/` per the
`code.run_adapter` (either an in-process `import` entrypoint or a `subprocess` command).
It's **restartable** (skips cells whose output already exists) and writes a provenance
sidecar (`*.prov.json`: code git SHA, params, seed, rayleigh version) beside each output,
plus a `data/<E>/_status.json` summary.

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

## Status

All three verbs are implemented: `init` (design), `conduct_exp` (run), `process_outputs`
(write up). The full loop — design → conduct → write up → `.docx` — works end to end.
