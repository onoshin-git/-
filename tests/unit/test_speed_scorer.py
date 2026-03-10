"""Unit tests for backend/lib/speed_scorer.py"""

import os
from unittest.mock import patch

from backend.lib.speed_scorer import calculate_speed_score, get_speed_thresholds


class TestGetSpeedThresholds:
    def test_returns_defaults_when_no_env(self):
        thresholds = get_speed_thresholds()
        assert thresholds["t_fast_ms"] == 60_000
        assert thresholds["t_mid_ms"] == 180_000
        assert thresholds["bonus_fast"] == 5
        assert thresholds["bonus_mid"] == 2
        assert thresholds["penalty_slow"] == 0

    def test_reads_from_env(self):
        env = {
            "SPEED_T_FAST_MS": "30000",
            "SPEED_T_MID_MS": "120000",
            "SPEED_BONUS_FAST": "10",
            "SPEED_BONUS_MID": "3",
            "SPEED_PENALTY_SLOW": "-2",
        }
        with patch.dict(os.environ, env):
            thresholds = get_speed_thresholds()
        assert thresholds["t_fast_ms"] == 30_000
        assert thresholds["t_mid_ms"] == 120_000
        assert thresholds["bonus_fast"] == 10
        assert thresholds["penalty_slow"] == -2


class TestCalculateSpeedScore:
    def test_fast_answer(self):
        result = calculate_speed_score(30_000)  # 30s < 60s default
        assert result["speed_label"] == "fast"
        assert result["speed_score"] == 5
        assert result["response_time_ms"] == 30_000

    def test_mid_answer(self):
        result = calculate_speed_score(120_000)  # 2min, between 1min and 3min
        assert result["speed_label"] == "mid"
        assert result["speed_score"] == 2

    def test_slow_answer(self):
        result = calculate_speed_score(300_000)  # 5min > 3min default
        assert result["speed_label"] == "slow"
        assert result["speed_score"] == 0

    def test_boundary_fast(self):
        result = calculate_speed_score(60_000)  # exactly at fast threshold
        assert result["speed_label"] == "fast"

    def test_boundary_mid(self):
        result = calculate_speed_score(180_000)  # exactly at mid threshold
        assert result["speed_label"] == "mid"

    def test_negative_time_treated_as_slow(self):
        result = calculate_speed_score(-1)
        assert result["speed_label"] == "slow"

    def test_invalid_type_treated_as_slow(self):
        result = calculate_speed_score("not_a_number")
        assert result["speed_label"] == "slow"
