# Build from the repo root. Code + deps only — parquet is downloaded at runtime.
#   docker build -f deployment/gpu/rental/Dockerfile -t edrsr-embed .
# Prefer the provider's PyTorch image + ./deployment/gpu/rental/run.sh instead
# of shipping this from home (the CUDA layers are several GB).
FROM pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY deployment/gpu ./deployment/gpu
RUN pip install --no-cache-dir -e '.[gpu]' 'huggingface_hub[hf_xet]>=0.32' \
    && chmod +x deployment/gpu/rental/*.sh

ENV DATA_ROOT=/workspace/edrsr
ENV SECTIONS="civil administrative"
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["/app/deployment/gpu/rental/run-multi.sh"]
