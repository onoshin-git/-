"""Unit tests for backend/handlers/timer_handler.py"""

import json
from unittest.mock import patch, MagicMock

from backend.handlers.timer_handler import (
    start_question_handler,
    server_time_handler,
)


def _api_event(body: dict) -> dict:
    return {"body": json.dumps(body)}


VALID_SESSION_ID = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"


class TestServerTimeHandler:
    def test_returns_200_with_server_time(self):
        resp = server_time_handler({}, None)
        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])
        assert "server_time" in data
        assert "server_time_ms" in data
        assert isinstance(data["server_time_ms"], int)

    def test_cors_header_present(self):
        resp = server_time_handler({}, None)
        assert resp["headers"]["Access-Control-Allow-Origin"] == "*"


class TestStartQuestionHandler:
    @patch("backend.handlers.timer_handler._get_dynamodb_resource")
    def test_returns_200_on_new_question_start(self, mock_ddb):
        mock_table = MagicMock()
        mock_ddb.return_value.Table.return_value = mock_table

        resp = start_question_handler(_api_event({
            "session_id": VALID_SESSION_ID,
            "step": 1,
        }), None)

        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])
        assert data["session_id"] == VALID_SESSION_ID
        assert data["step"] == 1
        assert "started_at" in data
        assert "started_at_ms" in data

    @patch("backend.handlers.timer_handler._get_dynamodb_resource")
    def test_idempotent_returns_existing_started_at(self, mock_ddb):
        """When started_at already exists, returns the existing value (idempotent)."""
        from botocore.exceptions import ClientError

        mock_table = MagicMock()
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
            "PutItem",
        )
        mock_table.get_item.return_value = {
            "Item": {"started_at": "2024-01-01T00:00:00Z", "started_at_ms": 1704067200000}
        }
        mock_ddb.return_value.Table.return_value = mock_table

        resp = start_question_handler(_api_event({
            "session_id": VALID_SESSION_ID,
            "step": 1,
        }), None)

        assert resp["statusCode"] == 200
        data = json.loads(resp["body"])
        assert data["started_at_ms"] == 1704067200000

    def test_returns_400_for_invalid_session_id(self):
        resp = start_question_handler(_api_event({
            "session_id": "not-a-uuid",
            "step": 1,
        }), None)
        assert resp["statusCode"] == 400

    def test_returns_400_for_missing_step(self):
        resp = start_question_handler(_api_event({
            "session_id": VALID_SESSION_ID,
        }), None)
        assert resp["statusCode"] == 400

    def test_returns_400_for_invalid_json(self):
        resp = start_question_handler({"body": "not json"}, None)
        assert resp["statusCode"] == 400

    def test_returns_400_for_negative_step(self):
        resp = start_question_handler(_api_event({
            "session_id": VALID_SESSION_ID,
            "step": -1,
        }), None)
        assert resp["statusCode"] == 400
