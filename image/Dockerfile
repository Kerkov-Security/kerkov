# syntax=docker/dockerfile:1.11

FROM ghcr.io/astral-sh/uv@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc AS uv

# kali-last-release follows Kali's tested point-release snapshot instead of the
# continuously changing rolling repository. Dependabot updates this digest.
FROM kalilinux/kali-last-release@sha256:97c05ce972700602390327a91cec4c230c118af47342e620f5763e886deb9733

LABEL org.opencontainers.image.title="Kerkov" \
      org.opencontainers.image.description="Security-focused conversational AI and security-tooling environment" \
      org.opencontainers.image.source="https://github.com/Kerkov-Security/kerkov" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/root \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/kerkov-venv \
    PATH="/opt/kerkov-venv/bin:/usr/local/bin:${PATH}"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

COPY --from=uv /uv /uvx /usr/local/bin/
COPY image/system-packages.txt /tmp/kerkov-system-packages.txt

# Kali point-release package versions are selected by the pinned base snapshot.
# hadolint ignore=DL3008
RUN apt-get update \
    && mapfile -t packages < <(sed -e 's/#.*$//' -e '/^[[:space:]]*$/d' /tmp/kerkov-system-packages.txt) \
    && apt-get install -y --no-install-recommends "${packages[@]}" \
    && rm -rf /var/lib/apt/lists/* /tmp/kerkov-system-packages.txt

WORKDIR /opt/kerkov

# The build context is the Kerkov repository root. Copy dependency metadata
# first so source-only changes retain the dependency layer cache.
COPY pyproject.toml uv.lock .python-version ./

RUN uv sync --locked --no-dev --no-install-project --extra security \
    && uv pip check --python /opt/kerkov-venv/bin/python \
    && uv run --no-sync python -m playwright install --with-deps chromium \
    && chmod -R a+rX /opt/ms-playwright \
    && rm -rf /root/.cache/uv /var/lib/apt/lists/*

COPY README.md ./
COPY src ./src
COPY agents ./agents
COPY config ./config
COPY skills ./skills

# Kerkov resolves top-level agents, config, and skills relative to its source
# tree. Keep that immutable source tree as the installed package target.
RUN uv pip install --no-deps --editable /opt/kerkov \
        --python /opt/kerkov-venv/bin/python \
    && uv pip check --python /opt/kerkov-venv/bin/python \
    && rm -rf /root/.cache/uv

RUN groupadd --gid 1000 kerkov \
    && useradd --uid 1000 --gid kerkov --create-home --shell /bin/bash kerkov \
    && chmod 0755 /home/kerkov \
    && install -d -o kerkov -g kerkov /home/kerkov/.cache /home/kerkov/.kerkov /workspace

ENV HOME=/home/kerkov

COPY image/smoke-test.sh /usr/local/bin/kerkov-smoke-test
RUN chmod 0755 /usr/local/bin/kerkov-smoke-test

WORKDIR /workspace
USER kerkov

RUN /usr/local/bin/kerkov-smoke-test

# Apply the per-commit label after all expensive build and smoke-test layers so
# a revision-only change does not invalidate the image dependency cache.
ARG KERKOV_SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision="${KERKOV_SOURCE_REVISION}"

ENTRYPOINT ["/usr/bin/tini", "--", "kerkov"]
CMD []
