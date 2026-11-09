FROM python:3.11-slim

ARG EMBEDDING_MODEL=intfloat/multilingual-e5-base

# Create mesh user first so all subsequent files are owned correctly.
# Avoids expensive `chown -R` layer that duplicates model cache.
RUN groupadd -r mesh && useradd -r -g mesh -d /app -m mesh

WORKDIR /app

COPY requirements.txt .

# CPU-only torch. A Smart Node node has no GPU; the default CUDA build of
# torch (pulled transitively by sentence-transformers) drags in ~2-3 GB
# of unused nvidia/cuda wheels. Pre-installing the CPU wheel from the
# PyTorch CPU index means `pip install -r requirements.txt` finds torch
# already satisfied and never fetches the CUDA build. Smart Node patch
# to the vendored snapshot -- not in upstream Mesh.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir -r requirements.txt

# Store HuggingFace model cache under /app so it is accessible to the
# non-root mesh user at runtime. The cache dir is created empty here;
# the model is fetched lazily on first /search or /index call. This
# keeps the image ~1.1 GB smaller and the installer ~4 min faster at
# the cost of a one-time ~30-60s warmup on the first semantic op.
# Mount /app/.cache as a volume in compose to persist across restarts.
# (verify_baked_model() in config.py skips its check when sentinel absent.)
ENV HF_HOME=/app/.cache

# Switch to mesh user BEFORE COPY so files are owned correctly
# without needing `chown -R`.
USER mesh
RUN mkdir -p /app/.cache

COPY --chown=mesh:mesh mesh/ ./mesh/
COPY --chown=mesh:mesh ui/ ./ui/
COPY --chown=mesh:mesh examples/ ./examples/

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["gunicorn", "mesh.main:app", "-k", "uvicorn.workers.UvicornWorker", \
    "--bind", "0.0.0.0:8000", \
    "--workers", "1", \
    "--graceful-timeout", "30", \
    "--timeout", "120"]
