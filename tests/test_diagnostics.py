from jusi_acp import diagnostics
from jusi_acp.state import EventStore


def test_diagnostics_exports_errors_without_prompts(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JUSI_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(diagnostics, "version", lambda package: "test-version")
    store = EventStore("fixture", tmp_path)
    store.bind_session("session", load_cache=False)
    store.add({"kind": "user_prompt", "text": "private prompt"})
    store.add_status("ACP diagnostic", "Unhandled notification method=fixture/unknown", "error")
    diagnostics.main()
    output = capsys.readouterr().out
    assert "fixture/unknown" in output
    assert "agent-client-protocol: test-version" in output
    assert "private prompt" not in output
