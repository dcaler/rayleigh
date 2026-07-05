"""rayleigh CLI — `rayleigh <init|conduct_exp|process_outputs|review|queue>`."""

import argparse

from rayleigh import __version__


def _common(p):
    p.add_argument("--dir", help="project root (default: cwd)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="rayleigh",
        description="Design, conduct, and write up experiments against a codebase.")
    ap.add_argument("--version", action="version", version=f"rayleigh {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser(
        "init",
        help="scaffold results/, open/roll a research cycle, and run the interactive design session")
    _common(init)
    init.add_argument("--name", help="project name (default: derived from the working-dir name)")
    init.add_argument("--brief", help="long-form research brief (else prompted)")
    init.add_argument("--new-cycle", action="store_true",
                      help="start a fresh {YYMMDD} research cycle, archiving the prior one")
    init.add_argument("--no-launch", action="store_true",
                      help="scaffold only; print the playbook path instead of launching claude")

    conduct = sub.add_parser("conduct_exp", help="run one experiment's cells against code/")
    _common(conduct)
    conduct.add_argument("experiment", help="experiment id, e.g. E1")
    conduct.add_argument("--dry-run", action="store_true",
                         help="list the cells and what would run; execute nothing")
    conduct.add_argument("--workers", type=int, default=0,
                         help="parallel workers (default: experiment/run_adapter setting)")
    conduct.add_argument("--limit", type=int, default=0,
                         help="run at most N not-yet-done cells this pass (smoke testing)")
    conduct.add_argument("--no-mem-guard", action="store_true",
                         help="skip the pilot-measured RAM sizing + per-worker memory ceiling "
                              "(default: on). Only use if you have sized workers yourself.")

    process = sub.add_parser("process_outputs",
                             help="reduce data -> preregistered outputs -> the .docx write-up")
    _common(process)
    process.add_argument("--experiment", help="process only this experiment id (default: all)")
    process.add_argument("--no-docx", action="store_true",
                         help="write RESULTS.md + figures only; skip the .docx")
    process.add_argument("--dry-run", action="store_true",
                         help="report data availability + planned outputs; render nothing")

    review = sub.add_parser(
        "review",
        help="human-led review gate after process_outputs (an interactive human+Claude session)")
    _common(review)
    review.add_argument("--no-launch", action="store_true",
                        help="scaffold REVIEW.md only; print the checklist path instead of launching claude")

    queue = sub.add_parser("queue",
                           help="linearize experiments.yaml -> submit the trundlr run chain")
    _common(queue)
    queue.add_argument("--dry-run", action="store_true", help="print the chain, create nothing")
    queue.add_argument("--exec-cmd", help="command the trundlr runner invokes (default: rayleigh)")

    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "init":
        from rayleigh.init import run_init
        return run_init(args)
    if args.cmd == "conduct_exp":
        from rayleigh.conduct_exp import run_conduct_exp
        return run_conduct_exp(args)
    if args.cmd == "process_outputs":
        from rayleigh.process_outputs import run_process_outputs
        return run_process_outputs(args)
    if args.cmd == "review":
        from rayleigh.review import run_review
        return run_review(args)
    if args.cmd == "queue":
        from rayleigh.queue import run_queue
        return run_queue(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
