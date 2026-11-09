"""A custom provider's model name: what the address calls it, as the address
returns it in its model list.

OpenRouter names every model <vendor>/<model> ("openrouter/auto",
"anthropic/claude-3-haiku"); the check refused the slash, so no OpenRouter model
could be connected from the first run.
"""

from routes.gateway_secrets import _CUSTOM_MODEL_RE


def test_a_vendor_prefixed_name_is_accepted():
    for name in ("openrouter/auto", "anthropic/claude-3-haiku", "models/gemini-3-flash", "gpt-5.4", "qwen2.5:7b"):
        assert _CUSTOM_MODEL_RE.match(name), name


def test_a_name_that_is_not_a_model_is_refused():
    for name in ("", "/auto", "a b", "x" * 201, "../etc"):
        assert not _CUSTOM_MODEL_RE.match(name), name
