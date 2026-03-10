"""タイマー関連ハンドラ - サーバー時刻基準の回答時間計測

POST /lv1/start-question : 設問開始時刻をサーバー側で記録（冪等）
GET  /lv1/server-time    : 現在のサーバー時刻を返却
"""

import json
import logging
import os
import re
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

PROGRESS_TABLE = os.environ.get("PROGRESS_TABLE", "ai-levels-progress")
CORS_HEADERS = {"Access-Control-Allow-Origin": "*"}

UUID_V4_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _get_dynamodb_resource():
    """Return a DynamoDB resource (extracted for testability)."""
    return boto3.resource("dynamodb", region_name="ap-northeast-1")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_epoch_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def start_question_handler(event, context):
    """POST /lv1/start-question - 設問開始時刻を記録（冪等）。

    Body: { "session_id": str, "step": int }
    冪等性: 同じ session_id + step の組み合わせで既に started_at がある場合は上書きしない。
    """
    try:
        body = json.loads(event.get("body", "{}"))
    except json.JSONDecodeError:
        return {
            "statusCode": 400,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "Invalid JSON in request body"}),
        }

    session_id = body.get("session_id")
    step = body.get("step")

    if not session_id or not isinstance(session_id, str) or not UUID_V4_PATTERN.match(session_id):
        return {
            "statusCode": 400,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "session_id must be a valid UUID v4"}),
        }

    if not isinstance(step, int) or step < 1:
        return {
            "statusCode": 400,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "step must be a positive integer"}),
        }

    now_iso = _now_iso()
    now_ms = _now_epoch_ms()

    try:
        dynamodb = _get_dynamodb_resource()
        table = dynamodb.Table(PROGRESS_TABLE)

        # 冪等: attribute_not_exists で既存レコードがある場合は更新しない
        pk = f"SESSION#{session_id}"
        sk = f"TIMER#lv1#step{step}"

        try:
            table.put_item(
                Item={
                    "PK": pk,
                    "SK": sk,
                    "session_id": session_id,
                    "step": step,
                    "started_at": now_iso,
                    "started_at_ms": now_ms,
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
            started_at_ms = now_ms
            started_at = now_iso
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # 既に記録済み → 既存値を返す
                resp = table.get_item(Key={"PK": pk, "SK": sk})
                item = resp.get("Item", {})
                started_at_ms = int(item.get("started_at_ms", now_ms))
                started_at = item.get("started_at", now_iso)
            else:
                raise

    except ClientError as e:
        logger.error("DynamoDB write failed: %s", str(e))
        return {
            "statusCode": 500,
            "headers": CORS_HEADERS,
            "body": json.dumps({"error": "タイマー開始の記録に失敗しました。"}),
        }

    return {
        "statusCode": 200,
        "headers": CORS_HEADERS,
        "body": json.dumps({
            "session_id": session_id,
            "step": step,
            "started_at": started_at,
            "started_at_ms": started_at_ms,
            "server_time": _now_iso(),
            "server_time_ms": _now_epoch_ms(),
        }),
    }


def server_time_handler(event, context):
    """GET /lv1/server-time - サーバー時刻を返却。"""
    return {
        "statusCode": 200,
        "headers": CORS_HEADERS,
        "body": json.dumps({
            "server_time": _now_iso(),
            "server_time_ms": _now_epoch_ms(),
        }),
    }
