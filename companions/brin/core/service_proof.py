"""Headers that prove brin to core-api on brin's own callbacks.

Writing an event, recording a web-chat turn, the routing lookup and the task
doorbell are calls brin makes as ITSELF: no agent and no person is acted for.
core-api admits them by where they come from, so each one carries the service
credential and brin's name, and nothing else.

The credential is read per call and has no default. A call that cannot prove
itself would be refused anyway, so a missing value is reported here by name
instead of being sent bare and read back as an unexplained 403.
"""
import os

SERVICE_NAME = "brin"
SECRET_SETTING = "SN_SERVICE_AUTH_SECRET"


class ServiceProofMissing(RuntimeError):
    """The service credential is absent from the service environment."""


def service_headers() -> dict:
    secret = os.environ.get(SECRET_SETTING, "").strip()
    if not secret:
        raise ServiceProofMissing(
            "%s is not set in the service environment: brin cannot prove its "
            "callbacks to core-api (re-run the core-api and dispatcher "
            "companions)" % SECRET_SETTING)
    return {"X-SN-Service": SERVICE_NAME, "X-SN-Service-Auth": secret}
