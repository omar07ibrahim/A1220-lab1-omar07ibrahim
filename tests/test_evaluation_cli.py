from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from receipt_extractor import evaluation_cli
from receipt_extractor.evaluation import (
    EvaluationReport,
    EvaluationSuite,
    evaluate_suite,
    evaluation_report_json,
    evaluation_report_text,
    evaluation_suite_json,
)
from tests.test_cli import CliResult
from tests.test_evaluation_io import _suite, _write_bundle

_VERIFY_STDOUT = (
    '{\n  "mode": "verify-evaluation",\n  "schema_version": 1,\n  "verified": true\n}\n'
)
_EVALUATION_FAILURE = (
    "evaluation failed; details are suppressed to avoid leaking suite data\n"
)
_VERIFICATION_FAILURE = (
    "evaluation verification failed; details are suppressed to avoid "
    "leaking suite data\n"
)
_ARGUMENT_FAILURE = "receipt-evaluator: error: invalid arguments\n"


def invoke(*arguments: str) -> CliResult:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = evaluation_cli.main(arguments)
        except SystemExit as error:
            code = error.code if isinstance(error.code, int) else 1
    return CliResult(code=code, stdout=stdout.getvalue(), stderr=stderr.getvalue())


def _write_suite(path: Path, suite: EvaluationSuite) -> bytes:
    encoded = evaluation_suite_json(suite).encode("ascii")
    path.write_bytes(encoded)
    return encoded


def test_evaluate_json_is_default_canonical_and_presentation_independent(
    tmp_path: Path,
) -> None:
    suite = _suite()
    report = evaluate_suite(suite)
    pretty_path = tmp_path / "pretty.json"
    compact_path = tmp_path / "compact.json"
    pretty_bytes = _write_suite(pretty_path, suite)
    compact_bytes = json.dumps(
        suite.model_dump(mode="json"),
        separators=(",", ":"),
    ).encode("ascii")
    compact_path.write_bytes(compact_bytes)

    first = invoke("evaluate", str(pretty_path))
    second = invoke("evaluate", str(pretty_path), "--format", "json")
    compact = invoke("evaluate", str(compact_path), "--format", "json")

    expected = evaluation_report_json(report)
    assert first == second == compact == CliResult(0, expected, "")
    assert json.loads(first.stdout) == report.model_dump(mode="json")
    assert first.stdout.endswith("\n") and not first.stdout.endswith("\n\n")
    assert pretty_path.read_bytes() == pretty_bytes
    assert compact_path.read_bytes() == compact_bytes


def test_evaluate_text_is_exact_deterministic_and_aggregate_only(
    tmp_path: Path,
) -> None:
    suite = _suite()
    report = evaluate_suite(suite)
    suite_path = tmp_path / "suite.json"
    _write_suite(suite_path, suite)

    first = invoke("evaluate", str(suite_path), "--format", "text")
    second = invoke("evaluate", str(suite_path), "--format", "text")

    assert first == second == CliResult(0, evaluation_report_text(report), "")
    assert "Authored negative-control calibration (not model accuracy)" in first.stdout
    assert "field_agreement: 3/4" in first.stdout
    assert "exact_records: 0/1" in first.stdout
    assert "record_exact_field_histogram (bins 0..4): 0, 0, 0, 1, 0" in first.stdout
    assert "Other -> Other: 1" in first.stdout
    for forbidden in (
        "Synthetic Truth",
        "Synthetic Candidate",
        "synthetic-case",
        "2026-07-24",
        "$12.34",
        "%",
        "\x1b",
    ):
        assert forbidden not in first.stdout


def test_verify_emits_only_fixed_success_json_and_loads_each_artifact_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite_path, report_path, _, _ = _write_bundle(tmp_path)
    real_suite_loader = cast(
        Callable[[Path], EvaluationSuite],
        vars(evaluation_cli)["load_evaluation_suite"],
    )
    real_report_loader = cast(
        Callable[[Path], EvaluationReport],
        vars(evaluation_cli)["load_evaluation_report"],
    )
    loaded: list[tuple[str, Path]] = []

    def load_suite(path: Path) -> EvaluationSuite:
        loaded.append(("suite", path))
        return real_suite_loader(path)

    def load_report(path: Path) -> EvaluationReport:
        loaded.append(("report", path))
        return real_report_loader(path)

    monkeypatch.setattr(evaluation_cli, "load_evaluation_suite", load_suite)
    monkeypatch.setattr(evaluation_cli, "load_evaluation_report", load_report)

    result = invoke("verify", str(suite_path), str(report_path))

    assert result == CliResult(0, _VERIFY_STDOUT, "")
    assert loaded == [("suite", suite_path), ("report", report_path)]
    assert set(json.loads(result.stdout)) == {"mode", "schema_version", "verified"}
    assert "sha256:" not in result.stdout


def test_runtime_failures_are_fixed_redacted_and_stdout_free(tmp_path: Path) -> None:
    private_suite = tmp_path / "PRIVATE-PATH-suite.json"
    private_suite.write_text(
        '{"vendor":"PRIVATE-VENDOR","api_key":"PRIVATE-KEY"}',
        encoding="ascii",
    )
    evaluation = invoke("evaluate", str(private_suite), "--format", "text")

    assert evaluation == CliResult(2, "", _EVALUATION_FAILURE)
    for secret in (
        "PRIVATE-PATH",
        "PRIVATE-VENDOR",
        "PRIVATE-KEY",
        "Traceback",
        str(tmp_path),
    ):
        assert secret not in evaluation.stderr

    suite_path, _, _, _ = _write_bundle(tmp_path)
    other_report = evaluate_suite(_suite(candidate_vendor="Different Candidate"))
    report_path = tmp_path / "PRIVATE-PATH-report.json"
    report_path.write_text(evaluation_report_json(other_report), encoding="ascii")
    verification = invoke("verify", str(suite_path), str(report_path))

    assert verification == CliResult(2, "", _VERIFICATION_FAILURE)
    assert "PRIVATE-PATH" not in verification.stderr
    assert "sha256:" not in verification.stderr
    assert "Traceback" not in verification.stderr


@pytest.mark.parametrize(
    "arguments",
    (
        (),
        ("PRIVATE-COMMAND",),
        ("evaluate", "suite.json", "--form", "json"),
        ("evaluate", "suite.json", "--format", "PRIVATE-FORMAT"),
        ("verify", "suite.json"),
        ("verify", "suite.json", "report.json", "--format", "json"),
        ("verify", "suite.json", "report.json", "PRIVATE-EXTRA"),
    ),
)
def test_argument_errors_are_redacted_and_happen_before_io(
    arguments: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_load(_path: Path) -> EvaluationSuite:
        raise AssertionError("loader ran before argument rejection")

    monkeypatch.setattr(evaluation_cli, "load_evaluation_suite", fail_load)

    result = invoke(*arguments)

    assert result.code == 2
    assert result.stdout == ""
    assert result.stderr.endswith(_ARGUMENT_FAILURE)
    assert "usage: receipt-evaluator" in result.stderr
    for private in ("PRIVATE-COMMAND", "PRIVATE-FORMAT", "PRIVATE-EXTRA"):
        assert private not in result.stderr


@pytest.mark.parametrize("arguments", (("--help",), ("evaluate", "--help")))
def test_help_layout_is_independent_of_terminal_width(
    arguments: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COLUMNS", "24")
    narrow = invoke(*arguments)
    monkeypatch.setenv("COLUMNS", "240")
    wide = invoke(*arguments)

    assert narrow == wide
    assert narrow.code == 0
    assert narrow.stderr == ""
    assert "usage: receipt-evaluator" in narrow.stdout


def test_double_dash_accepts_a_hyphen_leading_suite_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suite = _suite()
    suite_path = tmp_path / "-suite.json"
    _write_suite(suite_path, suite)
    monkeypatch.chdir(tmp_path)

    result = invoke("evaluate", "--", suite_path.name)

    assert result == CliResult(0, evaluation_report_json(evaluate_suite(suite)), "")


@pytest.mark.parametrize(
    "mode",
    ("write", "closed-write", "partial", "flush", "closed-flush"),
)
def test_output_write_failure_is_redacted_and_exit_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    suite_path = tmp_path / "suite.json"
    _write_suite(suite_path, _suite())

    class ControlledStdout:
        def write(self, _value: str) -> int:
            if mode == "write":
                raise OSError("PRIVATE-WRITER-DETAIL")
            if mode == "closed-write":
                raise ValueError("PRIVATE-CLOSED-WRITER-DETAIL")
            if mode == "partial":
                return len(_value) - 1
            return len(_value)

        def flush(self) -> None:
            if mode == "flush":
                raise OSError("PRIVATE-FLUSH-DETAIL")
            if mode == "closed-flush":
                raise ValueError("PRIVATE-CLOSED-FLUSH-DETAIL")

    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr), monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", ControlledStdout())
        with pytest.raises(SystemExit) as raised:
            evaluation_cli.main(("evaluate", str(suite_path)))

    assert raised.value.code == 1
    assert stderr.getvalue() == "evaluation output failed; details are suppressed\n"
    assert "PRIVATE-WRITER-DETAIL" not in stderr.getvalue()
    assert "PRIVATE-FLUSH-DETAIL" not in stderr.getvalue()
    assert "PRIVATE-CLOSED-WRITER-DETAIL" not in stderr.getvalue()
    assert "PRIVATE-CLOSED-FLUSH-DETAIL" not in stderr.getvalue()


def test_help_evaluate_and_verify_are_offline_with_poison_provider_and_socket(
    tmp_path: Path,
) -> None:
    suite_path, report_path, suite, report = _write_bundle(tmp_path)
    poison = tmp_path / "poison"
    poison.mkdir()
    (poison / "openai.py").write_text(
        "raise AssertionError('provider imported by offline evaluator')\n",
        encoding="ascii",
    )
    (poison / "sitecustomize.py").write_text(
        "import socket\n"
        "def blocked(*args, **kwargs):\n"
        "    raise AssertionError('network boundary crossed')\n"
        "socket.create_connection = blocked\n"
        "socket.socket.connect = blocked\n",
        encoding="ascii",
    )
    repository = Path(__file__).resolve().parents[1]
    base_environment = os.environ.copy()
    base_environment.pop("OPENAI_API_KEY", None)
    base_environment["PYTHONPATH"] = os.pathsep.join(
        [str(poison), str(repository / "src")]
    )

    help_run = subprocess.run(
        [sys.executable, "-m", "receipt_extractor.evaluation_cli", "--help"],
        cwd=repository,
        env=base_environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    evaluation_environment = base_environment.copy()
    evaluation_environment["OPENAI_API_KEY"] = "PRIVATE-UNUSED-KEY"
    evaluated = subprocess.run(
        [
            sys.executable,
            "-m",
            "receipt_extractor.evaluation_cli",
            "evaluate",
            str(suite_path),
            "--format",
            "json",
        ],
        cwd=repository,
        env=evaluation_environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    verified = subprocess.run(
        [
            sys.executable,
            "-m",
            "receipt_extractor.evaluation_cli",
            "verify",
            str(suite_path),
            str(report_path),
        ],
        cwd=repository,
        env=base_environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert help_run.returncode == 0
    assert "evaluate" in help_run.stdout and "verify" in help_run.stdout
    assert help_run.stderr == ""
    assert evaluated.returncode == 0
    assert evaluated.stdout == evaluation_report_json(report)
    assert evaluated.stderr == ""
    assert verified.returncode == 0
    assert verified.stdout == _VERIFY_STDOUT
    assert verified.stderr == ""
    combined = help_run.stdout + evaluated.stdout + verified.stdout
    assert "PRIVATE-UNUSED-KEY" not in combined
    assert json.loads(evaluated.stdout) == evaluate_suite(suite).model_dump(mode="json")
