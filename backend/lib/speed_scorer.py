"""スピード評価モジュール - 回答時間に基づく加点/減点を計算する。

しきい値は環境変数で設定可能:
  SPEED_T_FAST_MS: 高速回答とみなす上限(ms) デフォルト 60000 (1分)
  SPEED_T_MID_MS:  標準回答とみなす上限(ms) デフォルト 180000 (3分)
  SPEED_BONUS_FAST: 高速回答時の加点        デフォルト 5
  SPEED_BONUS_MID:  標準回答時の加点        デフォルト 2
  SPEED_PENALTY_SLOW: 低速回答時の減点      デフォルト 0
"""

import os
import logging

logger = logging.getLogger(__name__)

# --- デフォルト値 ---
_DEFAULT_T_FAST_MS = 60_000       # 1分
_DEFAULT_T_MID_MS = 180_000       # 3分
_DEFAULT_BONUS_FAST = 5
_DEFAULT_BONUS_MID = 2
_DEFAULT_PENALTY_SLOW = 0


def _env_int(key: str, default: int) -> int:
    """環境変数から整数を取得。変換不可ならデフォルト値を返す。"""
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        logger.warning("Invalid value for %s: %r, using default %d", key, raw, default)
        return default


def get_speed_thresholds() -> dict:
    """現在のスピード評価しきい値を取得する。"""
    return {
        "t_fast_ms": _env_int("SPEED_T_FAST_MS", _DEFAULT_T_FAST_MS),
        "t_mid_ms": _env_int("SPEED_T_MID_MS", _DEFAULT_T_MID_MS),
        "bonus_fast": _env_int("SPEED_BONUS_FAST", _DEFAULT_BONUS_FAST),
        "bonus_mid": _env_int("SPEED_BONUS_MID", _DEFAULT_BONUS_MID),
        "penalty_slow": _env_int("SPEED_PENALTY_SLOW", _DEFAULT_PENALTY_SLOW),
    }


def calculate_speed_score(response_time_ms: int) -> dict:
    """回答時間(ms)に基づくスピードスコアを計算する。

    Args:
        response_time_ms: 回答にかかった時間(ミリ秒)

    Returns:
        {
            "speed_score": int,       # 加点/減点値
            "speed_label": str,       # "fast" / "mid" / "slow"
            "response_time_ms": int,  # 入力値のエコー
            "thresholds": dict,       # 適用されたしきい値
        }
    """
    if not isinstance(response_time_ms, (int, float)) or response_time_ms < 0:
        logger.warning("Invalid response_time_ms: %r, treating as slow", response_time_ms)
        response_time_ms = 999_999_999

    thresholds = get_speed_thresholds()
    t_fast = thresholds["t_fast_ms"]
    t_mid = thresholds["t_mid_ms"]

    if response_time_ms <= t_fast:
        speed_score = thresholds["bonus_fast"]
        label = "fast"
    elif response_time_ms <= t_mid:
        speed_score = thresholds["bonus_mid"]
        label = "mid"
    else:
        speed_score = thresholds["penalty_slow"]
        label = "slow"

    return {
        "speed_score": speed_score,
        "speed_label": label,
        "response_time_ms": int(response_time_ms),
        "thresholds": thresholds,
    }
