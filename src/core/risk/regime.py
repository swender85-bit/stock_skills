"""市況レジーム判定 -- 「±22%」という統計レンジを判断に使える形にする.

## なぜこれを足すか（最低限を超える部分の提案）

現状の見通しはこう出る:

    短期（1ヶ月）: ▲22% 〜 +30%

**正直ではあるが、判断には使えない。** 3xレバレッジ62%のPFなら数学的にそうなる、
というだけで、**いま上と下のどちらに傾いているか**を何も言っていない。

一方、レポートには判断に効く材料が既に揃っている:

    Fear & Greed 85.4（Extreme Greed）
    VIX 14.90（低位）
    米30年債 5.211%（レバETFに効くのは長期金利）
    構成銘柄12中6が「戻り」（週次プラス×月次マイナス）
    PF実効レバレッジ 1.86倍・現金 ¥10,115

**これらを1つの状態として束ねれば、レンジの中でどちらに寄っているかは言える。**

## 設計上の一線

- **予言はしない。** 出すのは「いまはこういう状態である」という**状態の記述**と、
  その状態で**歴史的に起きやすいこと**の注意喚起に限る。
- **確率を数字で出さない。** 出せる根拠がない。出すと予言に化ける。
- 材料が欠けたら、その軸は**「判定不能」**にする。中立にしない。
  中立にすると「問題なし」と読まれる。
"""

from __future__ import annotations

from typing import Any, Optional

#: Fear & Greed の帯
FG_EXTREME_GREED = 75.0
FG_GREED = 55.0
FG_FEAR = 45.0
FG_EXTREME_FEAR = 25.0

#: VIX の帯
VIX_COMPLACENT = 15.0
VIX_ELEVATED = 25.0
VIX_PANIC = 35.0

#: レバETFに効く長期金利（config/thresholds.yaml の rates と揃える）
UST30Y_WATCH = 5.00


def _band(value: Optional[float], bands: list[tuple[float, str, str]],
          label: str) -> dict:
    """値を帯に落とす。取れなければ**「判定不能」**（中立にしない）。"""
    if value is None:
        return {"axis": label, "available": False, "state": "判定不能",
                "note": f"{label}を取得できませんでした。**中立ではありません。**"}
    for threshold, state, note in bands:
        if value >= threshold:
            return {"axis": label, "available": True, "value": value,
                    "state": state, "note": note}
    last = bands[-1]
    return {"axis": label, "available": True, "value": value,
            "state": last[1], "note": last[2]}


def assess_regime(
    fear_greed: Optional[float] = None,
    vix: Optional[float] = None,
    ust30y: Optional[float] = None,
    effective_leverage: Optional[float] = None,
    cash_ratio: Optional[float] = None,
    constituent_signals: Optional[dict] = None,
) -> dict:
    """いまの市況と PF の状態を1つに束ねる。

    Returns
    -------
    dict
        {"axes", "tilt", "cautions", "available_axes", "note"}
        `tilt` は「上下どちらに寄っているか」の**定性判定**。確率ではない。
    """
    axes: list[dict] = []

    axes.append(_band(fear_greed, [
        (FG_EXTREME_GREED, "極度の強欲",
         "統計的に短期の上値余地が小さい領域。反転は予測できないが、"
         "**この水準からの新規リスク追加は分が悪い**。"),
        (FG_GREED, "強欲", "楽観が優勢。悪材料への耐性が下がっている。"),
        (FG_FEAR, "中立", "特段の偏りは見られない。"),
        (FG_EXTREME_FEAR, "恐怖", "悲観が優勢。反発の余地がある一方、下落継続もある。"),
        (0.0, "極度の恐怖", "歴史的には反発しやすい領域だが、底は事後にしか分からない。"),
    ], "市場心理(F&G)"))

    axes.append(_band(vix, [
        (VIX_PANIC, "パニック", "保険が非常に高い。売却も買い増しも執行が悪化する。"),
        (VIX_ELEVATED, "警戒", "変動が大きい。レバレッジ商品のドラッグが増える。"),
        (VIX_COMPLACENT, "通常", "特段の偏りなし。"),
        (0.0, "低位（楽観）",
         "**保険が安い＝誰も警戒していない**。安心ではなく、"
         "**警戒の不在**として読む。低VIXは急騰局面の常態でもある。"),
    ], "変動率(VIX)"))

    axes.append(_band(ust30y, [
        (UST30Y_WATCH, "高位",
         "**レバレッジETFは実質ロング・デュレーション資産**で、効くのは短期金利ではなく"
         "長期金利。ここが高止まりするだけで逆風になる。"),
        (0.0, "通常", "長期金利は当面の逆風になっていない。"),
    ], "長期金利(30年債)"))

    cautions: list[str] = []

    # PF 側の状態
    if effective_leverage is not None:
        if effective_leverage >= 1.5:
            cautions.append(
                f"**PF実効レバレッジ {effective_leverage:.2f}倍**。"
                "原資産の変動がそのまま増幅される。下落局面での目減りが速い。")
    if cash_ratio is not None and cash_ratio < 0.01:
        cautions.append(
            f"**現金比率 {cash_ratio:.2%}**。"
            "「下落で株数を積み増す」方針の原資が実質ゼロ。"
            "**下げたときに買えない状態は、方針を持っていないのと同じ。**")

    # 構成銘柄の形
    sig = constituent_signals or {}
    rebound = len(sig.get("戻り") or [])
    advance = len(sig.get("継続上昇") or [])
    if rebound or advance:
        if rebound > advance:
            cautions.append(
                f"構成銘柄は**「戻り」{rebound}銘柄 > 「継続上昇」{advance}銘柄**。"
                "指数が上げていても、中身は月次マイナスからの回復途上。"
                "**上放れではない。**")
        elif advance > rebound:
            cautions.append(
                f"構成銘柄は**「継続上昇」{advance}銘柄 > 「戻り」{rebound}銘柄**。"
                "上昇の裾野が広い。")
        if advance and advance <= 4:
            cautions.append(
                f"月次プラスは **{advance}銘柄のみ**。上昇の担い手が絞られており、"
                "**この数銘柄が息切れすると支えが無い**。")

    available = [a for a in axes if a.get("available")]

    # 傾き（定性）。**確率ではない。**
    risk_on = sum(1 for a in available if a["state"] in ("極度の強欲", "強欲", "低位（楽観）"))
    risk_off = sum(1 for a in available
                   if a["state"] in ("恐怖", "極度の恐怖", "警戒", "パニック", "高位"))
    if not available:
        tilt = "判定不能"
        tilt_note = ("市況指標を1つも取得できませんでした。"
                     "**『中立』ではなく『測れていない』**です。")
    elif risk_on > risk_off:
        tilt = "楽観に傾いている"
        tilt_note = ("市場は楽観側。**楽観そのものはリスクではないが、"
                     "楽観の最中に取るリスクは高くつく。**")
    elif risk_off > risk_on:
        tilt = "警戒に傾いている"
        tilt_note = "市場は警戒側。執行コストと変動が増えている。"
    else:
        tilt = "拮抗"
        tilt_note = "楽観と警戒が拮抗している。"

    return {
        "axes": axes,
        "available_axes": len(available),
        "total_axes": len(axes),
        "tilt": tilt,
        "tilt_note": tilt_note,
        "cautions": cautions,
        "note": (
            f"{len(available)}/{len(axes)} 軸で判定。"
            + ("⚠️ 取得できなかった軸があります（**中立ではありません**）。"
               if len(available) < len(axes) else "")
        ),
    }


def format_regime(regime: Optional[dict]) -> str:
    """レポート用。**状態の記述であって予測ではないことを明示する。**"""
    if not regime:
        return "### 市況レジーム\n\n⚠️ 判定できませんでした。\n"

    lines = ["### 市況レジーム（状態の記述。予測ではない）", "",
             f"**傾き: {regime['tilt']}** — {regime['tilt_note']}", "",
             "| 軸 | 値 | 状態 | 読み方 |", "|:---|---:|:---|:---|"]
    for a in regime["axes"]:
        value = f"{a['value']:.2f}" if a.get("available") else "—"
        lines.append(f"| {a['axis']} | {value} | **{a['state']}** | {a['note']} |")

    if regime.get("cautions"):
        lines += ["", "**この状態で特に効くこと:**", ""]
        for c in regime["cautions"]:
            lines.append(f"- {c}")

    lines += ["", f"{regime['note']}", "",
              "> ⚠️ ここに書いたのは**いまどういう状態か**であって、"
              "**次に何が起きるか**ではありません。確率も出していません。"
              "出せる根拠が無いからです。"]
    return "\n".join(lines) + "\n"
