"""Real Neovim terminal regression: no input is sent while ACP is streaming."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from jusi_acp.ipc import WorkerApplicationBridge

import pytest

from test_application import _payload


def test_neovim_idle_redraw_and_sdk_error_history(tmp_path, request):
    body = "ui-idle"
    nvim = shutil.which("nvim")
    if not nvim:
        pytest.skip("Neovim is required for terminal integration")
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(_payload(tmp_path, body)))
    result_path = tmp_path / "screen.json"
    socket_dir = tempfile.TemporaryDirectory(prefix="jusi-ui-", dir="/tmp")
    request.addfinalizer(socket_dir.cleanup)
    socket_path = str(Path(socket_dir.name) / "control.sock")
    bridge = WorkerApplicationBridge(socket_path)
    request.addfinalizer(bridge.close)
    command = [sys.executable, "-m", "jusi_acp.application", str(payload_path), socket_path]
    lua_command = "{" + ",".join(json.dumps(part) for part in command) + "}"
    script = tmp_path / "check.lua"
    script.write_text('''
vim.o.columns = 360
vim.o.lines = 35
local job = vim.fn.termopen(COMMAND)

local function screen()
  return table.concat(vim.api.nvim_buf_get_lines(0, 0, -1, false), "\\n")
end
local streamed = vim.wait(10000, function() return screen():find("AUTOMATIC%-STREAM") ~= nil end, 50)
local stream_screen = screen()
local stopped = vim.wait(10000, function() return screen():find("end_turn") ~= nil end, 50)
local turn_screen = screen()
pcall(vim.fn.chansend, job, string.char(5))
local error_seen = vim.wait(5000, function() return screen():find("RequestError") ~= nil end, 50)
vim.fn.writefile({vim.json.encode({streamed=streamed, stopped=stopped, error_seen=error_seen,
    stream_screen=stream_screen, turn_screen=turn_screen, error_screen=screen()})}, RESULT)
vim.fn.jobstop(job)
vim.cmd("qa!")
'''.replace("COMMAND", lua_command).replace("RESULT", json.dumps(str(result_path))))
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
               JUSI_STATE_HOME=str(tmp_path / "state"), TERM="xterm-256color")
    result = subprocess.run([nvim, "--headless", "-u", "NONE", "-l", str(script)],
                            env=env, cwd=tmp_path, text=True, capture_output=True, timeout=35)
    assert result.returncode == 0, result.stderr
    screen = json.loads(result_path.read_text())
    assert screen["streamed"], screen["stream_screen"]
    assert screen["stopped"], screen["turn_screen"]
    assert "Traceback (most recent call last)" not in screen["stream_screen"]
    assert "Traceback (most recent call last)" not in screen["turn_screen"]
    assert screen["error_seen"], screen["error_screen"]
