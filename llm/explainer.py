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

# Strict prompt that locks small models onto the explanation task.
# Explicit format + explicit forbiddens prevent data-analysis tangents.
SYSTEM_PROMPT = """
You are a GO Transit assistant explaining bus route options to a commuter.

You will receive a JSON object with:
- "journey": origin → destination string
- "routes": list of route options; each has an "overall_risk" field (Low/Medium/High) and a list of "segments"
- "active_alerts": list of active service alert headlines (may be empty)

Write your response using EXACTLY this structure — no extra headers, no extra sections:

**Option 1:** [route name(s) and any walk transfers, e.g. "Route 27 → walk → Route 48"], departs [first segment departs], arrives [last segment arrives], [total_travel_time]
Risk: [copy overall_risk EXACTLY] — [1 sentence: name the specific risk_factors if any, otherwise write "no elevated risk factors"]

**Option 2:** [same format]
...

**Recommendation:** [option number and route, one-sentence reason based on risk and travel time]
**Backup plan:** [one sentence: which other option to fall back to and why]

Strict rules — violating any rule is wrong:
1. Copy the "overall_risk" value from each route VERBATIM as the Risk level. NEVER replace it with your own judgment.
2. Use ONLY information present in the JSON. Never invent stops, times, risk levels, or route numbers.
3. Do NOT comment on the JSON structure, data format, technical fields, or implementation.
4. Do NOT add sections like "Summary", "Analysis", "Overview", "Conclusion", "Note", or "Context".
5. Do NOT list "applications", "use cases", or "implications" of the data.
6. Keep each option to 2 lines maximum.
7. If a segment has cancelled=true, write "this trip is cancelled" in that option's risk sentence.
8. If active_alerts is non-empty, name the alert briefly in the affected option's risk sentence.
""".strip()


def _route_number(route_id: str) -> str:
    """Extract short route number from full route_id (e.g. '01260426-27' → 'Route 27')."""
    suffix = route_id.rsplit("-", 1)[-1].lstrip("0")
    return f"Route {suffix or route_id}"


def _hhmm(time_str: str) -> str:
    """Trim HH:MM:SS (or already HH:MM) to HH:MM."""
    return time_str[:5] if time_str else time_str


def _build_llm_payload(
    routes_with_scores: list[dict[str, Any]],
    active_alerts: list[dict[str, Any]],
    origin_name: str,
    destination_name: str,
) -> dict[str, Any]:
    """
    Collapse raw scored route data into a compact summary for a small LLM.

    Transformations applied:
    - Consecutive legs on the same trip_id are merged into a single bus segment
      (board at first stop, alight at last stop on that trip).
    - Internal IDs (trip_id, service_id, stop_ids) are stripped.
    - Times are trimmed to HH:MM.
    - Risk modifiers across merged legs are de-duplicated and combined.
    - Service alerts are reduced to header text only.
    """
    simplified_routes = []

    for idx, route in enumerate(routes_with_scores, 1):
        segments: list[dict[str, Any]] = []
        legs = route.get("legs", [])
        i = 0

        while i < len(legs):
            leg = legs[i]

            if leg.get("kind") == "trip":
                trip_id = leg.get("trip_id")
                route_id = leg.get("route_id", "")
                from_stop: str = leg["from_stop_name"]
                to_stop: str = leg["to_stop_name"]
                departs = _hhmm(leg["departure_time"])
                arrives = _hhmm(leg["arrival_time"])
                max_risk: float = leg["risk"]["risk_score"]
                risk_label: str = leg["risk"]["risk_label"]
                modifiers: list[str] = list(leg["risk"].get("modifiers", []))
                is_cancelled: bool = bool(leg["risk"].get("is_cancelled", False))

                # Merge subsequent legs on the same physical trip
                j = i + 1
                while (
                    j < len(legs)
                    and legs[j].get("kind") == "trip"
                    and legs[j].get("trip_id") == trip_id
                ):
                    nxt = legs[j]
                    to_stop = nxt["to_stop_name"]
                    arrives = _hhmm(nxt["arrival_time"])
                    if nxt["risk"]["risk_score"] > max_risk:
                        max_risk = nxt["risk"]["risk_score"]
                        risk_label = nxt["risk"]["risk_label"]
                    for m in nxt["risk"].get("modifiers", []):
                        if m not in modifiers:
                            modifiers.append(m)
                    if nxt["risk"].get("is_cancelled"):
                        is_cancelled = True
                    j += 1

                seg: dict[str, Any] = {
                    "type": "bus",
                    "route": _route_number(route_id),
                    "board_at": from_stop,
                    "alight_at": to_stop,
                    "departs": departs,
                    "arrives": arrives,
                    "risk": risk_label,
                    "risk_score": round(max_risk, 2),
                }
                if modifiers:
                    seg["risk_factors"] = modifiers
                if is_cancelled:
                    seg["cancelled"] = True
                segments.append(seg)
                i = j

            elif leg.get("kind") == "walk":
                minutes = max(1, round(leg.get("duration_s", 0) / 60))
                metres = round(leg.get("distance_m", 0))
                segments.append({
                    "type": "walk_transfer",
                    "from": leg["from_stop_name"],
                    "to": leg["to_stop_name"],
                    "duration": f"{minutes} min ({metres} m)",
                })
                i += 1
            else:
                i += 1

        travel_min = round(route.get("total_travel_seconds", 0) / 60)
        simplified_routes.append({
            "option": idx,
            # overall_risk is pre-computed by the routing engine — copy it verbatim
            "overall_risk": route.get("risk_label", "Unknown"),
            "total_travel_time": f"{travel_min} min",
            "transfers": route.get("transfers", 0),
            "segments": segments,
        })

    # Reduce alerts to header text only — nested structures confuse small models
    alert_headers: list[str] = []
    for alert in active_alerts:
        if not isinstance(alert, dict):
            continue
        header = (
            alert.get("header_text")
            or alert.get("header")
            or (alert.get("alert") or {}).get("header_text")
        )
        if isinstance(header, str) and header.strip() and header not in alert_headers:
            alert_headers.append(header.strip())

    return {
        "journey": f"{origin_name} → {destination_name}",
        "routes": simplified_routes,
        "active_alerts": alert_headers,
    }


async def explain_routes(
    routes_with_scores: list[dict[str, Any]],
    active_alerts: list[dict[str, Any]],
    origin_name: str,
    destination_name: str,
) -> str:
    """
    Generate a plain-language explanation of scored route options.

    Sends a pre-processed (collapsed, stripped) route summary to the local
    Ollama server and returns the model's plain-language response.  Returns a
    human-readable fallback message (rather than raising) if Ollama is
    unreachable or returns an error — the rest of the API response is still
    valid in that case.

    Args:
        routes_with_scores: Scored route dicts from the routing engine.
        active_alerts:      Active service alert dicts.
        origin_name:        Human-readable origin stop name.
        destination_name:   Human-readable destination stop name.

    Returns:
        Plain-language explanation string from the local model.
    """
    llm_input = _build_llm_payload(
        routes_with_scores, active_alerts, origin_name, destination_name
    )

    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(llm_input, indent=2)},
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
            OLLAMA_BASE_URL,
            OLLAMA_MODEL,
        )
        return (
            f"Explanation unavailable: Ollama is not running at {OLLAMA_BASE_URL}. "
            f"Start it with `ollama serve` and pull the model with "
            f"`ollama pull {OLLAMA_MODEL}`."
        )
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Ollama returned HTTP %d — explanation skipped.", exc.response.status_code
        )
        return f"Explanation unavailable: Ollama returned HTTP {exc.response.status_code}."

    explanation: str = resp.json()["message"]["content"]
    logger.debug("Ollama explanation generated (%d chars).", len(explanation))
    return explanation
