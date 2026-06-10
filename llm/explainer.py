"""
LLM explanation layer — multi-provider (Ollama or Gemini).

Dispatches to the backend chosen by LLM_PROVIDER env var:
  - "ollama" (default): local Ollama server, no API key required.
                        Requires `ollama serve` + `ollama pull <model>`.
  - "gemini": Google Gemini REST API, requires GEMINI_API_KEY.

Both backends share the same SYSTEM_PROMPT and _build_llm_payload()
pre-processing step.  explain_routes() returns a human-readable fallback
string (rather than raising) if the selected backend is unreachable or
misconfigured.

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
import re
from typing import Any

import httpx

from config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    LLM_PROVIDER,
    OLLAMA_BASE_URL,
    OLLAMA_MODEL,
)

logger = logging.getLogger(__name__)

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Strict prompt that locks small models onto the explanation task.
# Explicit format + explicit forbiddens prevent data-analysis tangents.
SYSTEM_PROMPT = """
You are a GO Transit assistant explaining bus route options to a commuter.

You will receive a JSON object with:
- "journey": origin → destination string
- "routes": list of route options; each has an "overall_risk" field (Low/Medium/High) and a list of "segments"
- "active_alerts": list of active service alert headlines (may be empty)

Write your response using EXACTLY this structure - no extra headers, no extra sections:

**Option 1:** [route name(s) and any walk transfers, e.g. "Route 27 -> walk -> Route 48"], departs [first segment departs], arrives [last segment arrives], [total_travel_time]
Risk: [copy overall_risk EXACTLY] - [1 sentence: name the specific risk_factors if any, otherwise write "no elevated risk factors"]

**Option 2:** [same format]
...

**Recommendation:** Option [recommended_option] ([its route name]), [one-sentence reason based on that option's risk and travel time]
**Backup plan:** Option [backup_option] ([its route name]), [one sentence on when to fall back to it]

Strict rules — violating any rule is wrong:
1. Copy the "overall_risk" value from each route VERBATIM as the Risk level. NEVER replace it with your own judgment.
2. Use ONLY information present in the JSON. Never invent stops, times, risk levels, or route numbers.
3. Do NOT comment on the JSON structure, data format, technical fields, or implementation.
4. Do NOT add sections like "Summary", "Analysis", "Overview", "Conclusion", "Note", or "Context".
5. Do NOT list "applications", "use cases", or "implications" of the data.
6. Keep each option to 2 lines maximum.
7. If a segment has cancelled=true, write "this trip is cancelled" in that option's risk sentence.
8. If active_alerts is non-empty, name the alert briefly in the affected option's risk sentence.
9. The "recommended_option" and "backup_option" numbers are computed by the routing engine. Copy them EXACTLY — NEVER recommend or fall back to a different option number than the ones given.
10. If "backup_option" is missing from the JSON, write "No backup option available." as the backup plan.
11. ALWAYS end with the **Recommendation:** and **Backup plan:** lines. Never omit them.
""".strip()


# Alert text is external input (GTFS-RT feed) that ends up inside the LLM
# prompt — flatten control characters and cap length as defence in depth
# against prompt injection via crafted alert headers.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
_MAX_ALERT_CHARS = 200


def _sanitise_alert_header(header: str) -> str:
    cleaned = _CONTROL_CHARS_RE.sub(" ", header).strip()
    return cleaned[:_MAX_ALERT_CHARS]


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
    # Cap at 3 routes — llama3.2 loses coherence beyond that and starts
    # referencing option numbers it never described.
    simplified_routes = []

    for idx, route in enumerate(routes_with_scores[:3], 1):
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
                minutes = max(1, round(leg.get("walk_seconds", 0) / 60))
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
        if isinstance(header, str):
            header = _sanitise_alert_header(header)
            if header and header not in alert_headers:
                alert_headers.append(header)

    payload: dict[str, Any] = {
        "journey": f"{origin_name} → {destination_name}",
        "routes": simplified_routes,
        "active_alerts": alert_headers,
    }

    # The recommendation is decided here, deterministically — lowest risk
    # label wins, ties broken by shortest travel time (ADR-004: algorithms
    # decide, the LLM only explains; small models are unreliable at
    # comparing options themselves).
    if simplified_routes:
        label_rank = {"Low": 0, "Medium": 1, "High": 2}
        order = sorted(
            range(len(simplified_routes)),
            key=lambda i: (
                label_rank.get(routes_with_scores[i].get("risk_label"), 3),
                routes_with_scores[i].get("total_travel_seconds", 0),
            ),
        )
        payload["recommended_option"] = order[0] + 1
        if len(order) > 1:
            payload["backup_option"] = order[1] + 1

    return payload


async def _post_llm_request(
    url: str,
    payload: dict[str, Any],
    provider: str,
    unreachable_msg: str,
) -> httpx.Response | str:
    """
    POST to an LLM backend with the shared error handling.

    Returns the successful response, or a human-readable fallback string
    when the backend is unreachable or returns an HTTP error.
    """
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp
    except httpx.ConnectError:
        logger.warning("%s unreachable — explanation skipped. %s", provider, unreachable_msg)
        return unreachable_msg
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "%s returned HTTP %d — explanation skipped.", provider, exc.response.status_code
        )
        return f"Explanation unavailable: {provider} returned HTTP {exc.response.status_code}."


async def _explain_ollama(llm_input: dict[str, Any]) -> str:
    """Send the pre-processed route summary to the local Ollama server."""
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        # Low temperature: this is a formatting/explanation task, not a
        # creative one — higher temperatures make llama3.2 drop required
        # sections.  Matches the 0.2 used for Gemini.
        "options": {"temperature": 0.2},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(llm_input, indent=2)},
        ],
    }

    result = await _post_llm_request(
        f"{OLLAMA_BASE_URL}/api/chat",
        payload,
        provider="Ollama",
        unreachable_msg=(
            f"Explanation unavailable: Ollama is not running at {OLLAMA_BASE_URL}. "
            f"Start it with `ollama serve` and pull the model with "
            f"`ollama pull {OLLAMA_MODEL}`."
        ),
    )
    if isinstance(result, str):
        return result
    resp = result

    try:
        data = resp.json()
        explanation: str = data.get("message", {}).get("content") or ""
    except (ValueError, AttributeError):
        explanation = ""
    if not explanation:
        logger.warning("Ollama returned an unexpected response structure — explanation skipped.")
        return "Explanation unavailable: unexpected response format from Ollama."
    explanation = _normalise_explanation(explanation)
    logger.debug("Ollama explanation generated (%d chars).", len(explanation))
    return explanation


async def _explain_gemini(llm_input: dict[str, Any]) -> str:
    """Send the pre-processed route summary to the Google Gemini REST API."""
    if not GEMINI_API_KEY:
        logger.warning(
            "LLM_PROVIDER=gemini but GEMINI_API_KEY is not set — explanation skipped."
        )
        return (
            "Explanation unavailable: GEMINI_API_KEY is not configured. "
            "Set it in your .env file to enable Gemini explanations."
        )

    url = f"{_GEMINI_BASE}/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "systemInstruction": {
            "parts": [{"text": SYSTEM_PROMPT}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": json.dumps(llm_input, indent=2)}],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1024,
        },
    }

    result = await _post_llm_request(
        url,
        payload,
        provider="Gemini API",
        unreachable_msg="Explanation unavailable: could not reach the Gemini API.",
    )
    if isinstance(result, str):
        return result
    resp = result

    candidates = resp.json().get("candidates", [])
    if not candidates:
        logger.warning("Gemini API returned no candidates — explanation skipped.")
        return "Explanation unavailable: Gemini API returned an empty response."

    try:
        parts = candidates[0].get("content", {}).get("parts", [])
        explanation: str = (parts[0].get("text") if parts else "") or ""
    except (IndexError, AttributeError, TypeError):
        explanation = ""
    if not explanation:
        logger.warning("Gemini API returned an unexpected response structure — explanation skipped.")
        return "Explanation unavailable: unexpected response format from Gemini API."
    explanation = _normalise_explanation(explanation)
    logger.debug("Gemini explanation generated (%d chars).", len(explanation))
    return explanation


async def explain_routes(
    routes_with_scores: list[dict[str, Any]],
    active_alerts: list[dict[str, Any]],
    origin_name: str,
    destination_name: str,
) -> str:
    """
    Generate a plain-language explanation of scored route options.

    Dispatches to the backend chosen by LLM_PROVIDER ("ollama" or "gemini").
    Returns a human-readable fallback string (rather than raising) if the
    selected backend is unreachable or misconfigured.

    Args:
        routes_with_scores: Scored route dicts from the routing engine.
        active_alerts:      Active service alert dicts.
        origin_name:        Human-readable origin stop name.
        destination_name:   Human-readable destination stop name.

    Returns:
        Plain-language explanation string from the selected model.
    """
    llm_input = _build_llm_payload(
        routes_with_scores, active_alerts, origin_name, destination_name
    )

    if LLM_PROVIDER == "gemini":
        return await _explain_gemini(llm_input)
    return await _explain_ollama(llm_input)


_SECTION_RE = re.compile(r"(?<!\n\n)(\*\*(?:Option \d+|Recommendation|Backup plan):)")


def _normalise_explanation(text: str) -> str:
    """
    Ensure a blank line precedes each **Option N:**, **Recommendation:**, and
    **Backup plan:** heading.  Small models frequently omit the blank line,
    collapsing the entire explanation onto one line in the rendered output.
    """
    # Collapse runs of 3+ newlines to 2, then inject \n\n before each section
    # marker that isn't already preceded by a blank line.
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = _SECTION_RE.sub(r"\n\n\1", text)
    return text.strip()
