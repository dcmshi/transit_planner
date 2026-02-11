"""
LLM explanation layer.

Uses Claude to generate plain-language summaries of scored route options.

STRICT SCOPE (per CLAUDE.md):
  - LLM receives structured route + risk JSON as input only.
  - LLM outputs plain-language explanations, tradeoff summaries,
    and explicit fallback instructions.
  - LLM must NEVER generate routes, invent transit data, or override
    deterministic scoring logic.
  - All outputs must be traceable to the provided input JSON.
"""

import json
import logging
from typing import Any

import anthropic

from config import ANTHROPIC_API_KEY, LLM_MODEL

logger = logging.getLogger(__name__)

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not configured. Set it in your .env file.")
        _client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    return _client


SYSTEM_PROMPT = """
You are a transit assistant that explains GO bus route options to commuters.

You will receive structured JSON containing:
- A list of candidate routes (legs, stops, departure/arrival times)
- A per-leg risk score and label (Low / Medium / High)
- Active service alerts
- Transfer details

Your job is to:
1. Summarise each route option in plain language.
2. Highlight the key reliability risks for each option.
3. Recommend the best option with a brief rationale.
4. Provide a concrete fallback plan if the recommended route fails.

Rules:
- Base ALL statements strictly on the provided JSON data.
- Do NOT invent stops, times, route numbers, or delays not in the input.
- Do NOT recommend routes that are not in the input.
- Keep explanations concise (2–4 sentences per route).
- If all routes are high-risk, say so clearly.
""".strip()


async def explain_routes(
    routes_with_scores: list[dict[str, Any]],
    active_alerts: list[dict[str, Any]],
    origin_name: str,
    destination_name: str,
) -> str:
    """
    Generate a plain-language explanation of scored route options.

    Args:
        routes_with_scores: List of dicts, each containing:
            - "legs": list of leg dicts
            - "risk_score": float
            - "risk_label": str
            - "total_travel_seconds": int
            - "modifiers": list[str]
        active_alerts: List of ServiceAlertState-like dicts.
        origin_name: Human-readable origin stop name.
        destination_name: Human-readable destination stop name.

    Returns:
        Plain-language string explanation from Claude.
    """
    user_content = json.dumps({
        "origin": origin_name,
        "destination": destination_name,
        "routes": routes_with_scores,
        "active_alerts": active_alerts,
    }, indent=2)

    client = _get_client()
    message = await client.messages.create(
        model=LLM_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )

    explanation = message.content[0].text
    logger.debug("LLM explanation generated (%d chars).", len(explanation))
    return explanation
