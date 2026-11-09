"""Which gateway provider runs a stateless chat for a model id.

A model id is <runtime>/<rest>. The first segment names who executes the turn;
everything after the first slash is that runtime's own model id and is handed
over untouched, slashes and all (openrouter/openai/gpt-5 -> openrouter runs
"openai/gpt-5"). Nothing here knows which runtimes exist: the caller passes the
names of the providers it has, and a runtime with no provider is refused rather
than guessed at.

Kept free of gateway imports so the rule can be exercised on its own
(tests/agent-model-runtime.test.sh).
"""
from typing import Iterable, Optional, Tuple

# The gateway registered the Claude CLI provider as "cli" long before model ids
# named their runtime. The runtime is "claude-cli" everywhere else (catalog,
# cli_providers, session rows), so the one spelling difference lives here.
_PROVIDER_OF_RUNTIME = {"claude-cli": "cli"}
_RUNTIME_OF_PROVIDER = {v: k for k, v in _PROVIDER_OF_RUNTIME.items()}


class UnknownRuntime(ValueError):
    """The id names a runtime the gateway has no provider for."""


def route_model(model: Optional[str], providers: Iterable[str]) -> Tuple[str, Optional[str]]:
    """Return (provider name, the provider's own model id) for a model id.

    Legacy, one release, then removed:
      - a bare name ('sonnet') is the Claude CLI, as it was before ids named
        their runtime;
      - an id whose first segment is not a runtime ('anthropic/claude-x') is an
        OpenRouter model written without its runtime, as brin sent them.
    """
    known = set(providers)
    if not model:
        return "cli", None
    if "/" not in model:
        return "cli", model
    runtime, rest = model.split("/", 1)
    provider = _PROVIDER_OF_RUNTIME.get(runtime, runtime)
    if provider in known:
        return provider, rest
    if runtime.endswith("-cli"):
        # A CLI runtime this gateway cannot run statelessly (codex-cli, ...).
        # Falling through to OpenRouter would send a local model name to a cloud
        # provider and bill a failure.
        raise UnknownRuntime(f"no provider for runtime {runtime!r} in {model!r}")
    return "openrouter", model


def runtime_of(provider: str) -> str:
    """The runtime name a session row records for a gateway provider name."""
    return _RUNTIME_OF_PROVIDER.get(provider, provider)
