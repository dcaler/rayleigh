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
                            code/ (restartable, provenance)                 [Session 2]
rayleigh process_outputs  ▸ reduce data → the preregistered outputs → findings →
                            the datestamped .docx write-up                  [Session 3]
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

## Status

`init` is implemented (scaffold + cycle management + the interactive design session).
`conduct_exp` and `process_outputs` are stubbed with their contract pinned — the
`experiments.yaml` schema they consume is authored by `init` and settles first.
