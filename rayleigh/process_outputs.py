"""`rayleigh process_outputs` — render preregistered outputs + findings into the .docx.  [Session 3]

Contract (pinned; implemented in a follow-up session):
  - Read results/designdocs/experiments.yaml and the data conduct_exp wrote under results/data/.
  - For each experiment, reduce its data per `metric.reduce`, then render each planned entry
    in its `outputs` list (kind: figure | table | …) into results/figures/ and inline artifacts.
  - State the finding honestly: compare the result to `expected_direction` and report
    support/refute; label `mode: exploratory` experiments as exploratory (never as a
    prediction confirmed). No gate — reporting is where the discipline lives.
  - Assemble the datestamped write-up `results/{YYMMDD}_{project}_results_ra.docx` (the `ra`
    suffix = tool-authored) so it enters the ra/DCR annotation cycle.
  - Update PROGRESS.md (📊 once an experiment's outputs + finding are rendered).
"""


def run_process_outputs(args) -> int:
    print("rayleigh process_outputs: not yet implemented — Session 3.")
    print("  (conduct experiments first with `rayleigh conduct_exp <E>`; this renders their outputs.)")
    return 0
