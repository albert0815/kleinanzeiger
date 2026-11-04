"""Agent registry: one strands.Agent per Telegram chat_id.

Agents are lazily created and held in memory. Sessions are purely in-memory
and do not survive container restarts.

A session is automatically reset after SESSION_TIMEOUT_DAYS of inactivity
(default 1 day, configurable via KLEINANZEIGEN_SESSION_TIMEOUT_DAYS).

Usage:
    from agent_registry import get_agent, clear_agent

    agent = get_agent(chat_id)
    result = await asyncio.to_thread(agent, user_message)
    response_text = result.message  # or str(result)
"""

from __future__ import annotations

import logging
import os
import time

from strands import Agent
from strands.agent.conversation_manager import SlidingWindowConversationManager

from skills import BASE_SYSTEM_PROMPT, build_skills_plugin
from tools import (
    delete_kleinanzeigen_ad,
    get_current_date,
    get_lunch_plan,
    get_lunch_planning_data,
    list_kleinanzeigen_ads,
    list_search_watches,
    mark_kleinanzeigen_ad_sold,
    add_lunches_to_shopping_list,
    add_to_shopping_list,
    create_search_watch,
    delete_search_watch,
    propose_lunch_plan,
    propose_recipe,
    read_recipe_from_link,
    refresh_kleinanzeigen_status,
    search_kleinanzeigen_categories,
    set_kleinanzeigen_auto_republish,
    update_search_watch,
)

log = logging.getLogger("kleinanzeigen-agent.registry")

# Reset session after this many seconds of inactivity (default: 1 day)
SESSION_TIMEOUT_SECONDS: float = (
    float(os.environ.get("KLEINANZEIGEN_SESSION_TIMEOUT_DAYS", "1")) * 86400
)

_TOOLS = [
    get_current_date,
    get_lunch_plan,
    get_lunch_planning_data,
    propose_lunch_plan,
    add_to_shopping_list,
    add_lunches_to_shopping_list,
    read_recipe_from_link,
    propose_recipe,
    list_kleinanzeigen_ads,
    refresh_kleinanzeigen_status,
    search_kleinanzeigen_categories,
    delete_kleinanzeigen_ad,
    mark_kleinanzeigen_ad_sold,
    set_kleinanzeigen_auto_republish,
    create_search_watch,
    list_search_watches,
    update_search_watch,
    delete_search_watch,
]

# chat_id -> Agent
_agents: dict[int, Agent] = {}
# chat_id -> last activity timestamp (monotonic seconds)
_last_activity: dict[int, float] = {}


def _make_model():
    """Create a Strands model based on the active LLM provider."""
    provider = os.environ.get("LLM_PROVIDER", "").lower()
    if not provider:
        provider = (
            "bedrock" if os.environ.get("AWS_BEARER_TOKEN_BEDROCK") else "anthropic"
        )

    if provider == "bedrock":
        from strands.models import BedrockModel

        model_id = os.environ.get("CLAUDE_MODEL", "eu.anthropic.claude-sonnet-5")
        region = os.environ.get("AWS_REGION", "eu-central-1")
        return BedrockModel(model_id=model_id, region_name=region)

    from strands.models.anthropic import AnthropicModel

    model_id = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    return AnthropicModel(model_id=model_id, api_key=api_key)


def _create_agent(chat_id: int) -> Agent:
    agent = Agent(
        model=_make_model(),
        system_prompt=BASE_SYSTEM_PROMPT,
        plugins=[build_skills_plugin()],
        tools=_TOOLS,
        conversation_manager=SlidingWindowConversationManager(window_size=50),
    )
    _agents[chat_id] = agent
    _last_activity[chat_id] = time.monotonic()
    log.info("[chat=%d] Agent created (in-memory session)", chat_id)
    return agent


def get_agent(chat_id: int) -> Agent:
    """Return the Agent for *chat_id*, creating or resetting it as needed.

    If the last activity was more than SESSION_TIMEOUT_SECONDS ago, the old
    agent is discarded and a fresh one is created (resetting conversation history).
    """
    now = time.monotonic()
    last = _last_activity.get(chat_id)
    if last is not None and (now - last) > SESSION_TIMEOUT_SECONDS:
        log.info(
            "[chat=%d] Session timed out after %.1f h – starting fresh",
            chat_id,
            (now - last) / 3600,
        )
        _agents.pop(chat_id, None)
        _last_activity.pop(chat_id, None)

    if chat_id not in _agents:
        return _create_agent(chat_id)

    _last_activity[chat_id] = now
    return _agents[chat_id]


def clear_agent(chat_id: int) -> None:
    """Discard the in-memory agent for *chat_id* (e.g. on /neu).

    The next call to get_agent() will create a fresh agent with empty history.
    """
    _agents.pop(chat_id, None)
    _last_activity.pop(chat_id, None)
    log.info("[chat=%d] Agent cleared from registry", chat_id)
