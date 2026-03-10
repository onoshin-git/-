"""Unit tests for backend/lib/ai_proxy_detector.py"""

import json
import os
from unittest.mock import patch

from backend.lib.ai_proxy_detector import detect_ai_proxy, is_detection_enabled


class TestIsDetectionEnabled:
    def test_default_is_true(self):
        assert is_detection_enabled() is True

    def test_env_false(self):
        with patch.dict(os.environ, {"AI_PROXY_DETECTION_ENABLED": "false"}):
            assert is_detection_enabled() is False


class TestDetectAiProxy:
    def test_disabled_returns_not_suspected(self):
        with patch.dict(os.environ, {"AI_PROXY_DETECTION_ENABLED": "false"}):
            result = detect_ai_proxy("Q?", "My answer here that is long enough")
        assert result["ai_proxy_suspected"] is False
        assert result["confidence"] == 0.0
        assert result["detection_error"] is None

    def test_short_answer_skips_detection(self):
        result = detect_ai_proxy("Q?", "Short")
        assert result["ai_proxy_suspected"] is False
        assert result["rationale"] == "Short answer, skipped detection"

    @patch("backend.lib.ai_proxy_detector.invoke_claude")
    def test_fail_open_on_bedrock_error(self, mock_invoke):
        """Detection failure should NOT raise - fail-open behavior."""
        mock_invoke.side_effect = RuntimeError("Bedrock unavailable")
        result = detect_ai_proxy("Q?", "This is a sufficiently long answer for detection to proceed")
        assert result["ai_proxy_suspected"] is False
        assert result["detection_error"] is not None
        assert "Bedrock unavailable" in result["detection_error"]

    @patch("backend.lib.ai_proxy_detector.invoke_claude")
    def test_returns_detection_result(self, mock_invoke):
        mock_invoke.return_value = {
            "content": [{"text": json.dumps({
                "ai_proxy_suspected": True,
                "confidence": 0.9,
                "rationale": "Highly structured response",
            })}]
        }
        result = detect_ai_proxy("Q?", "This is a sufficiently long answer for detection to proceed")
        assert result["ai_proxy_suspected"] is True
        assert result["confidence"] == 0.9
        assert result["detection_error"] is None

    @patch("backend.lib.ai_proxy_detector.invoke_claude")
    def test_below_threshold_not_suspected(self, mock_invoke):
        """If confidence < threshold, suspected should be False."""
        mock_invoke.return_value = {
            "content": [{"text": json.dumps({
                "ai_proxy_suspected": True,
                "confidence": 0.3,
                "rationale": "Low confidence",
            })}]
        }
        result = detect_ai_proxy("Q?", "This is a sufficiently long answer for detection to proceed")
        assert result["ai_proxy_suspected"] is False  # Below default 0.7 threshold
