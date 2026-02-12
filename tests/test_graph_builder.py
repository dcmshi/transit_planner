"""
Unit tests for graph.builder pure functions.

_haversine_metres and _hms_to_seconds are private helpers but are
tested directly since they encapsulate meaningful logic.
"""

import pytest
from graph.builder import _haversine_metres, _hms_to_seconds


class TestHaversineMetres:
    def test_same_point_is_zero(self):
        assert _haversine_metres(43.6, -79.4, 43.6, -79.4) == pytest.approx(0.0, abs=0.01)

    def test_symmetry(self):
        d1 = _haversine_metres(43.6453, -79.3806, 43.5448, -80.2482)
        d2 = _haversine_metres(43.5448, -80.2482, 43.6453, -79.3806)
        assert d1 == pytest.approx(d2, rel=1e-6)

    def test_union_to_guelph_approx_70km(self):
        # Union Station (43.6453, -79.3806) → Guelph Central (43.5448, -80.2482)
        # Great-circle distance should be roughly 70 km
        d = _haversine_metres(43.6453, -79.3806, 43.5448, -80.2482)
        assert 65_000 < d < 80_000

    def test_short_walk_within_stop_radius(self):
        # ~0.003° latitude ≈ 333 m — should be within MAX_WALK_METRES (500)
        d = _haversine_metres(43.6453, -79.3806, 43.6483, -79.3806)
        assert 300 < d < 400

    def test_far_apart_exceeds_walk_radius(self):
        # Two stops ~5 km apart — clearly beyond MAX_WALK_METRES
        d = _haversine_metres(43.6453, -79.3806, 43.6, -79.34)
        assert d > 500

    def test_equator_meridian_crossing(self):
        # Sanity check with non-Toronto coordinates
        # (0°, 0°) → (0°, 1°) ≈ 111 km
        d = _haversine_metres(0.0, 0.0, 0.0, 1.0)
        assert 110_000 < d < 112_000


class TestHmsToSecondsBuilder:
    """Tests for the _hms_to_seconds copy in graph/builder.py."""

    def test_normal_time(self):
        assert _hms_to_seconds("08:30:00") == 8 * 3600 + 30 * 60

    def test_midnight(self):
        assert _hms_to_seconds("00:00:00") == 0

    def test_over_24h(self):
        assert _hms_to_seconds("25:10:00") == 25 * 3600 + 10 * 60

    def test_with_seconds(self):
        assert _hms_to_seconds("09:05:30") == 9 * 3600 + 5 * 60 + 30

    def test_invalid_returns_zero(self):
        assert _hms_to_seconds("bad") == 0

    def test_empty_returns_zero(self):
        assert _hms_to_seconds("") == 0
