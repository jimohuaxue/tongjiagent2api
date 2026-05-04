FROM ubuntu:24.04

ARG TARGETARCH
ARG FINGERPRINT_CHROMIUM_URL_AMD64="https://github.com/adryfish/fingerprint-chromium/releases/download/142.0.7444.175/ungoogled-chromium-142.0.7444.175-1-x86_64_linux.tar.xz"
ARG FINGERPRINT_CHROMIUM_URL_ARM64_CHROMIUM_DEB="https://github.com/luispater/fingerprint-chromium-arm64/releases/download/135.0.7049.95-1/ungoogled-chromium_135.0.7049.95-1.deb12u1_arm64.deb"
ARG FINGERPRINT_CHROMIUM_URL_ARM64_COMMON_DEB="https://github.com/luispater/fingerprint-chromium-arm64/releases/download/135.0.7049.95-1/ungoogled-chromium-common_135.0.7049.95-1.deb12u1_arm64.deb"
ARG FINGERPRINT_CHROMIUM_URL_ARM64_SANDBOX_DEB="https://github.com/luispater/fingerprint-chromium-arm64/releases/download/135.0.7049.95-1/ungoogled-chromium-sandbox_135.0.7049.95-1.deb12u1_arm64.deb"
ARG FINGERPRINT_CHROMIUM_URL_ARM64_L10N_DEB="https://github.com/luispater/fingerprint-chromium-arm64/releases/download/135.0.7049.95-1/ungoogled-chromium-l10n_135.0.7049.95-1.deb12u1_all.deb"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WEB2API_DATA_DIR=/data \
    HOME=/data

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    xz-utils \
    xvfb \
    xauth \
    python3 \
    python3-pip \
    python3-venv \
    python-is-python3 \
    poppler-utils \
    software-properties-common \
    fonts-liberation \
    libasound2t64 \
    libatk-bridge2.0-0t64 \
    libatk1.0-0t64 \
    libcairo2 \
    libcups2t64 \
    libdbus-1-3 \
    libdrm2 \
    libfontconfig1 \
    libgbm1 \
    libglib2.0-0t64 \
    libgtk-3-0t64 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libu2f-udev \
    libvulkan1 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxkbcommon0 \
    libxrandr2 \
    libxrender1 \
    libxshmfence1 \
    && add-apt-repository -y universe \
    && add-apt-repository -y multiverse \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN python -m venv "${VIRTUAL_ENV}" \
    && pip install --no-cache-dir --upgrade pip

RUN set -eux; \
    arch="${TARGETARCH:-}"; \
    if [ -z "${arch}" ]; then arch="$(dpkg --print-architecture)"; fi; \
    mkdir -p /opt/fingerprint-chromium; \
    case "${arch}" in \
      amd64|x86_64) \
        curl -L --fail "${FINGERPRINT_CHROMIUM_URL_AMD64}" -o /tmp/fingerprint-chromium.tar.xz; \
        tar -xf /tmp/fingerprint-chromium.tar.xz -C /opt/fingerprint-chromium --strip-components=1; \
        rm -f /tmp/fingerprint-chromium.tar.xz; \
        ;; \
      arm64|aarch64) \
        curl -L --fail "${FINGERPRINT_CHROMIUM_URL_ARM64_CHROMIUM_DEB}" -o /tmp/ungoogled-chromium.deb; \
        curl -L --fail "${FINGERPRINT_CHROMIUM_URL_ARM64_COMMON_DEB}" -o /tmp/ungoogled-chromium-common.deb; \
        curl -L --fail "${FINGERPRINT_CHROMIUM_URL_ARM64_SANDBOX_DEB}" -o /tmp/ungoogled-chromium-sandbox.deb; \
        curl -L --fail "${FINGERPRINT_CHROMIUM_URL_ARM64_L10N_DEB}" -o /tmp/ungoogled-chromium-l10n.deb; \
        apt-get update; \
        apt-get install -y --no-install-recommends /tmp/ungoogled-chromium.deb /tmp/ungoogled-chromium-common.deb /tmp/ungoogled-chromium-sandbox.deb /tmp/ungoogled-chromium-l10n.deb; \
        rm -rf /var/lib/apt/lists/* /tmp/ungoogled-chromium*.deb; \
        for bin in /usr/bin/ungoogled-chromium /usr/bin/chromium /usr/bin/chromium-browser; do \
          if [ -x "${bin}" ]; then ln -sf "${bin}" /opt/fingerprint-chromium/chrome; break; fi; \
        done; \
        test -x /opt/fingerprint-chromium/chrome; \
        ;; \
      *) \
        echo "Unsupported architecture: ${arch}" >&2; \
        exit 1; \
        ;; \
    esac

COPY pyproject.toml /tmp/pyproject.toml
RUN python - <<'PY'
import subprocess
import tomllib

with open("/tmp/pyproject.toml", "rb") as f:
    deps = tomllib.load(f)["project"]["dependencies"]

subprocess.check_call(["pip", "install", "--no-cache-dir", *deps])
PY

COPY . /app

RUN chmod +x /app/docker/entrypoint.sh

VOLUME ["/data"]
EXPOSE 9000

ENTRYPOINT ["/app/docker/entrypoint.sh"]
