"""Exact provider for the real Jusi follow-up integration test."""
from pathlib import Path
import sys

from jusi_acp import ACPWorker, AgentLaunch, KernelProviderAdapter, ProviderSpec, family_claim


def catalog_entry():
    return {
        "plugin_id": "acp_fixture", "plugin_version": "1.0.0",
        "distribution": "jusi-acp-followup-fixture", "families": [family_claim()],
        "kernel_extensions": [__name__], "worker_entry_point": __name__ + ":create_worker",
        "media_types": ["text/x-ansi"], "interaction": "terminal_interactive",
    }


_adapter = KernelProviderAdapter("acp_fixture", "1.0.0")
jusi_kernel_adapter_v1 = _adapter.manifest
configure_jusi_runtime_v1 = _adapter.configure
load_ipython_extension = _adapter.load_ipython_extension


def create_worker(context):
    return ACPWorker(context, ProviderSpec("acp_fixture", "1.0.0", lambda config, cwd:
        AgentLaunch((sys.executable, str(Path(__file__).with_name("fake_acp_agent.py"))), cwd,
                    environment={"JUSI_FIXTURE_UNIQUE_SESSIONS": "1"})))
