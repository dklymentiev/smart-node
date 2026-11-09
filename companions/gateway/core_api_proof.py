"""Headers that prove the gateway to core-api on the gateway's own reads.

The gateway reads an agent's assembled prompt as ITSELF: no agent and no
person is acted for. core-api admits a node service that presents the service
credential and is listed for the permission. The owner notices the gateway
raises are a different call with a different proof; see sessions_v2_routes.

The credential is read per call and has no default. A read that cannot prove
itself would be refused anyway, so a missing value is reported here by name.
"""
import os

SERVICE_NAME = "gateway"
SECRET_SETTING = "SN_SERVICE_AUTH_SECRET"


def service_headers() -> dict:
    secret = os.environ.get(SECRET_SETTING, "").strip()
    if not secret:
        raise RuntimeError(
            "%s is not set in the gateway environment: the gateway cannot "
            "prove its reads to core-api (re-run the core-api and gateway "
            "companions)" % SECRET_SETTING)
    return {"X-SN-Service": SERVICE_NAME, "X-SN-Service-Auth": secret}
