"""
Unit tests for llm/explainer.py.

All HTTP calls are mocked with unittest.mock so no live Ollama or Gemini
server is needed.  LLM_PROVIDER / GEMINI_API_KEY / OLLAMA_* config values
are monkey-patched per-test via importlib or direct module attribute writes.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import llm.explainer as explainer_mod
from llm.explainer import (
    _build_llm_payload,
    _hhmm,
    _normalise_explanation,
    _route_number,
    explain_routes,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trip_leg(
    from_name="Stop A",
    to_name="Stop B",
    trip_id="T1",
    route_id="R-27",
    departs="08:00:00",
    arrives="08:30:00",
    risk_score=0.1,
    risk_label="Low",
):
    return {
        "kind": "trip",
        "from_stop_id": "A",
        "to_stop_id": "B",
        "from_stop_name": from_name,
        "to_stop_name": to_name,
        "trip_id": trip_id,
        "route_id": route_id,
        "service_id": "SVC1",
        "departure_time": departs,
        "arrival_time": arrives,
        "travel_seconds": 1800,
        "risk": {
            "risk_score": risk_score,
            "risk_label": risk_label,
            "modifiers": [],
            "is_cancelled": False,
        },
    }


def _make_walk_leg(from_name="Stop B", to_name="Stop C", walk_seconds=300, distance_m=375):
    return {
        "kind": "walk",
        "from_stop_id": "B",
        "to_stop_id": "C",
        "from_stop_name": from_name,
        "to_stop_name": to_name,
        "walk_seconds": walk_seconds,
        "distance_m": distance_m,
    }


def _make_route(legs, risk_label="Low", risk_score=0.1, total_travel_seconds=1800, transfers=0):
    return {
        "legs": legs,
        "risk_label": risk_label,
        "risk_score": risk_score,
        "total_travel_seconds": total_travel_seconds,
        "transfers": transfers,
        "total_walk_metres": 0.0,
    }


# ---------------------------------------------------------------------------
# _route_number
# ---------------------------------------------------------------------------

class TestRouteNumber:
    def test_strips_prefix_and_leading_zeros(self):
        assert _route_number("01260426-27") == "Route 27"

    def test_single_segment(self):
        assert _route_number("27") == "Route 27"

    def test_no_leading_zeros(self):
        assert _route_number("prefix-100") == "Route 100"

    def test_all_zeros_suffix_falls_back_to_full_id(self):
        # lstrip("0") of "00" → "", so falls back to original route_id
        assert _route_number("prefix-00") == "Route prefix-00"


# ---------------------------------------------------------------------------
# _hhmm
# ---------------------------------------------------------------------------

class TestHhmm:
    def test_trims_seconds(self):
        assert _hhmm("08:30:00") == "08:30"

    def test_already_hhmm(self):
        assert _hhmm("08:30") == "08:30"

    def test_empty_string(self):
        assert _hhmm("") == ""

    def test_none_passthrough(self):
        assert _hhmm(None) is None


# ---------------------------------------------------------------------------
# _build_llm_payload
# ---------------------------------------------------------------------------

class TestBuildLlmPayload:
    def test_basic_single_trip_leg(self):
        route = _make_route([_make_trip_leg()])
        payload = _build_llm_payload([route], [], "Origin", "Dest")
        assert payload["journey"] == "Origin → Dest"
        assert len(payload["routes"]) == 1
        seg = payload["routes"][0]["segments"][0]
        assert seg["type"] == "bus"
        assert seg["route"] == "Route 27"
        assert seg["departs"] == "08:00"
        assert seg["arrives"] == "08:30"

    def test_walk_leg_uses_walk_seconds(self):
        route = _make_route([_make_walk_leg(walk_seconds=600, distance_m=450)])
        payload = _build_llm_payload([route], [], "O", "D")
        seg = payload["routes"][0]["segments"][0]
        assert seg["type"] == "walk_transfer"
        assert "10 min" in seg["duration"]
        assert "450 m" in seg["duration"]

    def test_consecutive_same_trip_legs_merged(self):
        leg1 = _make_trip_leg(from_name="A", to_name="B", trip_id="T1", departs="08:00:00", arrives="08:15:00")
        leg2 = _make_trip_leg(from_name="B", to_name="C", trip_id="T1", departs="08:15:00", arrives="08:30:00")
        route = _make_route([leg1, leg2])
        payload = _build_llm_payload([route], [], "O", "D")
        segs = payload["routes"][0]["segments"]
        assert len(segs) == 1
        assert segs[0]["board_at"] == "A"
        assert segs[0]["alight_at"] == "C"
        assert segs[0]["arrives"] == "08:30"

    def test_capped_at_three_routes(self):
        route = _make_route([_make_trip_leg()])
        payload = _build_llm_payload([route] * 5, [], "O", "D")
        assert len(payload["routes"]) == 3

    def test_alert_header_extracted(self):
        alert = {"header_text": "Service disruption on Route 27"}
        payload = _build_llm_payload([], [alert], "O", "D")
        assert "Service disruption on Route 27" in payload["active_alerts"]

    def test_duplicate_alert_headers_deduplicated(self):
        alert = {"header_text": "Same alert"}
        payload = _build_llm_payload([], [alert, alert], "O", "D")
        assert payload["active_alerts"].count("Same alert") == 1

    def test_cancelled_flag_propagated(self):
        leg = _make_trip_leg()
        leg["risk"]["is_cancelled"] = True
        route = _make_route([leg])
        payload = _build_llm_payload([route], [], "O", "D")
        assert payload["routes"][0]["segments"][0].get("cancelled") is True

    def test_risk_modifiers_included(self):
        leg = _make_trip_leg()
        leg["risk"]["modifiers"] = ["late evening", "historical delay"]
        route = _make_route([leg])
        payload = _build_llm_payload([route], [], "O", "D")
        seg = payload["routes"][0]["segments"][0]
        assert "late evening" in seg["risk_factors"]

    def test_empty_routes_list(self):
        payload = _build_llm_payload([], [], "O", "D")
        assert payload["routes"] == []
        assert payload["active_alerts"] == []


# ---------------------------------------------------------------------------
# _normalise_explanation
# ---------------------------------------------------------------------------

class TestNormaliseExplanation:
    def test_injects_blank_line_before_option(self):
        text = "Some text\n**Option 1:** foo"
        result = _normalise_explanation(text)
        assert "\n\n**Option 1:**" in result

    def test_already_normalised_unchanged(self):
        text = "Some text\n\n**Option 1:** foo\n\n**Recommendation:** bar"
        result = _normalise_explanation(text)
        # Should not double-inject blank lines
        assert "\n\n\n" not in result

    def test_collapses_triple_newlines(self):
        text = "a\n\n\n\nb"
        result = _normalise_explanation(text)
        assert "\n\n\n" not in result

    def test_strips_leading_trailing_whitespace(self):
        text = "\n\n  hello  \n\n"
        result = _normalise_explanation(text)
        assert result == "hello"


# ---------------------------------------------------------------------------
# _explain_ollama (via explain_routes with LLM_PROVIDER=ollama)
# ---------------------------------------------------------------------------

class TestExplainOllama:
    @pytest.fixture(autouse=True)
    def set_ollama_provider(self):
        with patch.object(explainer_mod, "LLM_PROVIDER", "ollama"):
            yield

    @pytest.mark.anyio
    async def test_happy_path_returns_explanation(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"message": {"content": "**Option 1:** Route 27"}}
        mock_resp.raise_for_status = MagicMock()

        with patch("llm.explainer.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await explain_routes([], [], "Origin", "Dest")

        assert "Option 1" in result

    @pytest.mark.anyio
    async def test_connect_error_returns_fallback(self):
        import httpx

        with patch("llm.explainer.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await explain_routes([], [], "Origin", "Dest")

        assert "Explanation unavailable" in result
        assert "Ollama" in result

    @pytest.mark.anyio
    async def test_http_error_returns_fallback(self):
        import httpx

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        http_err = httpx.HTTPStatusError("500", request=MagicMock(), response=mock_resp)

        with patch("llm.explainer.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=http_err)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await explain_routes([], [], "Origin", "Dest")

        assert "HTTP 500" in result


# ---------------------------------------------------------------------------
# _explain_gemini (via explain_routes with LLM_PROVIDER=gemini)
# ---------------------------------------------------------------------------

class TestExplainGemini:
    @pytest.fixture(autouse=True)
    def set_gemini_provider(self):
        with (
            patch.object(explainer_mod, "LLM_PROVIDER", "gemini"),
            patch.object(explainer_mod, "GEMINI_API_KEY", "test-key"),
            patch.object(explainer_mod, "GEMINI_MODEL", "gemini-2.5-flash"),
        ):
            yield

    @pytest.mark.anyio
    async def test_happy_path_returns_explanation(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "candidates": [
                {"content": {"parts": [{"text": "**Option 1:** Route 27"}]}}
            ]
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("llm.explainer.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await explain_routes([], [], "Origin", "Dest")

        assert "Option 1" in result

    @pytest.mark.anyio
    async def test_url_includes_model_and_key(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}]
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("llm.explainer.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await explain_routes([], [], "O", "D")

        called_url = mock_client.post.call_args[0][0]
        assert "gemini-2.5-flash" in called_url
        assert "key=test-key" in called_url

    @pytest.mark.anyio
    async def test_missing_api_key_returns_fallback(self):
        with patch.object(explainer_mod, "GEMINI_API_KEY", ""):
            result = await explain_routes([], [], "Origin", "Dest")

        assert "Explanation unavailable" in result
        assert "GEMINI_API_KEY" in result

    @pytest.mark.anyio
    async def test_connect_error_returns_fallback(self):
        import httpx

        with patch("llm.explainer.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await explain_routes([], [], "Origin", "Dest")

        assert "Explanation unavailable" in result
        assert "Gemini" in result

    @pytest.mark.anyio
    async def test_http_error_returns_fallback(self):
        import httpx

        mock_resp = MagicMock()
        mock_resp.status_code = 429
        http_err = httpx.HTTPStatusError("429", request=MagicMock(), response=mock_resp)

        with patch("llm.explainer.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=http_err)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await explain_routes([], [], "Origin", "Dest")

        assert "HTTP 429" in result

    @pytest.mark.anyio
    async def test_empty_candidates_returns_fallback(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"candidates": []}
        mock_resp.raise_for_status = MagicMock()

        with patch("llm.explainer.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await explain_routes([], [], "Origin", "Dest")

        assert "Explanation unavailable" in result
        assert "empty" in result

    @pytest.mark.anyio
    async def test_system_instruction_in_payload(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}]
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("llm.explainer.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await explain_routes([], [], "O", "D")

        sent_payload = mock_client.post.call_args[1]["json"]
        assert "systemInstruction" in sent_payload
        assert sent_payload["systemInstruction"]["parts"][0]["text"] == explainer_mod.SYSTEM_PROMPT
