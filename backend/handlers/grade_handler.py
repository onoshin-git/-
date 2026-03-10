"""POST /lv1/grade - 回答採点+レビューハンドラ

改善点:
- サーバー時刻基準のタイマー連動（started_at取得 → submitted_at記録 → response_time_ms計算）
- AI代理回答判定（risk_flags保存）
- 意図理解重視の採点（日本語力で差が出ない）
"""

import json
import logging
import os
import re
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from backend.lib.bedrock_client import invoke_claude, strip_code_fence
from backend.lib.threshold_resolver import resolve_passed
from backend.lib.speed_scorer import calculate_speed_score
from backend.lib.ai_proxy_detector import detect_ai_proxy, show_to_user

logger = logging.getLogger(__name__)

PROGRESS_TABLE = os.environ.get("PROGRESS_TABLE", "ai-levels-progress")

UUID_V4_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# --- 意図理解重視の採点プロンプト ---
SYSTEM_PROMPT = """あなたはAIカリキュラム「分業設計×依頼設計×品質担保×2ケース再現」の採点エージェントです。

ユーザーの回答を設問に照らして採点し、フィードバックと解説も同時に生成してください。

## 採点基準（重要：以下の観点で採点すること）

### 1. 意図理解 (intent_understanding) - 配点25%
設問が何を問うているかを正しく把握し、的を射た回答になっているか。

### 2. 要点網羅 (coverage) - 配点25%
必要な論点・観点を漏れなくカバーしているか。部分的でも重要な点に触れていれば評価する。

### 3. 構造化 (structure) - 配点25%
回答が論理的に構成されているか。箇条書きでなくても、考えの筋道が通っていれば高評価。

### 4. 実務妥当性 (practical_relevance) - 配点25%
実務で通用する具体性・現実性があるか。机上の空論でなく、実行可能な内容か。

## 重要な注意事項
- **日本語の美しさ・流暢さでは減点しない**。語彙力や文章の巧みさは採点対象外。
- 多少拙い日本語でも、要点が正しく述べられていれば高得点を与える。
- 逆に、流暢でも論点がズレている場合は低得点とする。

## Few-shot 採点例

### 高得点例（日本語は拙いが要点は正確）
設問: "AIと機械学習の違いを顧客に説明してください"
回答: "AIは広い、コンピュータが賢くなること全部。機械学習はその中の一つで、データから学ぶ方法。ディープラーニングはさらにその中。顧客には「AIは目標、MLは道具」と言えばわかりやすい"
→ score: 78, 理由: 階層関係を正確に理解し顧客向け説明も提示。日本語は簡素だが要点は十分。

### 低得点例（流暢だが論点ズレ）
設問: "AIと機械学習の違いを顧客に説明してください"
回答: "近年のAI技術の発展は目覚ましく、多くの企業がDX推進の一環としてAI導入を検討しています。AIは第3次ブームを迎え、特にディープラーニングの進歩が著しいです。企業経営においてAI活用は不可欠な要素となっています。"
→ score: 25, 理由: 文章は流暢だがAIとMLの「違い」という問いに答えていない。一般論に終始。

出力は必ず以下のJSON形式で返してください。それ以外のテキストは含めないでください:
{
  "passed": true または false,
  "score": 0〜100の整数,
  "feedback": "回答の良かった点と改善点を具体的に指摘するフィードバック文",
  "explanation": "正解の考え方や背景知識、実務での応用例を含む解説文",
  "score_breakdown": {
    "intent_understanding": 0〜25の整数,
    "coverage": 0〜25の整数,
    "structure": 0〜25の整数,
    "practical_relevance": 0〜25の整数
  }
}"""


def _get_dynamodb_resource():
    """Return a DynamoDB resource (extracted for testability)."""
    return boto3.resource("dynamodb", region_name="ap-northeast-1")


def _get_started_at(session_id: str, step: int) -> int | None:
    """DynamoDBからstarted_at_msを取得する。"""
    try:
        dynamodb = _get_dynamodb_resource()
        table = dynamodb.Table(PROGRESS_TABLE)
        resp = table.get_item(
            Key={
                "PK": f"SESSION#{session_id}",
                "SK": f"TIMER#lv1#step{step}",
            }
        )
        item = resp.get("Item")
        if item:
            return int(item.get("started_at_ms", 0))
    except Exception as e:
        logger.warning("Failed to get started_at for timer: %s", str(e))
    return None


def _save_timing(session_id: str, step: int, submitted_at_ms: int, response_time_ms: int):
    """回答時間をDynamoDBに保存する。"""
    try:
        dynamodb = _get_dynamodb_resource()
        table = dynamodb.Table(PROGRESS_TABLE)
        table.update_item(
            Key={
                "PK": f"SESSION#{session_id}",
                "SK": f"TIMER#lv1#step{step}",
            },
            UpdateExpression="SET submitted_at_ms = :s, response_time_ms = :r",
            ExpressionAttributeValues={
                ":s": submitted_at_ms,
                ":r": response_time_ms,
            },
        )
    except Exception as e:
        logger.warning("Failed to save timing data: %s", str(e))


def _save_risk_flags(session_id: str, step: int, risk_flags: dict):
    """AI代理判定結果をDynamoDBに保存する（サーバー側永続化）。
    show_to_userの設定に関わらず、実際の判定結果を常にサーバー側に保存する。"""
    from decimal import Decimal
    try:
        dynamodb = _get_dynamodb_resource()
        table = dynamodb.Table(PROGRESS_TABLE)
        # float→Decimal変換（DynamoDB互換）
        safe_flags = json.loads(json.dumps(risk_flags), parse_float=Decimal)
        table.update_item(
            Key={
                "PK": f"SESSION#{session_id}",
                "SK": f"TIMER#lv1#step{step}",
            },
            UpdateExpression="SET risk_flags = :rf",
            ExpressionAttributeValues={
                ":rf": safe_flags,
            },
        )
    except Exception as e:
        logger.warning("Failed to save risk_flags: %s", str(e))


def _parse_grade_result(result: dict) -> dict:
    """Bedrockレスポンスから採点結果・フィードバック・解説を抽出しバリデーションする。"""
    text = result.get("content", [{}])[0].get("text", "")
    text = strip_code_fence(text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.error("Failed to parse Grader response as JSON: %s", text[:200])
        raise ValueError("Grader response is not valid JSON")

    passed = data.get("passed")
    score = data.get("score")
    feedback = data.get("feedback", "")
    explanation = data.get("explanation", "")
    score_breakdown = data.get("score_breakdown", {})

    if not isinstance(passed, bool):
        raise ValueError("passed must be a boolean")
    if not isinstance(score, int) or score < 0 or score > 100:
        raise ValueError("score must be an integer between 0 and 100")

    # score_breakdownのバリデーション
    validated_breakdown = {}
    for key in ("intent_understanding", "coverage", "structure", "practical_relevance"):
        val = score_breakdown.get(key) if isinstance(score_breakdown, dict) else None
        if isinstance(val, (int, float)) and 0 <= val <= 25:
            validated_breakdown[key] = int(val)
        else:
            validated_breakdown[key] = 0

    return {
        "passed": passed,
        "score": score,
        "feedback": feedback if isinstance(feedback, str) else "",
        "explanation": explanation if isinstance(explanation, str) else "",
        "score_breakdown": validated_breakdown,
    }


def handler(event, context):
    """Lambda handler for POST /lv1/grade."""
    try:
        body = json.loads(event.get("body", "{}"))
    except json.JSONDecodeError:
        return {
            "statusCode": 400,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": "Invalid JSON in request body"}),
        }

    session_id = body.get("session_id")
    step = body.get("step")
    question = body.get("question")
    answer = body.get("answer")

    if not session_id or not isinstance(session_id, str):
        return {
            "statusCode": 400,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": "session_id is required"}),
        }
    if not isinstance(step, int) or step < 1:
        return {
            "statusCode": 400,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": "step must be a positive integer"}),
        }
    if not isinstance(question, dict):
        return {
            "statusCode": 400,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": "question is required"}),
        }
    if not isinstance(answer, str) or not answer.strip():
        return {
            "statusCode": 400,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": "answer is required"}),
        }

    # タイマー: submitted_at を記録し response_time_ms を計算
    submitted_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    started_at_ms = _get_started_at(session_id, step)
    response_time_ms = None
    speed_result = None

    if started_at_ms is not None and started_at_ms > 0:
        response_time_ms = submitted_at_ms - started_at_ms
        if response_time_ms < 0:
            response_time_ms = 0
        speed_result = calculate_speed_score(response_time_ms)
        _save_timing(session_id, step, submitted_at_ms, response_time_ms)

    user_prompt = (
        f"設問: {json.dumps(question, ensure_ascii=False)}\n"
        f"回答: {answer}\n\n"
        "この回答を採点してください。"
    )

    try:
        # 採点 + フィードバック + 解説を1回のBedrock呼び出しで生成
        grade_raw = invoke_claude(SYSTEM_PROMPT, user_prompt)
        grade_result = _parse_grade_result(grade_raw)
        grade_result["passed"] = resolve_passed(level=1, score=grade_result["score"])
    except (ValueError, Exception) as e:
        logger.error("Failed to grade/review: %s", str(e))
        return {
            "statusCode": 500,
            "headers": {"Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": "採点に失敗しました。リトライしてください。"}),
        }

    # AI代理回答判定（失敗しても採点結果は返す - fail-open）
    question_text = question.get("prompt", "")
    risk_flags = detect_ai_proxy(
        question_text=question_text,
        answer_text=answer,
        response_time_ms=response_time_ms,
    )

    # レスポンス組み立て
    response_body = {
        "session_id": session_id,
        "step": step,
        "passed": grade_result["passed"],
        "score": grade_result["score"],
        "feedback": grade_result["feedback"],
        "explanation": grade_result["explanation"],
        "score_breakdown": grade_result["score_breakdown"],
        "server_time_ms": int(datetime.now(timezone.utc).timestamp() * 1000),
    }

    # タイミング情報を追加
    if response_time_ms is not None:
        response_body["response_time_ms"] = response_time_ms
    if speed_result is not None:
        response_body["speed_score"] = speed_result["speed_score"]
        response_body["speed_label"] = speed_result["speed_label"]

    # AI代理判定結果をサーバー側に永続化（show_to_userに関わらず常に保存）
    _save_risk_flags(session_id, step, risk_flags)

    # レスポンスには常に実際のrisk_flagsを含める（complete_handlerでの永続化に必要）
    # フロントエンドでの表示制御はshow_risk_to_userフラグで行う
    response_body["risk_flags"] = {
        "ai_proxy_suspected": risk_flags["ai_proxy_suspected"],
        "confidence": risk_flags["confidence"],
        "rationale": risk_flags["rationale"],
    }
    response_body["show_risk_to_user"] = show_to_user()
    if risk_flags.get("detection_error"):
        logger.warning("AI proxy detection error logged: %s", risk_flags["detection_error"])

    return {
        "statusCode": 200,
        "headers": {"Access-Control-Allow-Origin": "*"},
        "body": json.dumps(response_body, ensure_ascii=False),
    }
