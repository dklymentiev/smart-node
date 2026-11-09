"""May a model run here? The node's catalog decides, with the built-in list
as the floor. Shared by the door (cloud chats) and the runtime (CLI sessions)."""
import logging

import db
from config import ALLOWED_MODELS

logger = logging.getLogger(__name__)


def model_allowed(upstream: str, model: str, legacy_key: str) -> bool:
    """A model may run here if the node's catalog has it enabled, or if it is on the
    built-in ALLOWED_MODELS list.

    The catalog (sn_config.models) is what the owner and the configurator edit
    through core-api. Before this, a model added there was still refused
    here by a list hardcoded in config.py, so "added" did not mean "works". The
    built-in list stays as the floor for callers that never had a catalog row.
    `upstream` is the catalog's provider name, `legacy_key` the ALLOWED_MODELS key
    (they differ for the CLI: claude-cli vs cli).
    """
    if model in ALLOWED_MODELS.get(legacy_key, set()):
        return True
    try:
        rows = db.query(
            "SELECT 1 FROM sn_config.models "
            "WHERE provider = %s AND model = %s AND enabled = true",
            (upstream, model), fetch=True,
        )
    except Exception as e:
        # The catalog is unreadable: refuse rather than guess, and say why in the log.
        logger.error("model catalog unreadable, refusing %s/%s: %s", upstream, model, e)
        return False
    return bool(rows)
