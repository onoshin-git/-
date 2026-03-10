"""AI代理回答判定モジュール - LLMを用いて回答が生成AIによる代理回答かを判定する。

判定は採点から分離し、risk_flags として保存される。
判定失敗時は試験完了をブロックしない（fail-open）。

設定（環境変数）:
  AI_PROXY_DETECTION_ENABLED: "true"/"false" (デフォルト: "true")
  AI_PROXY_CONFIDENCE_THRESHOLD: 0-1 の閾値 (デフォルト: "0.7")
  AI_PROXY_SHOW_TO_USER: "true"/"false" (デフォルト: "false" = 管理者のみ)
"""

import json
import logging
import os

from backend.lib.bedrock_client import invoke_claude, strip_code_fence

logger = logging.getLogger(__name__)

# --- 設定 ---

def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.lower() in ("true", "1", "yes")


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        logger.warning("Invalid value for %s: %r, using default %s", key, raw, default)
        return default


def is_detection_enabled() -> bool:
    return _env_bool("AI_PROXY_DETECTION_ENABLED", True)


def get_confidence_threshold() -> float:
    return _env_float("AI_PROXY_CONFIDENCE_THRESHOLD", 0.7)


def show_to_user() -> bool:
    return _env_bool("AI_PROXY_SHOW_TO_USER", False)


# --- 判定プロンプト ---

DETECTION_SYSTEM_PROMPT = """あなたはAI試験の公平性を担保する「代理回答判定エージェント」です。

受験者の回答テキストが、ChatGPTやClaude等の生成AIによって書かれた可能性を判定してください。

## 判定基準
以下の特徴が複数当てはまる場合、AI生成の可能性が高い：
1. 過度に整った文章構成（箇条書き、番号付きリスト等が過剰に使用）
2. 一般論に終始し、個人の経験・具体例が欠如
3. 質問の範囲を超えた網羅的すぎる回答
4. 特有の言い回し（「〜について説明します」「まとめると」等のAI的表現パターン）
5. 回答時間に対して不自然に長い・整った回答
6. 専門用語の均一な使用（人間は通常ばらつきがある）

## 注意
- 日本語の巧拙は判定に使わない（日本語が拙くてもAI使用とは限らない）
- 回答時間が短すぎる場合はコピペの可能性を考慮
- 確信度が低い場合は「suspected: false」とする（推定無罪）

## Few-shot Examples

### Example 1: AI生成の可能性が高い回答
設問: "AIと機械学習の違いを説明してください"
回答: "AIと機械学習の違いについて説明します。\\n\\n1. **人工知能（AI）**は、人間の知能を模倣するコンピュータシステムの総称です。\\n2. **機械学習（ML）**は、AIの一分野であり、データから自動的にパターンを学習するアルゴリズムの手法です。\\n3. **関係性**: MLはAIのサブセットであり、AIを実現する主要な手法の一つです。\\n\\nまとめると、AIは目標（知的な振る舞い）であり、MLはその目標を達成するための手段の一つといえます。"
判定: {"ai_proxy_suspected": true, "confidence": 0.85, "rationale": "過度に構造化された回答、AI特有の導入・まとめパターン、個人的経験の欠如"}

### Example 2: 人間らしい回答
設問: "AIと機械学習の違いを説明してください"
回答: "AIは人間みたいに考えるコンピュータのことで、機械学習はその中の一つの方法です。データを沢山見せて学ばせるやつ。AIの方が広い概念で、機械学習はAIを作るための技術という感じです。"
判定: {"ai_proxy_suspected": false, "confidence": 0.2, "rationale": "口語的表現、簡潔で個人的な理解に基づく説明、構造化されていない自然な文体"}

### Example 3: 判断が難しい回答
設問: "生成AIのビジネス活用例を挙げてください"
回答: "うちの会社ではカスタマーサポートにチャットボットを入れてます。あとは、マーケティングのメール文面の下書きにも使ってます。導入前は1日50件対応が限界でしたが、チャットボットで100件まで捌けるようになりました。"
判定: {"ai_proxy_suspected": false, "confidence": 0.15, "rationale": "具体的な数値と個人の業務経験に基づく回答、自然な会話調"}

出力は必ず以下のJSON形式で返してください。それ以外のテキストは含めないでください:
{
  "ai_proxy_suspected": true または false,
  "confidence": 0.0〜1.0の小数,
  "rationale": "判定理由の短い説明文"
}"""


def detect_ai_proxy(
    question_text: str,
    answer_text: str,
    response_time_ms: int | None = None,
    rubric: str | None = None,
) -> dict:
    """回答がAI代理回答かどうかを判定する。

    Args:
        question_text: 設問文
        answer_text: 受験者の回答テキスト
        response_time_ms: 回答時間(ms)。Noneの場合は時間情報なしで判定。
        rubric: 採点基準/ルーブリック。Noneの場合は省略。

    Returns:
        {
            "ai_proxy_suspected": bool,
            "confidence": float (0-1),
            "rationale": str,
            "detection_error": None or str,
        }

    Note:
        判定APIが失敗した場合もエラーをraiseせず、デフォルト結果を返す（fail-open）。
    """
    if not is_detection_enabled():
        return {
            "ai_proxy_suspected": False,
            "confidence": 0.0,
            "rationale": "Detection disabled",
            "detection_error": None,
        }

    # 選択問題など短い回答はスキップ
    if not answer_text or len(answer_text.strip()) < 20:
        return {
            "ai_proxy_suspected": False,
            "confidence": 0.0,
            "rationale": "Short answer, skipped detection",
            "detection_error": None,
        }

    # ユーザープロンプト組み立て
    parts = [f"設問: {question_text}", f"回答: {answer_text}"]
    if response_time_ms is not None:
        parts.append(f"回答時間: {response_time_ms}ms ({response_time_ms / 1000:.1f}秒)")
    if rubric:
        parts.append(f"採点観点: {rubric}")
    parts.append("\nこの回答がAIによる代理回答かどうか判定してください。")

    user_prompt = "\n".join(parts)

    try:
        result = invoke_claude(DETECTION_SYSTEM_PROMPT, user_prompt, max_tokens=512, temperature=0.1)
        text = result.get("content", [{}])[0].get("text", "")
        text = strip_code_fence(text)
        data = json.loads(text)

        suspected = data.get("ai_proxy_suspected", False)
        confidence = data.get("confidence", 0.0)
        rationale = data.get("rationale", "")

        if not isinstance(suspected, bool):
            suspected = False
        if not isinstance(confidence, (int, float)):
            confidence = 0.0
        confidence = max(0.0, min(1.0, float(confidence)))

        # しきい値未満なら suspected=false に補正
        threshold = get_confidence_threshold()
        if confidence < threshold:
            suspected = False

        return {
            "ai_proxy_suspected": suspected,
            "confidence": confidence,
            "rationale": str(rationale)[:500],
            "detection_error": None,
        }

    except Exception as e:
        # Fail-open: 判定失敗時はログに残して疑義なしとして返す
        logger.error("AI proxy detection failed (fail-open): %s", str(e))
        return {
            "ai_proxy_suspected": False,
            "confidence": 0.0,
            "rationale": "Detection failed",
            "detection_error": str(e)[:200],
        }
