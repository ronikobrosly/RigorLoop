"""The argparse surface: init/check/report and project-loading errors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rigorloop.core.types import NOTHING
from rigorloop.shell.cli import execute_run, main
from tests.conftest import BASE_CONFIG, Recorder, make_project, scripted_agent

pytestmark = pytest.mark.integration


class TestInit:
    def test_scaffolds_a_runnable_project(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--dir", str(tmp_path), "init"]) == 0
        assert (tmp_path / "rigorloop.toml").is_file()
        assert (tmp_path / "task.md").is_file()
        lines = (tmp_path / "examples.jsonl").read_text().splitlines()
        assert len(lines) >= 20
        record = json.loads(lines[0])
        assert set(record) == {"input", "expected_output"}
        # The scaffold must itself pass `rigorloop check`.
        assert main(["--dir", str(tmp_path), "check"]) == 0
        out = capsys.readouterr().out
        assert "Agent-call budget estimate" in out

    def test_refuses_to_overwrite(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (tmp_path / "task.md").write_text("precious")
        assert main(["--dir", str(tmp_path), "init"]) == 1
        assert (tmp_path / "task.md").read_text() == "precious"
        assert "refusing to overwrite" in capsys.readouterr().out


class TestCheck:
    def test_reports_splits_warnings_and_budget(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        make_project(tmp_path)
        assert main(["--dir", str(tmp_path), "check"]) == 0
        out = capsys.readouterr().out
        assert "dev 18 / validation 6 / test 6" in out
        assert "can only distinguish" in out  # power warning at n=6
        assert "No tokens were spent" in out

    def test_missing_config(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--dir", str(tmp_path), "check"]) == 1
        assert "rigorloop init" in capsys.readouterr().out

    def test_bad_config(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        make_project(tmp_path)
        (tmp_path / "rigorloop.toml").write_text("this is not toml [")
        assert main(["--dir", str(tmp_path), "check"]) == 1
        assert "not valid TOML" in capsys.readouterr().out

    def test_missing_task_and_examples(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        make_project(tmp_path)
        (tmp_path / "task.md").unlink()
        assert main(["--dir", str(tmp_path), "check"]) == 1
        assert "task description" in capsys.readouterr().out

        make_project(tmp_path / "second")
        (tmp_path / "second" / "examples.jsonl").unlink()
        assert main(["--dir", str(tmp_path / "second"), "check"]) == 1
        assert "examples file" in capsys.readouterr().out

    def test_malformed_examples(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        make_project(tmp_path)
        (tmp_path / "examples.jsonl").write_text("garbage\n")
        assert main(["--dir", str(tmp_path), "check"]) == 1
        assert "not valid JSON" in capsys.readouterr().out

    def test_too_few_examples(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        make_project(tmp_path)
        (tmp_path / "examples.jsonl").write_text('{"input": "a", "expected_output": "b"}\n')
        assert main(["--dir", str(tmp_path), "check"]) == 1
        assert "too few" in capsys.readouterr().out


class TestReport:
    def test_rerenders_a_finalized_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        project = make_project(tmp_path)
        recorder = Recorder(agent_handler=scripted_agent)
        assert execute_run(project, tmp_path, recorder.deps(), NOTHING) == 0

        report_path = tmp_path / "runs" / "run-test" / "final" / "report.md"
        original = report_path.read_text()
        report_path.unlink()
        assert main(["--dir", str(tmp_path), "report", "run-test"]) == 0
        assert report_path.read_text() == original
        assert "RigorLoop report" in capsys.readouterr().out

    def test_unknown_run(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--dir", str(tmp_path), "report", "nope"]) == 1
        assert "no finalized results" in capsys.readouterr().out


class TestVersion:
    def test_version_flag(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["--version"])
        assert excinfo.value.code == 0
        assert "rigorloop" in capsys.readouterr().out


def test_base_config_matches_readme_defaults() -> None:
    """BASE_CONFIG intentionally overrides loop knobs; the untouched ones must
    keep their documented defaults so tests exercise real default paths."""
    from rigorloop.core import config_calcs
    from rigorloop.core.types import Ok

    parsed = config_calcs.parse_config(BASE_CONFIG)
    assert isinstance(parsed, Ok)
    assert parsed.value.loop.max_consecutive_eval_failures == 5
    assert parsed.value.loop.strategy_full_detail_loops == 4
    assert parsed.value.validation.cohort_size == 2
