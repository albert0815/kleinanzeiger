# syntax=docker/dockerfile:1

######################
# kleinanzeigen-bot build stage
# Building from source (instead of downloading the prebuilt release binary)
# so we can layer our own nodriver connect-timeout patch on top of
# kleinanzeigen-bot's own post_install nodriver patches (scripts/fix_nodriver.py).
# See scripts/patch_nodriver_connect_timeout.py for why.
######################
FROM python:3.12-slim AS kleinanzeigen-bot-build

# Pinned on purpose; update it deliberately after testing a newer upstream commit.
ARG KLEINANZEIGEN_BOT_REF=32cbee7d5d8ef998aa363c719f091894373d714d

RUN apt-get update \
    && apt-get install -y --no-install-recommends git binutils build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pdm \
    && git init /opt/kleinanzeigen-bot-src \
    && git -C /opt/kleinanzeigen-bot-src fetch --depth 1 \
       https://github.com/Second-Hand-Friends/kleinanzeigen-bot.git "${KLEINANZEIGEN_BOT_REF}" \
    && git -C /opt/kleinanzeigen-bot-src checkout --detach FETCH_HEAD

COPY scripts/patch_nodriver_connect_timeout.py /opt/patch_nodriver_connect_timeout.py
# Reads the labels of the redesigned category dialog; see the script for why.
COPY scripts/patch_category_dialog_labels.py /opt/patch_category_dialog_labels.py
COPY scripts/test_category_dialog_patch.py /opt/test_category_dialog_patch.py
# Adds a `fields` command and names fields in form errors; see the script.
COPY scripts/patch_category_fields_command.py /opt/patch_category_fields_command.py
COPY bot-patches/ /opt/bot-patches/
COPY scripts/test_fields_command.py /opt/test_fields_command.py

WORKDIR /opt/kleinanzeigen-bot-src
# `pdm install` runs kleinanzeigen-bot's own post_install hook
# (scripts/fix_nodriver.py) first; our extra patch is applied on top of that.
RUN pdm install -v \
    && pdm run python /opt/patch_nodriver_connect_timeout.py \
    && python /opt/patch_category_dialog_labels.py \
    && python /opt/patch_category_fields_command.py /opt/bot-patches \
    && cp /opt/test_category_dialog_patch.py /opt/test_fields_command.py tests/unit/ \
    && pdm run pytest -q -p no:cacheprovider --no-cov tests/unit/test_category_dialog_patch.py tests/unit/test_fields_command.py \
    && pdm run compile \
    && ./dist/kleinanzeigen-bot --help

######################
# runtime image
######################
FROM python:3.12-slim

# Install uv (pinned for reproducibility)
COPY --from=ghcr.io/astral-sh/uv:0.7.2 /uv /uvx /bin/

# Set environment variables for uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Run as non-root user (fixed UID so host volume permissions can be set to match)
RUN useradd --create-home --shell /bin/false --uid 1001 appuser
WORKDIR /app

# Install Chromium (used directly by the bot, not via Playwright).
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=kleinanzeigen-bot-build /opt/kleinanzeigen-bot-src/dist/kleinanzeigen-bot /usr/local/bin/kleinanzeigen
RUN chmod +x /usr/local/bin/kleinanzeigen
COPY --from=kleinanzeigen-bot-build /opt/kleinanzeigen-bot-src/src/kleinanzeigen_bot/resources/categories.yaml /app/resources/categories.yaml

# Install Python dependencies
COPY --chown=appuser pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Copy application source
COPY --chown=appuser main.py mahlino.py background_worker.py queue_manager.py agent_registry.py tools.py skills.py ad_store.py bot_runner.py lunch_plan.py recipes.py recipe_import_api.py category_fields.py search_watch.py ./
COPY --chown=appuser skills/ ./skills/

USER appuser

CMD ["/app/.venv/bin/python", "main.py"]

