"""
LLM explanation layer — local Ollama backend.

Uses a locally-running Ollama server to generate plain-language summaries
of scored route options.  No API key or cloud account required; Ollama
must be running at OLLAMA_BASE_URL (default: http://localhost:11434).

Quick setup:
    # Install Ollama: https://ollama.com
    ollama pull llama3.2          # fast, ~2 GB — good enough for explanation
    ollama pull llama3.1:8b       # better quality, ~4.5 GB (optional)

STRICT SCOPE (per CLAUDE.md / design principles):
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

import httpx

from config import OLLAMA_BASE_URL, OLLAMA_MODEL

logger = logging.getLogger(__name__)

# Keep the system prompt strict so smaller models stay on-task.
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

    Sends structured route JSON to the local Ollama server and returns the
    model's plain-language response.  Returns a human-readable fallback
    message (rather than raising) if Ollama is unreachable or returns an
    error — the rest of the API response is still valid in that case.

    Args:
        routes_with_scores: Scored route dicts from the routing engine.
        active_alerts:      Active service alert dicts.
        origin_name:        Human-readable origin stop name.
        destination_name:   Human-readable destination stop name.

    Returns:
        Plain-language explanation string from the local model.
    """
    user_content = json.dumps(
        {
            "origin": origin_name,
            "destination": destination_name,
            "routes": routes_with_scores,
            "active_alerts": active_alerts,
        },
        indent=2,
    )

    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json=payload,
            )
            resp.raise_for_status()
    except httpx.ConnectError:
        logger.warning(
            "Ollama unreachable at %s — explanation skipped. "
            "Run `ollama serve` and `ollama pull %s` to enable it.",
            OLLAMA_BASE_URL, OLLAMA_MODEL,
        )
        return (
            f"Explanation unavailable: Ollama is not running at {OLLAMA_BASE_URL}. "
            f"Start it with `ollama serve` and pull the model with "
            f"`ollama pull {OLLAMA_MODEL}`."
        )
    except httpx.HTTPStatusError as exc:
        logger.warning("Ollama returned HTTP %d — explanation skipped.", exc.response.status_code)
        return f"Explanation unavailable: Ollama returned HTTP {exc.response.status_code}."

    explanation: str = resp.json()["message"]["content"]
    logger.debug("Ollama explanation generated (%d chars).", len(explanation))
    return explanation
