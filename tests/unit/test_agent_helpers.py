"""Tests for agent helpers: JSON extraction, file listing, diff parsing, analyst output."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from mat.agents.analyst import AnalystAgent, list_project_files
from mat.agents.errors import AgentOutputError
from mat.agents.llm import LLMClient, LLMResponse
from mat.agents.parsing import extract_json_object
from mat.agents.programmer import changed_files
from mat.gate.baseline import BaselineMeasurer
from mat.models.contracts import Baseline
from mat.sandbox.docker_runner import DockerRunner
from mat.settings import AgentProfile, SandboxConfig


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('raciocínio... {"a": 2} fim', {"a": 2}),
        ('```json\n{"a": 1}\n```\nfinal:\n```json\n{"a": 3}\n```', {"a": 3}),
        ("sem json aqui", None),
        ("```json\n[1, 2]\n```", None),
        ("{quebrado", None),
    ],
)
def test_extract_json_object(text, expected):
    assert extract_json_object(text) == expected


def test_list_project_files_works_under_hidden_parent(tmp_path):
    root = tmp_path / ".mat" / "worktrees" / "run1"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    (root / "node_modules" / "lib").mkdir(parents=True)
    (root / "node_modules" / "lib" / "index.js").write_text("", encoding="utf-8")
    (root / ".env").write_text("SECRET=1", encoding="utf-8")

    assert list_project_files(root) == ["src/app.py"]


def test_list_project_files_respects_limit(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.py").write_text("", encoding="utf-8")
    assert list_project_files(tmp_path, limit=3) == ["f0.py", "f1.py", "f2.py"]


def test_changed_files_handles_modified_deleted_and_new():
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n"
        "diff --git a/old.py b/old.py\n--- a/old.py\n+++ /dev/null\n"
        "--- /dev/null\n+++ b/new.py\n"
    )
    assert changed_files(diff) == ["x.py", "old.py", "new.py"]


def test_docker_runner_from_config():
    runner = DockerRunner.from_config(
        SandboxConfig(image="img:1", memory_limit="1g", cpu_limit=0.5, pids_limit=50)
    )
    assert (runner.image, runner.memory_limit, runner.cpu_limit, runner.pids_limit) == (
        "img:1",
        "1g",
        0.5,
        50,
    )


@dataclass
class _Block:
    type: str
    text: str = ""


@pytest.mark.parametrize("answer", ["Não sei montar o plano.", '{"risk_level": "low"}'])
async def test_analyst_never_invents_a_plan(tmp_path, answer):
    llm = AsyncMock(spec=LLMClient)
    llm.call.return_value = LLMResponse(
        content=[_Block(type="text", text=answer)],
        stop_reason="end_turn",
        input_tokens=1,
        output_tokens=1,
        raw_message=None,
    )
    measurer = AsyncMock(spec=BaselineMeasurer)
    measurer.measure.return_value = Baseline(id="b", run_id="r")
    analyst = AnalystAgent(
        llm=llm,
        profile=AgentProfile(model="m", temperature=0.0, max_tokens=10),
        worktree_path=tmp_path,
        baseline_measurer=measurer,
    )

    with pytest.raises(AgentOutputError):
        await analyst.analyze("r", 1, "tarefa")
