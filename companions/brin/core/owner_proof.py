"""Headers for the calls brin makes to core-api as the node owner.

core-api refuses an owner name that arrives over TCP with nothing behind it.
brin is a service, not the owner's browser, so for the few owner-level calls it
makes it presents the service credential and names the owner the node was
configured with. core-api accepts that pair on a short list of routes only.

Both values are read from the environment per call and neither has a default:
a guessed owner name would be an identity nobody set, and a call sent without
the credential is refused anyway. A missing value is an installation fault, so
it is reported by name instead of being skipped.
"""
import logging
import os

log = logging.getLogger(__name__)

OWNER_SETTING = "SN_OPERATOR_ACTOR"
SECRET_SETTING = "SN_SERVICE_AUTH_SECRET"


class OwnerProofMissing(RuntimeError):
    """A setting the owner delegation needs is absent from the environment."""


def owner() -> str:
    """The configured owner, or '' when the node was never told."""
    return os.environ.get(OWNER_SETTING, "").strip()


def _require(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n, "").strip()]
    if missing:
        msg = ("%s not set in the service environment: brin cannot call "
               "core-api as the node owner (re-run the core-api and dispatcher "
               "companions)" % " and ".join(missing))
        log.error(msg)
        raise OwnerProofMissing(msg)


def owner_read_headers() -> dict:
    """For safe-method reads. They need the name and no proof, so the
    credential is not put on the wire."""
    _require(OWNER_SETTING)
    return {"Remote-User": owner()}


def owner_headers() -> dict:
    """For the owner-level mutations core-api lists for brin."""
    _require(OWNER_SETTING, SECRET_SETTING)
    name = owner()
    return {"Remote-User": name, "X-SN-Service": "brin",
            "X-SN-Service-Auth": os.environ[SECRET_SETTING].strip(),
            "X-SN-On-Behalf-Of": name}
