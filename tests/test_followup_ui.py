"""Use real notebook edits and Jusi's follow-up lane to answer an ACP agent."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def _run_notebook_fixture(tmp_path, script_name):
    root = Path(__file__).resolve().parents[1]
    frontend = Path(os.environ.get("JUSI_NVIM_ROOT", str(root.parent / "jusi")))
    nvim = shutil.which("nvim")
    if not nvim or not (frontend / "lua/jusi/init.lua").exists():
        pytest.skip("Requires Neovim and JUSI_NVIM_ROOT (or a sibling jusi checkout)")
    fixture_root = root / "tests/fixtures"
    plugins = tmp_path / "plugins"
    metadata = plugins / "jusi_acp_followup_fixture-1.0.0.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: jusi-acp-followup-fixture\nVersion: 1.0.0\n")
    (metadata / "entry_points.txt").write_text("[jusi.plugins.v1]\nacp_fixture = acp_followup_provider:catalog_entry\n")
    home = tmp_path / "home"
    (home / ".jusi").mkdir(parents=True)
    (home / ".jusi/jusi.toml").write_text(
        '[acp.fixture]\nprovider = "acp_fixture"\npath = ' + json.dumps(str(tmp_path)) + '\n')
    result_path = tmp_path / "result.json"
    env = dict(os.environ, HOME=str(home),
               PYTHONPATH=os.pathsep.join(map(str, [root / "src", fixture_root, plugins])),
               JUSI_STATE_HOME=str(tmp_path / "state"), JUSI_NVIM_ROOT=str(frontend),
               JUSI_TEST_PYTHON=sys.executable, JUSI_TEST_RESULT=str(result_path),
               TERM="xterm-256color")
    run = subprocess.run([nvim, "--headless", "-i", "NONE", "-u", "NONE", "-l",
                          str(fixture_root / script_name)],
                         env=env, cwd=tmp_path, capture_output=True, text=True, timeout=100)
    assert run.returncode == 0, run.stderr
    result = json.loads(result_path.read_text())
    assert result["ok"], result.get("failure", "") + "\n" + result.get("stderr", "") + "\n" + result.get("screen", "")
    events = [json.loads(line) for path in (tmp_path / "state").rglob("events.jsonl")
              for line in path.read_text().splitlines()]
    return result, events


def test_questions_follow_the_real_jusi_notebook_ux(tmp_path):
    result, events = _run_notebook_fixture(tmp_path, "followup_questions.lua")
    prompts = [row["text"] for row in events if row["type"] == "user_prompt"]
    assert [body.strip() for body in prompts if body.strip() != "fresh-cell-only"] == [
        "questions-many", "questions", "questions-wait", "healthy", "diff",
    ]
    assert [body.strip() for body in prompts].count("fresh-cell-only") == 1
    assert result["fresh_isolated"]
    answers = [row["text"] for row in events if row["type"] == "question_answer"]
    assert answers[:2] == ["Application", "  Preserve indentation.\nα and β\n/config is literal answer text."]
    assert answers[2:] in ([], ["Application"])
    replies = [row["text"] for row in events if row["type"] == "agent_message_chunk"]
    returned = json.loads(next(text for text in replies if text.startswith("ANSWERS:")).removeprefix("ANSWERS:"))
    assert returned == {"0": answers[0], "1": answers[1]}
    assert all(answer in result["history"] for answer in answers[:2]), result["history"]


def test_session_features_through_real_notebook_and_terminal(tmp_path):
    (tmp_path / "sample.py").write_text("print('sample')\n")
    result, events = _run_notebook_fixture(tmp_path, "session_features.lua")
    assert result["completion_followup"]
    assert result["form"] and result["terminal"] and result["plan"] and result["url"] and result["model"]
    assert result["attention"]
    assert result["metadata_attention"]
    assert not any(row["type"] == "user_prompt" and row["text"].strip() == "/model" for row in events)
    assert next(row for row in events if row["type"] == "user_prompt" and row["text"].strip() == "terminal")["raw"]["model"] == "fixture-beta"
    assert not any(row["type"] == "user_prompt" and "sample" in row["text"] for row in events)
