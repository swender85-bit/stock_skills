"""観測の試行を第一級のデータとして扱う。

## なぜこれがあるか

このシステムは長らく、取得の結果を「値 または ``None``」で表してきた。
そのため次の3つが同じ ``None`` に潰れていた。

============== ==========================================
状態           意味
============== ==========================================
UNAVAILABLE    取りに行ったが失敗した（回線・API・権限）
ABSENT         取れたが、その値が存在しない（ETFのPER等）
NOT_ATTEMPTED  そもそも観測対象ではなかった
============== ==========================================

**失敗が情報ではなく「穴」になり、穴は下流で「無かったこと」として扱われ、
穴のまま集計に入った。** 2026-08-08 は10銘柄中9銘柄の価格が取れず、
その9件が黙って分母から消え、残り1件の合計 ¥1,386,358 が
注記なしで「総資産」として出荷された（真値は約 ¥22,000,000。**94%の過少表示**）。

数字が欠けたのではない。**欠けた数字が、正しい全体値の顔をして出た。**

## 原則

1. 中核データ（価格・保有・数量）の取得失敗を、静かに ``None`` にしない
2. 「取得できなかった」と「取得できて値が無かった」を型として区別する
3. 不完全な入力から計算した集計値を、全体値として表示しない
   （抑制するか、母数を必ず併記する）
4. 縮退（graceful degradation）は補助データにのみ許す。中核データには許さない
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

#: 観測できた
OBSERVED = "observed"
#: 取りに行ったが失敗した（＝後で取り直せば取れるかもしれない）
UNAVAILABLE = "unavailable"
#: 取れたが値が存在しない（＝取り直しても永遠に出ない）
ABSENT = "absent"
#: 観測対象ではなかった
NOT_ATTEMPTED = "not_attempted"

#: 中核データ。ここでの UNAVAILABLE は縮退させてはならない。
CORE_FIELDS = ("price", "shares", "value_jpy")


@dataclass(frozen=True)
class Coverage:
    """ある集合を何件観測できたか。**集計値には必ずこれを添える。**"""

    total: int
    observed: int
    missing: list = field(default_factory=list)
    label: str = ""

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.observed >= self.total

    @property
    def ratio(self) -> float:
        return (self.observed / self.total) if self.total else 0.0

    @property
    def fraction(self) -> str:
        return f"{self.observed}/{self.total}"

    def note(self, unit: str = "件") -> str:
        """母数の併記文。完全なら空文字を返す（無害な追記にしない）。"""
        if self.complete or not self.total:
            return ""
        missing_n = self.total - self.observed
        return (f"（{self.total}{unit}中 {self.observed}{unit}のみ評価・"
                f"残り {missing_n}{unit}は取得不可）")

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "observed": self.observed,
            "missing": list(self.missing),
            "complete": self.complete,
            "ratio": round(self.ratio, 3),
            "label": self.label,
            "note": self.note(),
        }


def classify(value: Any, *, attempted: bool = True,
             error: Optional[str] = None,
             expected: bool = True) -> str:
    """1つの観測結果を4状態に分類する。

    Parameters
    ----------
    value:
        取れた値。``None`` なら失敗か不在。
    attempted:
        観測を試みたか。
    error:
        取得時のエラー。あれば UNAVAILABLE 側に倒す。
    expected:
        そもそもその値が存在しうるか（ETF に PER は存在しない）。
    """
    if not attempted:
        return NOT_ATTEMPTED
    if value is not None:
        return OBSERVED
    if not expected:
        return ABSENT
    return UNAVAILABLE if error is not None else UNAVAILABLE


def coverage_of(rows: Sequence[dict],
                predicate: Callable[[dict], bool],
                id_key: str = "symbol",
                label: str = "") -> Coverage:
    """行の集合について、観測できた件数と欠けた識別子を数える。"""
    rows = list(rows or [])
    missing = [str(r.get(id_key) or r.get("name") or "?")
               for r in rows if not predicate(r)]
    return Coverage(total=len(rows), observed=len(rows) - len(missing),
                    missing=missing, label=label)


def partial_total(rows: Sequence[dict], key: str,
                  id_key: str = "symbol",
                  label: str = "") -> tuple[float, Coverage]:
    """合計と、その合計が何件から出たかを**必ず一緒に**返す。

    合計だけを返す関数を作らないこと。それが 2026-08-08 の事故の形である。
    """
    rows = list(rows or [])
    cov = coverage_of(rows, lambda r: r.get(key) is not None, id_key, label)
    total = sum(float(r[key]) for r in rows if r.get(key) is not None)
    return total, cov


def safe_ratio(rows: Sequence[dict], numerator_key: str, denominator_key: str,
               id_key: str = "symbol") -> dict:
    """比率を、**分子と分母が同じ集合から来ているときだけ**返す。

    2026-08-08 の「評価損益 +2.1%」は、分子が価格の取れた1銘柄、
    分母が取得単価から出せる全10銘柄という**混合分母**だった。
    比率としての意味が無い数字が、意味がある顔をして出た。

    集合が食い違うときは値を返さず、理由と母数を返す（抑制）。
    """
    rows = list(rows or [])
    usable = [r for r in rows
              if r.get(numerator_key) is not None
              and r.get(denominator_key) is not None]
    den_rows = [r for r in rows if r.get(denominator_key) is not None]
    cov = coverage_of(den_rows,
                      lambda r: r.get(numerator_key) is not None, id_key)

    numerator = sum(float(r[numerator_key]) for r in usable)
    denominator = sum(float(r[denominator_key]) for r in usable)

    if not cov.complete:
        return {
            "value": None,
            "suppressed": True,
            "reason": (f"分子と分母の母集団が一致しません（分母 {cov.total}件 / "
                       f"分子 {cov.observed}件）。混合分母の比率は表示しません。"),
            "coverage": cov.to_dict(),
            "partial_value": (numerator / denominator * 100.0) if denominator else None,
        }
    return {
        "value": (numerator / denominator * 100.0) if denominator else None,
        "suppressed": False,
        "reason": None,
        "coverage": cov.to_dict(),
        "partial_value": None,
    }


def annotate(value: Any, coverage: Coverage, unit: str = "件") -> str:
    """表示用。母数が欠けていれば必ず併記される文字列を作る。"""
    note = coverage.note(unit)
    return f"{value}{note}" if note else f"{value}"


def core_failures(rows: Sequence[dict],
                  status_key: str = "price_status",
                  id_key: str = "symbol") -> list:
    """中核データの取得に失敗した行を、理由つきで列挙する。

    「取得できて値が無かった（ABSENT）」は含めない。**直せる失敗だけを返す。**
    """
    out = []
    for r in rows or []:
        if r.get(status_key) == UNAVAILABLE:
            out.append({
                "id": str(r.get(id_key) or r.get("name") or "?"),
                "reason": r.get("price_unavailable_reason") or r.get("error")
                or "理由不明（取得側がエラーを残していない）",
            })
    return out
