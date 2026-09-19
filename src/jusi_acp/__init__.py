"""Provider-facing API for the Jusi ACP plugin family."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("jusi-acp")
except PackageNotFoundError:  # source checkout
    __version__ = "0.1.0"

from .family import (  # noqa: E402
    CAPABILITIES,
    FAMILY_ID,
    MAGIC_NAME,
    PRESENTATION,
    KernelProviderAdapter,
    family_claim,
)
from .provider import AgentLaunch, ProviderSpec  # noqa: E402
from .worker import ACPWorker  # noqa: E402

__all__ = [
    "ACPWorker",
    "AgentLaunch",
    "CAPABILITIES",
    "FAMILY_ID",
    "KernelProviderAdapter",
    "MAGIC_NAME",
    "PRESENTATION",
    "ProviderSpec",
    "family_claim",
]
