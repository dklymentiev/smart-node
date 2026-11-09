"""Recompose one agent's CLAUDE.md, through the node's own canonical path.

This script used to assemble the prompt ITSELF, and that made it a third
composer: `_compose_body` in routes/agents.py writes the file and renders
GET /api/agents/<slug>/system-prompt (unified in 1.9.8), and this one built a
different, thinner text from a different query. What it produced was missing the
agent's name, its memory workspace, the team roster, its own channels and its
contacts -- and, worse, it looked policies up through department_policies /
agent_policy_opts instead of the agent's policy_categories, so on a node where
policies are assigned by category it composed **zero rules** and wrote that over
the good file.

That is exactly what happened on test-node on 2026-08-06: the agent was created
correctly, this script ran afterwards during onboarding, and the node was left
with a 15 910-character prompt that opened "# Configurator" and contained none of
the thirteen rules, where the canonical composition is 21 570 characters and
opens with the agent's name.

So it no longer composes. It asks the node to do it, which recomposes AND
refreshes any live gateway session from the same text -- the one operation, in
one place. If the recomposition ever needs a new block, it is added once, in the
composer, and every path gets it.

    python companions/compose-prompt.py <agent-slug> [--dry-run]

--dry-run prints the prompt the node would write, and writes nothing.
"""

import os
import sys
import urllib.error
import urllib.request


CORE_API = (os.environ.get("SN_CORE_API_URL") or "").rstrip("/")
if not CORE_API:
    raise RuntimeError(
        "SN_CORE_API_URL is not set. "
        "The node installer should write this to the service environment."
    )


def _proof() -> dict:
    """What this script presents to core-api.

    It is core-api's own companion script, run as core-api's service account
    by the installer, and it acts for nobody. It proves itself with the service
    credential that account already holds and names itself core-api; core-api
    admits it to the two calls below by its service list. A typed name is no
    account and is refused, so there is no bare form.
    """
    secret = os.environ.get("SN_SERVICE_AUTH_SECRET", "").strip()
    if not secret:
        sys.exit("SN_SERVICE_AUTH_SECRET is not set: this script cannot prove "
                 "itself to core-api (run it through core-api.sh, which supplies it)")
    return {"X-SN-Service": "core-api", "X-SN-Service-Auth": secret}


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers=_proof())
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: compose-prompt.py <agent-slug> [--dry-run]")
    slug = sys.argv[1]
    dry = "--dry-run" in sys.argv[2:]

    try:
        if dry:
            body = _get("%s/api/agents/%s/system-prompt" % (CORE_API, slug))
            print("agent=%s -> %d chars (dry run, nothing written)" % (slug, len(body)))
            print("--- composed CLAUDE.md ---")
            print(body)
            return

        req = urllib.request.Request(
            "%s/api/agents/%s/regenerate" % (CORE_API, slug),
            method="POST", headers=_proof())
        with urllib.request.urlopen(req, timeout=180) as r:
            r.read()
        body = _get("%s/api/agents/%s/system-prompt" % (CORE_API, slug))
        print("agent=%s recomposed -> %d chars" % (slug, len(body)))
    except urllib.error.HTTPError as e:
        sys.exit("agent=%s: core-api answered HTTP %s -- %s"
                 % (slug, e.code, e.read()[:200].decode("utf-8", "replace")))
    except Exception as e:
        # Loud, not silent: a recompose that quietly does nothing is how an agent
        # ends up running yesterday's instructions.
        sys.exit("agent=%s: could not reach core-api at %s -- %s" % (slug, CORE_API, e))


if __name__ == "__main__":
    main()
