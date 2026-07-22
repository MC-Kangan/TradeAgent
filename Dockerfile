ARG PYTHON_BASE_IMAGE=python:3.12-slim
FROM ${PYTHON_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRADE_RESEARCH_DATA_DIR=/var/lib/trade-research

WORKDIR /app
RUN groupadd --system research && useradd --system --gid research --home /nonexistent research

COPY pyproject.toml README.md requirements.lock ./
COPY src ./src
RUN python -m pip install --no-cache-dir --requirement requirements.lock \
    && python -m pip install --no-cache-dir --no-deps --no-build-isolation .

RUN mkdir -p /var/lib/trade-research && chown research:research /var/lib/trade-research
USER research
VOLUME ["/var/lib/trade-research"]
CMD ["trade-research", "doctor"]
