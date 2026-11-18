"""`fde-kit` command line: eval, serve, demo."""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

from fde_kit.config import get_settings
from fde_kit.evals import LLMJudge, load_golden, run_eval
from fde_kit.llm import get_llm
from fde_kit.observability import configure_logging

DEFAULT_AGENT = "examples.hello_agent.agent:run"


def load_agent(spec: str):
    sys.path.insert(0, str(Path.cwd()))  # let customers keep agents next to their repo
    module, _, attr = spec.partition(":")
    return getattr(importlib.import_module(module), attr or "run")


def cmd_eval(args: argparse.Namespace) -> int:
    cases = load_golden(args.golden)
    judge = LLMJudge(get_llm()) if args.judge else None
    report = run_eval(load_agent(args.agent), cases, judge=judge, threshold=args.threshold)
    print(report.to_markdown())
    if args.json_out:
        Path(args.json_out).write_text(report.to_json())
    if not report.ok:
        print(f"\nFAIL: accuracy {report.accuracy:.1%} < threshold {args.threshold:.1%}")
        return 1
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("fde_kit.server:create_app", factory=True, host=args.host, port=args.port)
    return 0


def cmd_demo(_: argparse.Namespace) -> int:
    sys.path.insert(0, str(Path.cwd()))
    from examples.hello_agent.demo import main

    return main()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fde-kit")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("eval", help="run an agent over a JSONL golden set")
    p.add_argument("--golden", required=True)
    p.add_argument("--threshold", type=float, default=0.9)
    p.add_argument("--agent", default=DEFAULT_AGENT, help="module:function (dict -> dict)")
    p.add_argument("--judge", action="store_true", help="add an LLM judge on top of the checks")
    p.add_argument("--json-out", help="also write the JSON report here")
    p.set_defaults(fn=cmd_eval)

    p = sub.add_parser("serve", help="run the webhook + review-queue service")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("demo", help="run the hello_agent end to end")
    p.set_defaults(fn=cmd_demo)

    args = parser.parse_args(argv)
    quiet = "WARNING" if args.cmd == "eval" else get_settings().log_level
    configure_logging(os.environ.get("LOG_LEVEL") or quiet)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
