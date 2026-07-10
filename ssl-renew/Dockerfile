# ssl-renew Linux verification image.
#
# Provides a repeatable Linux + systemd-tools environment for developer
# acceptance, since macOS has no systemd. Build and run from the repo root:
#
#   docker build -t ssl-renew-verify -f ssl-renew/Dockerfile .
#   docker run --rm -v "$PWD":/workspace -w /workspace ssl-renew-verify \
#       make ssl-lint ssl-test ssl-verify-systemd
#
# This image is for LOCAL / CI verification only:
#   - it never contacts DNSPod, Let's Encrypt, or Qiniu
#   - it does not install or run the real systemd PID 1 (no `docker run --privileged`,
#     no init system) — `systemd-analyze verify` only parses/validates unit
#     files, it does not require a running systemd instance
#   - it is not, and must not be treated as, a production deployment target
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        curl \
        openssl \
        jq \
        ca-certificates \
        coreutils \
        systemd \
        shellcheck \
        python3 \
        python3-pip \
        python3-venv \
        git \
        make \
        gnupg \
    && rm -rf /var/lib/apt/lists/*

# Official Qiniu SDK, used by qiniu_helper.py for all signed API calls —
# see ssl-renew/requirements.txt and tests/test_qiniu_helper.py.
COPY requirements.txt requirements-dev.txt /tmp/
RUN pip3 install --break-system-packages --no-cache-dir \
        -r /tmp/requirements.txt -r /tmp/requirements-dev.txt \
    && rm -f /tmp/requirements.txt /tmp/requirements-dev.txt

# shfmt has no apt package on Debian; install the static binary.
ARG SHFMT_VERSION=3.8.0
RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
         amd64) shfmt_arch=amd64 ;; \
         arm64) shfmt_arch=arm64 ;; \
         *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
       esac \
    && curl -fsSL -o /usr/local/bin/shfmt \
        "https://github.com/mvdan/sh/releases/download/v${SHFMT_VERSION}/shfmt_v${SHFMT_VERSION}_linux_${shfmt_arch}" \
    && chmod +x /usr/local/bin/shfmt

# bats-core has no apt package on Debian; install from its own installer script.
ARG BATS_VERSION=1.11.1
RUN git clone --depth 1 --branch "v${BATS_VERSION}" https://github.com/bats-core/bats-core.git /tmp/bats-core \
    && /tmp/bats-core/install.sh /usr/local \
    && rm -rf /tmp/bats-core

WORKDIR /workspace

CMD ["bash"]
