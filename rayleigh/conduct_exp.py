"""`rayleigh conduct_exp <E>` — run one experiment's cells against code/.  [Session 2]

Contract (pinned; implemented in a follow-up session):
  - Read results/designdocs/experiments.yaml; locate experiment <E>.
  - Expand E.design into a flat list of CELLS (cartesian product of axes × seeds; or the
    conditions/oat/ablation shape). Each cell is one parameter combo × one seed.
  - For each cell whose output does not already exist (restartable / skip-existing), invoke
    code/ per `code.run_adapter`:
      kind: import      -> import "module:callable" and call it with the cell's config
      kind: subprocess  -> run the `command` template, substituting {config}/{out}/…
    dispatching up to `workers` (experiment override or run_adapter default) in parallel.
  - Write each output to `run_adapter.output_template`, and stamp provenance alongside it
    (code git SHA, the resolved config, the seed, and the rayleigh version).
  - Update the experiment's row in PROGRESS.md (🟦 running → ✅ conducted / ❌ failed).

rayleigh reimplements none of the model — the actual run is entirely code/'s.
"""


def run_conduct_exp(args) -> int:
    print(f"rayleigh conduct_exp {args.experiment}: not yet implemented — Session 2.")
    print("  (design experiments first with `rayleigh init`; this runs their cells against code/.)")
    return 0
