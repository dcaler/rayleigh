"""rayleigh — experiment designer & runner in the ra* tool family.

rayleigh takes a codebase (usually a raster-built model package under code/),
designs and conducts experiments against it, and delivers to results/ a written
account of the experiments and their results, with figures. Siblings: rabbitHole
(literature review -> litReview/), raconteur (paper drafting -> paper/), and
raster (offline-first, test-driven code builder -> code/).

Verbs:
  rayleigh init            scaffold results/, open/roll a research cycle, and run the
                           interactive prereg/design session -> designdocs/experiments.yaml
  rayleigh conduct_exp <E> expand an experiment's design into cells and run them against
                           code/ (restartable, provenance)
  rayleigh process_outputs reduce data -> the preregistered outputs -> findings -> the
                           datestamped .docx write-up
  rayleigh queue           linearize experiments.yaml -> a trundlr chain (conduct_exp per
                           experiment, then process_outputs) for running at scale

The atomic unit is the experiment (question + design + metric + outputs = one finding);
a sweep is the common interior shape of an experiment's design.
"""

__version__ = "0.1.0"
