# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.13.15-slim-bookworm@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1

FROM ${PYTHON_IMAGE} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
RUN python -m venv /opt/venv
COPY requirements.lock /tmp/requirements.lock
RUN --mount=type=cache,target=/root/.cache/pip \
    /opt/venv/bin/pip install --require-hashes --only-binary=:all: -r /tmp/requirements.lock

FROM ${PYTHON_IMAGE} AS runner
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /nonexistent --shell /usr/sbin/nologin app \
    && install -d -o 10001 -g 10001 /app /data \
    && rm -rf /usr/local/lib/python3.13/site-packages/pip* \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13 \
      /usr/bin/apt /usr/bin/apt-get /usr/bin/apt-cache /usr/bin/apt-config /usr/bin/apt-mark \
      /usr/bin/dpkg /usr/bin/dpkg-deb /usr/bin/dpkg-divert /usr/bin/dpkg-maintscript-helper \
      /usr/bin/dpkg-query /usr/bin/dpkg-realpath /usr/bin/dpkg-split \
      /usr/bin/dpkg-statoverride /usr/bin/dpkg-trigger
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    PLATFORM_DB=/data/assambleya.db \
    WEBAPP_HOST=0.0.0.0 \
    WEBAPP_PORT=8080
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --chown=10001:10001 bot.py ./bot.py
RUN python -m py_compile bot.py \
    && rm -rf /opt/venv/lib/python3.13/site-packages/pip* \
    && rm -f /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.13
USER 10001:10001
EXPOSE 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=20s --timeout=5s --start-period=20s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()"]
CMD ["python", "bot.py"]
