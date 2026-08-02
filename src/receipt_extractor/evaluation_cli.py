"""Offline command-line interface for synthetic evaluator calibration."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from receipt_extractor.evaluation import (
    EvaluationError,
    evaluate_suite,
    evaluation_report_json,
    evaluation_report_text,
    load_evaluation_report,
    load_evaluation_suite,
    verify_evaluation_report,
)

_VERIFY_SUCCESS = (
    json.dumps(
        {
            "mode": "verify-evaluation",
            "schema_version": 1,
            "verified": True,
        },
        allow_nan=False,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    )
    + "\n"
)
_EVALUATION_FAILURE = (
    "evaluation failed; details are suppressed to avoid leaking suite data\n"
)
_VERIFICATION_FAILURE = (
    "evaluation verification failed; details are suppressed to avoid "
    "leaking suite data\n"
)
_OUTPUT_FAILURE = "evaluation output failed; details are suppressed\n"


class _HelpFormatter(argparse.HelpFormatter):
    def __init__(self, prog: str) -> None:
        super().__init__(prog, max_help_position=28, width=88)


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(2, "receipt-evaluator: error: invalid arguments\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="receipt-evaluator",
        allow_abbrev=False,
        formatter_class=_HelpFormatter,
        description=(
            "Evaluate or verify a repository-authored synthetic negative "
            "control entirely offline."
        ),
    )
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_ArgumentParser,
    )

    evaluate = commands.add_parser(
        "evaluate",
        allow_abbrev=False,
        formatter_class=_HelpFormatter,
        help="evaluate one content-addressed synthetic suite",
    )
    evaluate.add_argument("suite", type=Path, metavar="SUITE.json")
    evaluate.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="stdout format (default: json)",
    )

    verify = commands.add_parser(
        "verify",
        allow_abbrev=False,
        formatter_class=_HelpFormatter,
        help="recompute and verify an aggregate report against its exact suite",
    )
    verify.add_argument("suite", type=Path, metavar="SUITE.json")
    verify.add_argument("report", type=Path, metavar="REPORT.json")
    return parser


def _write_stdout(parser: argparse.ArgumentParser, payload: str) -> None:
    try:
        written = sys.stdout.write(payload)
        if written != len(payload):
            raise OSError("stdout accepted only part of the evaluation output")
        sys.stdout.flush()
    except (OSError, ValueError):
        parser.exit(1, _OUTPUT_FAILURE)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one offline evaluation command and return its process status."""

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "evaluate":
        try:
            suite = load_evaluation_suite(args.suite)
            report = evaluate_suite(suite)
        except EvaluationError:
            parser.exit(2, _EVALUATION_FAILURE)
        try:
            output = (
                evaluation_report_json(report)
                if args.format == "json"
                else evaluation_report_text(report)
            )
        except EvaluationError:
            parser.exit(1, _OUTPUT_FAILURE)
        _write_stdout(parser, output)
        return 0

    try:
        suite = load_evaluation_suite(args.suite)
        report = load_evaluation_report(args.report)
        verify_evaluation_report(suite=suite, report=report)
    except EvaluationError:
        parser.exit(2, _VERIFICATION_FAILURE)
    _write_stdout(parser, _VERIFY_SUCCESS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
