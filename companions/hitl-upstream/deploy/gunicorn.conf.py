# gunicorn settings for the hitl server, behind a TLS-terminating proxy.
import os

# Loopback unless told otherwise: the proxy in front is the only client. The Dockerfile sets 0.0.0.0.
bind = os.environ.get("HITL_BIND", "127.0.0.1") + ":" + os.environ.get("PORT", "8080")
worker_class = "gthread"
workers = int(os.environ.get("HITL_WORKERS", "2"))
threads = int(os.environ.get("HITL_THREADS", "8"))
# Blob uploads (up to HITL_MAX_BLOB_BYTES) stream through one request.
timeout = 600
accesslog = "-"
errorlog = "-"
# gunicorn 24+ opens a control socket under $HOME, which www-data cannot write.
control_socket = "/tmp/hitl-gunicorn.ctl"
