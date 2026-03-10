"""POST /lv1/complete - 完了レコード保存ハンドラ

改善点:
- タイミングデータ（response_time_ms, speed_score等）をgradesに含めて保存
- risk_flags（AI代理判定結果）をgradesに含めて保存
- score_breakdown（観点別スコア）をgradesに含めて保存
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

RESULTS_TABLE = os.environ.get("RESULTS_TABLE", "ai-levels-results")
PROGRESS_TABLE = os.environ.get("PROGRESS_TABLE", "ai-levels-progress")

UUID_V4_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

REQUIRED_FIELDS = ("session_id", "questions", "answers", "grades", "final_passed")


def _validate_body(body: dict) -> str | None:
    """Validate request body. Returns error message or None if valid."""
    for field in REQUIRED_FIELDS:
        if field not in body:
            return f"{field} is required"

    session_id = body["session_id"]
    if not isinstance(session_id, str) or not UUID_V4_PATTERN.match(session_id):
        return "session_id must be a valid UUID v4"

    if not isinstance(body["questions"], list) or len(body["questions"]) == 0:
        return "questions must be a non-empty list"

    if not isinstance(body["answers"], list) or len(body["answers"]) == 0:
        return "answers must be a non-empty list"

    if not isinstance(body["grades"], list) or len(body["grades"]) == 0:
        return "grades must be a non-empty list"

    if not isinstance(body["final_passed"], bool):
        return "final_passed must be a boolean"

    return None


def _get_dynamodb_resource():
    """Return a DynamoDB resource (extracted for testability)."""
    return boto3.resource("dynamodb", region_name="ap-northeast-1")


def _enrich_grades(grades: list) -> list:
    """gradesにタイミング・判定データが含まれていればそのまま保存する。
    不要なフィールドの除去は行わず、拡張フィールドを許容する。"""
    enriched = []
    for g in grades:
        if not isinstance(g, dict):
            enriched.append(g)
            continue
        grade_item = {
            "passed": g.get("passed"),
            "score": g.get("score"),
            "feedback": g.get("feedback", ""),
            "explanation": g.get("explanation", ""),
        }
        # 拡張フィールド: score_breakdown
        if "score_breakdown" in g and isinstance(g["score_breakdown"], dict):
            grade_item["score_breakdown"] = g["score_breakdown"]
        # 拡張フィールド: タイミング
        if "response_time_ms" in g:
            grade_item["response_time_ms"] = g["response_time_ms"]
        if "speed_score" in g:
            grade_item["speed_score"] = g["speed_score"]
        if "speed_label" in g:
            grade_item["speed_label"] = g["speed_label"]
        # 拡張フィールド: AI代理判定 (float→Decimal変換: DynamoDB互換)
        if "risk_flags" in g and isinstance(g["risk_flags"], dict):
            grade_item["risk_flags"] = json.loads(
                json.dumps(g["risk_flags"]), parse_float=Decimal
            )
        enriched.append(grade_item)
    return enriched


def _save_result(dynamodb, session_id: str, body: dict, completed_at: str) -> str:
    """Save the completion record to ai-levels-results table."""
    table = dynamodb.Table(RESULTS_TABLE)
    total_score = 0
    for g in body["grades"]:
        if isinstance(g, dict) and isinstance(g.get("score"), (int, float)):
            total_score += g["score"]

    enriched_grades = _enrich_grades(body["grades"])

    # メトリクス集計
    metrics = _compute_metrics(enriched_grades)

    table.put_item(Item={
        "PK": f"SESSION#{session_id}",
        "SK": "RESULT#lv1",
        "session_id": session_id,
        "level": "lv1",
        "questions": body["questions"],
        "answers": body["answers"],
        "grades": enriched_grades,
        "final_passed": body["final_passed"],
        "total_score": total_score,
        "completed_at": completed_at,
        "metrics": metrics,
    })


def _compute_metrics(grades: list) -> dict:
    """集計メトリクスを計算する（ログ/監査用）"""
    metrics = {
        "avg_response_time_ms": 0,
        "ai_proxy_flagged_count": 0,
        "total_questions": len(grades),
    }
    response_times = []
    for g in grades:
        if isinstance(g, dict):
            rt = g.get("response_time_ms")
            if isinstance(rt, (int, float)) and rt > 0:
                response_times.append(rt)
            risk = g.get("risk_flags", {})
            if isinstance(risk, dict) and risk.get("ai_proxy_suspected"):
                metrics["ai_proxy_flagged_count"] += 1

    if response_times:
        metrics["avg_response_time_ms"] = int(sum(response_times) / len(response_times))

    return metrics


def _update_progress(dynamodb, session_id: str, final_passed: bool, updated_at: str):
    """Update the lv1_passed flag in ai-levels-progress table."""
    table = dynamodb.Table(PROGRESS_TABLE)
    table.put_item(Item={
        "PK": f"SESSION#{session_id}",
        "SK": "PROGRESS",
        "session_id": session_id,
        "lv1_passed": final_passed,
        "lv2_passed": False,
        "lv3_passed": False,
        "lv4_passed": False,
        "updated_at": updated_at,
    })


def handler(event, context):
    """Lambda handler for POST /lv1/complete."""
    try:
        body = json.loads(event.get("body", "{}"))
    except json.JSONDecodeError:
        return {
            "statusCode": 400,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": "Invalid JSON in request body"}),
        }

    error = _validate_body(body)
    if error:
        return {
            "statusCode": 400,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": error}),
        }

    session_id = body["session_id"]
    now = datetime.now(timezone.utc).isoformat()

    try:
        dynamodb = _get_dynamodb_resource()
        _save_result(dynamodb, session_id, body, now)
        _update_progress(dynamodb, session_id, body["final_passed"], now)
    except (ClientError, TypeError) as e:
        logger.error("DynamoDB write failed: %s", str(e))
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({
                "error": "データの保存に失敗しました。リトライしてください。"
            }),
        }

    return {
        "statusCode": 200,
        "headers": {"Access-Control-Allow-Origin": "*"},
        "body": json.dumps({
            "saved": True,
            "record_id": f"SESSION#{session_id}",
        }),
    }
