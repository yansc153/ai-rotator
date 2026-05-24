FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/pyproject.toml
COPY . /app

RUN pip install --upgrade pip \
    && pip install -e .

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["morning"]
