"""模型監査 — リスク模型を毎週採点される仮説として扱う (土曜設計書 提案10)。

## 名指しする問題

Stock Skills は多くの模型を持つ。HHI、因子エクスポージャー、相関、ストレス感応度、VaR。
これらは全て「ポートフォリオがどう動くはずか」を予測している。
そして毎週、**実際にどう動いたかが観測される**。

> 両者を突き合わせれば、模型が正しいかが分かる。にもかかわらず、
> この照合は一度も行われていない。

「今週、USDJPYが-2.1%、日経が+0.8%、金利が+5bp動いた。因子模型によれば
ポートフォリオは-0.9%になるはずだった。実際は-2.4%だった。差の-1.5%はどこから来たのか？」

**この乖離が継続的に一方向であれば、模型に欠けている因子が存在する。**
これは新しいデータを足すことでは発見できず、模型と実現の突合でしか発見できない。

## 記録は今すぐ始める

26週の蓄積が前提だが、**記録を始めなければ26週後も測れない**。
分析は保留でよいが、毎週の (予測, 実現) ペアは今週から積む。

## 早すぎる結論を出さない

週次データは年52点しかない。最低26週の蓄積を要件とし、
それ以前は「データ蓄積中」とだけ表示する。
統計的に有意でない乖離は報告しない（設計書 提案10-⑧）。

## 欠落因子は仮説として提示する

自動で因子を追加しない。人間の承認を要する変更として議題キューへ送る。
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

#: 蓄積先（gitignore 対象）
DEFAULT_STORE = "data/model_scorecard"

#: この週数が溜まるまで結論を出さない（設計書 提案10-⑨-2）
MIN_WEEKS = 26

#: 片側二項検定でこの p 値未満なら系統的バイアスとみなす
SIGNIFICANCE_P = 0.01


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# ---------------------------------------------------------------------------
# 予測
# ---------------------------------------------------------------------------


def predict_return(pf_betas: dict, factor_moves: dict) -> dict:
    """因子模型による今週のPFリターン予測。

    Args:
        pf_betas: `exposure.portfolio_exposure()` の betas
        factor_moves: 因子の週次変化率（%）

    使える因子が1つも無ければ**予測を出さない**（0%と書かない）。
    """
    if not pf_betas or not factor_moves:
        return {"available": False,
                "reason": "因子エクスポージャーまたは因子の週次変化が不足しています"}

    used: dict[str, float] = {}
    total = 0.0
    for name, beta in pf_betas.items():
        b, m = _num(beta), _num(factor_moves.get(name))
        if b is None or m is None:
            continue
        contribution = b * m
        used[name] = round(contribution, 3)
        total += contribution

    if not used:
        return {"available": False,
                "reason": "共通する因子がなく予測を作れませんでした"}

    return {
        "available": True,
        "predicted_pct": round(total, 3),
        "contributions": used,
        "factors_used": sorted(used),
        "missing_factors": sorted(set(pf_betas) - set(used)),
    }


# ---------------------------------------------------------------------------
# 記録
# ---------------------------------------------------------------------------


def record_week(
    predicted_pct: Optional[float],
    realized_pct: Optional[float],
    *,
    as_of: Optional[str] = None,
    context: Optional[dict] = None,
    base_dir: str = DEFAULT_STORE,
) -> Optional[Path]:
    """今週の (予測, 実現) ペアを追記する。

    **どちらかが欠けていても記録する**（欠測として残す）。
    後から「なぜこの週が抜けているのか」を追えるようにするため。
    """
    row = {
        "as_of": as_of or date.today().isoformat(),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "predicted_pct": _num(predicted_pct),
        "realized_pct": _num(realized_pct),
        "error_pct": (None if predicted_pct is None or realized_pct is None
                      else round(_num(realized_pct) - _num(predicted_pct), 3)),
        "context": context or {},
    }
    try:
        d = Path(base_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "scorecard.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path
    except Exception:
        return None


def load_scorecard(base_dir: str = DEFAULT_STORE) -> list[dict]:
    path = Path(base_dir) / "scorecard.jsonl"
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                out.append(row)
    except Exception:
        return []
    out.sort(key=lambda r: str(r.get("as_of") or ""))
    return out


# ---------------------------------------------------------------------------
# 採点
# ---------------------------------------------------------------------------


def _binomial_tail_p(k: int, n: int, p: float = 0.5) -> float:
    """片側二項検定の p 値（k回以上）。scipy を使わずに計算する。"""
    if n <= 0 or k > n:
        return 1.0
    total = 0.0
    for i in range(k, n + 1):
        total += math.comb(n, i) * (p ** i) * ((1 - p) ** (n - i))
    return min(1.0, total)


def score_model(rows: Optional[list[dict]] = None,
                *, min_weeks: int = MIN_WEEKS,
                base_dir: str = DEFAULT_STORE) -> dict:
    """模型の説明力と系統的バイアスを採点する。

    サンプルが `min_weeks` に満たなければ**結論を出さない**。
    「データ蓄積中」と言うだけにする（週次データは年52点しかない）。
    """
    rows = rows if rows is not None else load_scorecard(base_dir)
    paired = [r for r in rows
              if _num(r.get("predicted_pct")) is not None
              and _num(r.get("realized_pct")) is not None]

    if len(paired) < min_weeks:
        return {
            "available": False,
            "weeks": len(paired),
            "min_weeks": min_weeks,
            "reason": (f"データ蓄積中（{len(paired)}/{min_weeks}週）。"
                       "週次データは年52点しかないため、"
                       "これ未満で結論を出すと偶然を模型の欠陥と誤認します。"),
        }

    errors = [r["realized_pct"] - r["predicted_pct"] for r in paired]
    predicted = [r["predicted_pct"] for r in paired]
    realized = [r["realized_pct"] for r in paired]

    n = len(errors)
    mean_error = sum(errors) / n
    below = sum(1 for e in errors if e < 0)

    # 片側検定。模型が損失を過小評価しているか（実現が予測を下回る側）。
    p_below = _binomial_tail_p(below, n)
    p_above = _binomial_tail_p(n - below, n)
    p_value = min(p_below, p_above)
    biased = p_value < SIGNIFICANCE_P

    r2 = _r_squared(predicted, realized)

    return {
        "available": True,
        "weeks": n,
        "r2": round(r2, 3) if r2 is not None else None,
        "mean_error_pct": round(mean_error, 3),
        "weeks_below_prediction": below,
        "p_value": round(p_value, 5),
        "systematic_bias": biased,
        "bias_direction": ("模型は損失を過小評価する傾向" if mean_error < 0 else
                           "模型は損失を過大評価する傾向") if biased else None,
        "message": _score_message(n, below, mean_error, p_value, biased, r2),
        "caveat": ("模型が完璧になることはありません。重要なのは、"
                   "模型がどちらの方向に、どれだけ間違えるかを知っていることです。"),
    }


def _r_squared(predicted: list[float], realized: list[float]) -> Optional[float]:
    n = len(realized)
    if n < 2:
        return None
    mean = sum(realized) / n
    ss_tot = sum((y - mean) ** 2 for y in realized)
    ss_res = sum((y - p) ** 2 for y, p in zip(realized, predicted))
    return None if not ss_tot else 1.0 - ss_res / ss_tot


def _score_message(n, below, mean_error, p_value, biased, r2) -> str:
    parts = [f"直近{n}週の因子模型の説明力 R² = "
             + (f"{r2:.2f}" if r2 is not None else "算出不能")]
    parts.append(f"系統的バイアス 平均 {mean_error:+.2f}%/週")
    if biased:
        parts.append(
            f"⚠️ {n}週中{below}週で、実現が模型予測を下回っています"
            f"（片側検定 p={p_value:.4f} < {SIGNIFICANCE_P}）。"
            "これは偶然としては起こりにくい偏りです。")
    else:
        parts.append(
            f"乖離は統計的に有意ではありません（p={p_value:.3f}）。報告する偏りはありません。")
    return " / ".join(parts)


# ---------------------------------------------------------------------------
# 欠落因子の仮説
# ---------------------------------------------------------------------------


def suggest_missing_factor(rows: Optional[list[dict]] = None,
                           *, base_dir: str = DEFAULT_STORE,
                           min_weeks: int = MIN_WEEKS) -> dict:
    """乖離がどの状況に集中しているかから、欠けている因子を**仮説として**提示する。

    **自動で因子を追加しない**（設計書 提案10-⑧）。人間の承認を要する議題にする。
    """
    rows = rows if rows is not None else load_scorecard(base_dir)
    paired = [r for r in rows if r.get("error_pct") is not None]
    if len(paired) < min_weeks:
        return {"available": False,
                "reason": f"データ蓄積中（{len(paired)}/{min_weeks}週）"}

    # context に記録された条件フラグごとに、乖離の平均を比べる
    flags: dict[str, list[float]] = {}
    for r in paired:
        for key, value in (r.get("context") or {}).items():
            if isinstance(value, bool) and value:
                flags.setdefault(key, []).append(float(r["error_pct"]))

    hypotheses: list[dict] = []
    overall = sum(float(r["error_pct"]) for r in paired) / len(paired)
    for key, errs in flags.items():
        if len(errs) < 4:
            continue
        mean = sum(errs) / len(errs)
        if abs(mean - overall) >= 0.5:
            hypotheses.append({
                "condition": key,
                "weeks": len(errs),
                "mean_error_pct": round(mean, 3),
                "overall_mean_pct": round(overall, 3),
                "hypothesis": (f"乖離が『{key}』の週に集中しています"
                               f"（{mean:+.2f}% vs 全体 {overall:+.2f}%）。"
                               "現在の因子集合にこの軸が含まれていない可能性があります。"),
            })

    hypotheses.sort(key=lambda h: -abs(h["mean_error_pct"] - h["overall_mean_pct"]))
    return {
        "available": bool(hypotheses),
        "hypotheses": hypotheses,
        "note": ("これは仮説であり、因子は自動追加されません。"
                 "採用するかどうかは人が判断してください。"),
        "reason": (None if hypotheses else
                   "乖離が特定の条件に集中している証拠は見つかりませんでした"),
    }


# ---------------------------------------------------------------------------
# 下流への信頼度伝播
# ---------------------------------------------------------------------------


def propagate_confidence(score: dict, value: Optional[float],
                         label: str = "予測値") -> dict:
    """模型ベースの数値に、採点結果から導いた信頼区間を添える。

    「テック暴落シナリオの予測損失 -18.2%」を、バイアス補正後の幅で読ませる。
    模型を疑うための表示であり、数字を打ち消すためではない。
    """
    v = _num(value)
    if v is None:
        return {"available": False, "reason": f"{label}が数値ではありません"}
    if not score.get("available"):
        return {"available": False, "value": v,
                "reason": ("模型の採点がまだできていないため、"
                           "この数値に信頼度を付けられません"),
                "note": "採点前の数値をそのまま信用しないでください。"}

    if not score.get("systematic_bias"):
        return {"available": True, "value": v, "adjusted_range": None,
                "note": f"{label} {v:+.1f}% — 模型に有意な系統的バイアスはありません。"}

    bias = score.get("mean_error_pct") or 0.0
    # 週次のバイアスがそのままシナリオ損失に乗るわけではないので、
    # 補正は「方向」と「概算幅」に留める。
    low = v + bias * 4
    high = v + bias * 2
    lo, hi = sorted((low, high))
    return {
        "available": True,
        "value": v,
        "adjusted_range": (round(lo, 1), round(hi, 1)),
        "note": (f"{label} {v:+.1f}% は、模型の系統的バイアス"
                 f"（{bias:+.2f}%/週・{score.get('bias_direction')}）を踏まえると "
                 f"{lo:+.1f}%〜{hi:+.1f}% の範囲とみるべきです。"),
    }


# ---------------------------------------------------------------------------
# まとめ
# ---------------------------------------------------------------------------


def build_model_audit(
    pf_betas: Optional[dict] = None,
    factor_moves: Optional[dict] = None,
    realized_pct: Optional[float] = None,
    *,
    context: Optional[dict] = None,
    store: bool = True,
    base_dir: str = DEFAULT_STORE,
) -> dict:
    """今週の予測を作り、実現と併せて記録し、採点結果を返す。

    **記録は分析より先に行う。** 分析が失敗しても来週の材料は残る。
    """
    out: dict = {"errors": []}

    prediction = predict_return(pf_betas or {}, factor_moves or {})
    out["prediction"] = prediction
    predicted = prediction.get("predicted_pct") if prediction.get("available") else None
    out["realized_pct"] = _num(realized_pct)

    if store:
        try:
            path = record_week(predicted, realized_pct, context=context,
                               base_dir=base_dir)
            out["recorded"] = str(path) if path else None
        except Exception as e:
            out["errors"].append(f"記録に失敗: {type(e).__name__}")

    try:
        out["score"] = score_model(base_dir=base_dir)
    except Exception as e:
        out["score"] = {"available": False, "reason": f"{type(e).__name__}"}
        out["errors"].append(f"採点に失敗: {type(e).__name__}: {e}")

    try:
        out["missing_factor"] = suggest_missing_factor(base_dir=base_dir)
    except Exception:
        out["missing_factor"] = {"available": False, "reason": "算出できません"}

    return out
