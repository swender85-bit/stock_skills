"""前提空間ダイバーシフィケーション -- 資産ではなく仮定で分散を測る (Fable5 第1弾 案2).

## なぜ必要か

分散はセクター・地域・通貨・規模の HHI で測られてきたが、これは**資産空間**の
集中度にすぎない。thesis に書かれた「なぜ持つか」が複数銘柄で同一前提
（円安継続・AI設備投資・金利低下）に依存していても、既存のどの指標にも映らない。

価格相関は前提相関の**遅行指標**である。セクター分散済みのPFが、
単一の仮定の反転で全滅するリスクが不可視のまま残っている。

## 何をするか

1. thesis ノートから前提を抽出して Assumption 化する
2. 各銘柄がどの前提を共有するかで「前提HHI」を算出（既存HHI機構を再利用）
3. 「あなたの最大共有前提が反転した世界」シナリオを自動生成し、
   固定8シナリオとは**別枠で**ストレステストに足す

リスクモデルの一部が、外生（一般シナリオ）から内生（自分の信念グラフ由来）に変わる。

## 非破壊

既存の8シナリオは変更しない。前提が抽出できなければ空を返すだけで、
ストレステストは従来どおり動く。
"""

from __future__ import annotations

from typing import Iterable, Optional

from src.core.portfolio.concentration import compute_hhi


#: 前提の正準名 → 検出キーワード。
#: thesis は散文なので完全な抽出は無理だが、投資判断を支える主要な仮定は
#: 語彙が限られている。取りこぼしより誤検出の方が害が大きいので、
#: 曖昧な語（「成長」「好調」）は入れない。
ASSUMPTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "円安継続": ("円安", "ドル高円安", "為替メリット", "輸出有利"),
    "円高転換": ("円高", "ドル安円高", "輸入有利"),
    "AI設備投資拡大": ("ai設備投資", "ai投資", "データセンター", "gpu需要", "ai需要", "生成ai"),
    "半導体サイクル上昇": ("半導体サイクル", "シリコンサイクル", "半導体市況", "メモリ市況"),
    "金利低下": ("金利低下", "利下げ", "緩和", "金融緩和"),
    "金利上昇": ("金利上昇", "利上げ", "引き締め", "金融引き締め"),
    "インフレ沈静": ("インフレ鈍化", "インフレ沈静", "物価安定", "ディスインフレ"),
    "インフレ継続": ("インフレ継続", "インフレ再燃", "物価上昇圧力"),
    "米国景気拡大": ("米国景気", "米景気拡大", "ソフトランディング", "米国経済堅調"),
    "中国景気回復": ("中国回復", "中国景気", "中国需要"),
    "個人消費堅調": ("個人消費", "消費堅調", "実質賃金"),
    "コスト転嫁継続": ("価格転嫁", "値上げ浸透", "コスト転嫁"),
    "株主還元拡大": ("自社株買い", "増配", "株主還元", "還元強化"),
    "地政学安定": ("地政学リスク低下", "緊張緩和"),
}

#: 前提が反転したときに何が起きるか。自動生成シナリオの effects になる。
#: 既存 SCENARIOS と同じ形（target / impact / reason）で持つ。
INVERSION_EFFECTS: dict[str, dict] = {
    "円安継続": {
        "inverted": "急速な円高転換",
        "base_shock": -0.15,
        "effects": [
            {"target": "米国株(円建て)", "impact": -0.15, "reason": "為替換算で目減り"},
            {"target": "輸出関連", "impact": -0.12, "reason": "採算悪化"},
            {"target": "内需", "impact": 0.03, "reason": "輸入コスト低下"},
        ],
    },
    "AI設備投資拡大": {
        "inverted": "AI設備投資の失速",
        "base_shock": -0.30,
        "effects": [
            {"target": "半導体", "impact": -0.35, "reason": "受注急減"},
            {"target": "グロース株", "impact": -0.25, "reason": "成長期待の剥落"},
            {"target": "半導体商社", "impact": -0.25, "reason": "在庫調整"},
            {"target": "ディフェンシブ", "impact": 0.02, "reason": "資金退避先"},
        ],
    },
    "半導体サイクル上昇": {
        "inverted": "半導体サイクルの下降転換",
        "base_shock": -0.25,
        "effects": [
            {"target": "半導体", "impact": -0.30, "reason": "市況悪化"},
            {"target": "半導体商社", "impact": -0.22, "reason": "在庫評価損"},
        ],
    },
    "金利低下": {
        "inverted": "金利の再上昇",
        "base_shock": -0.15,
        "effects": [
            {"target": "グロース株", "impact": -0.20, "reason": "割引率上昇"},
            {"target": "銀行", "impact": 0.08, "reason": "利ざや改善"},
            {"target": "不動産", "impact": -0.12, "reason": "調達コスト増"},
        ],
    },
    "米国景気拡大": {
        "inverted": "米国リセッション入り",
        "base_shock": -0.25,
        "effects": [
            {"target": "米国株", "impact": -0.25, "reason": "業績悪化"},
            {"target": "景気敏感", "impact": -0.28, "reason": "需要減"},
            {"target": "ディフェンシブ", "impact": -0.08, "reason": "相対的に耐性"},
        ],
    },
    "個人消費堅調": {
        "inverted": "個人消費の失速",
        "base_shock": -0.15,
        "effects": [
            {"target": "小売", "impact": -0.20, "reason": "客数・単価の減少"},
            {"target": "食品", "impact": -0.08, "reason": "節約志向"},
        ],
    },
    "コスト転嫁継続": {
        "inverted": "価格転嫁の限界到達",
        "base_shock": -0.12,
        "effects": [
            {"target": "食品", "impact": -0.15, "reason": "利益率の圧縮"},
            {"target": "小売", "impact": -0.12, "reason": "粗利悪化"},
        ],
    },
}

#: 前提HHI の警戒閾値
HHI_WARNING = 0.35
HHI_DANGER = 0.50


# ---------------------------------------------------------------------------
# 前提に符号を持たせる (改善4)
# ---------------------------------------------------------------------------
#
# 前提HHI は「複数銘柄が同一前提に依存していても、どの指標にも映らない」を解く。
# だが**同じ変数に逆向きの意味づけが同時に存在するケースを検出できない。**
#
#     両替計画  : 円高（¥155）を待つ → 円高が「好機」
#     日本株保有: 円高は日本株の逆風  → 円高が「危機」
#     → 同じ変数（USDJPY）の同じ方向に、好機と危機が同時に存在する
#     → **円高が来た瞬間、両替の原資（日本株）が目減りする**
#
# これは「通貨の二重ロング」（米国株と日本の輸出企業を同時に持つと円高で同時に不利）
# とは**別種**である。二重ロングは「同方向の集中」、こちらは
# 「**逆方向の依存が相殺せず、片方の実行が他方を壊す**」。
#
# 前提の役割:
#   opportunity … その方向に動くのを**待っている**（行動の前提）
#   risk        … その方向に動くと**痛む**（保有の前提）
#
# thesis（保有の理由）からは risk 側が、target（購入・売却の計画）からは
# opportunity 側が derive される。

#: 正準前提 → (変数, その前提が崩れる方向)。
#: 「円安継続」というテーゼが痛むのは USDJPY が down（円高）に振れたとき。
ASSUMPTION_VARIABLES: dict[str, tuple[str, str]] = {
    "円安継続": ("USDJPY", "down"),
    "円高転換": ("USDJPY", "up"),
    "AI設備投資拡大": ("AI_CAPEX", "down"),
    "半導体サイクル上昇": ("SEMI_CYCLE", "down"),
    "金利低下": ("RATES", "up"),
    "金利上昇": ("RATES", "down"),
    "インフレ沈静": ("INFLATION", "up"),
    "インフレ継続": ("INFLATION", "down"),
    "米国景気拡大": ("US_GROWTH", "down"),
    "中国景気回復": ("CN_GROWTH", "down"),
    "個人消費堅調": ("JP_CONSUMPTION", "down"),
    "コスト転嫁継続": ("PRICING_POWER", "down"),
    "株主還元拡大": ("SHAREHOLDER_RETURN", "down"),
    "地政学安定": ("GEOPOLITICS", "down"),
}

#: 変数 × 方向 の読み下し。衝突メッセージを日本語で書くため。
VARIABLE_LABELS: dict[str, tuple[str, str]] = {
    #                    up            down
    "USDJPY": ("円安", "円高"),
    "AI_CAPEX": ("AI設備投資の拡大", "AI設備投資の失速"),
    "SEMI_CYCLE": ("半導体サイクルの上昇", "半導体サイクルの下降"),
    "RATES": ("金利上昇", "金利低下"),
    "INFLATION": ("インフレ再燃", "インフレ沈静"),
    "US_GROWTH": ("米国景気の拡大", "米国景気の後退"),
    "CN_GROWTH": ("中国景気の回復", "中国景気の失速"),
    "JP_CONSUMPTION": ("個人消費の回復", "個人消費の失速"),
    "PRICING_POWER": ("価格転嫁の進展", "価格転嫁の限界"),
    "SHAREHOLDER_RETURN": ("株主還元の拡大", "株主還元の縮小"),
    "GEOPOLITICS": ("地政学の緊張緩和", "地政学リスクの上昇"),
}

#: 計画（行動の前提）として読むノート種別。ここから opportunity 側が出る。
PLAN_NOTE_TYPES = ("target",)

#: 保有の前提として読むノート種別。ここから risk 側が出る。
HOLDING_NOTE_TYPES = ("thesis",)


def _direction_label(variable: str, direction: str) -> str:
    up, down = VARIABLE_LABELS.get(variable, (f"{variable}上昇", f"{variable}低下"))
    return up if direction == "up" else down


def _invert(direction: str) -> str:
    return "down" if direction == "up" else "up"


def extract_assumptions(text: str) -> list[str]:
    """散文から前提を抽出する。

    完全な抽出は原理的に無理なので、誤検出を避ける方向に倒している
    （存在しない前提を「共有している」と警告する方が、取りこぼしより有害）。
    """
    if not text:
        return []
    lowered = text.lower()
    found: list[str] = []
    for canonical, keywords in ASSUMPTION_PATTERNS.items():
        if any(kw in lowered for kw in keywords):
            found.append(canonical)
    return found


def build_assumption_map(notes: Iterable[dict]) -> dict[str, list[str]]:
    """thesis ノート群から「前提 → それに依存する銘柄」の対応を作る。

    Parameters
    ----------
    notes : iterable of dict
        note_manager の note レコード。``symbol`` と ``content`` を見る。

    Returns
    -------
    dict
        {前提: [銘柄, ...]}。銘柄は重複を除いて昇順。
    """
    mapping: dict[str, set[str]] = {}
    for note in notes:
        symbol = (note.get("symbol") or "").strip()
        if not symbol:
            continue
        text = f"{note.get('content', '')} {note.get('trigger', '')}"
        for assumption in extract_assumptions(text):
            mapping.setdefault(assumption, set()).add(symbol)
    return {k: sorted(v) for k, v in sorted(mapping.items())}


def restrict_to_holdings(
    assumption_map: dict[str, list[str]], holdings: Optional[list[dict]]
) -> dict[str, list[str]]:
    """保有していない銘柄の前提を落とす。

    thesis は売却済み銘柄や検討だけした銘柄についても書かれている。
    それらの前提を混ぜると、**保有していない銘柄由来の前提が exposure 0 で
    紛れ込み、HHI が不当に下がって「分散している」という偽の安心を生む。**
    """
    if not holdings:
        return dict(assumption_map)

    held = {h.get("symbol", "") for h in holdings if h.get("symbol")}
    restricted: dict[str, list[str]] = {}
    for assumption, symbols in assumption_map.items():
        kept = [s for s in symbols if s in held]
        if kept:
            restricted[assumption] = kept
    return restricted


def assumption_exposure(
    assumption_map: dict[str, list[str]],
    holdings: Optional[list[dict]] = None,
) -> dict[str, float]:
    """各前提が PF 評価額のどれだけを支えているかを出す。

    1銘柄が複数の前提に依存する場合、その銘柄の評価額は各前提に**重複して**
    計上する。「この前提が壊れたら、いくら分が影響を受けるか」を見たいのであって、
    配分を分け合わせたいのではないため。したがって合計は100%を超えうる。

    Parameters
    ----------
    holdings : list of dict
        ``symbol`` と ``value`` を持つ。省略時は銘柄数ベースで数える。
    """
    if holdings:
        values = {
            h.get("symbol", ""): float(h.get("value") or 0.0) for h in holdings
        }
        total = sum(values.values())
    else:
        values = {}
        total = 0.0

    exposure: dict[str, float] = {}
    for assumption, symbols in assumption_map.items():
        if total > 0:
            covered = sum(values.get(s, 0.0) for s in symbols)
            exposure[assumption] = covered / total
        else:
            exposure[assumption] = float(len(symbols))
    return exposure


def assumption_hhi(
    assumption_map: dict[str, list[str]],
    holdings: Optional[list[dict]] = None,
) -> dict:
    """前提空間の集中度を測る。

    資産空間のHHIと同じ機構（``compute_hhi``）を使うが、測る対象が違う。
    セクター分散済みでも前提HHIが高ければ「一つの物語に全張り」である。

    Returns
    -------
    dict
        {"hhi", "level", "top_assumption", "top_share", "exposure", "assumption_count"}
    """
    assumption_map = restrict_to_holdings(assumption_map, holdings)
    exposure = assumption_exposure(assumption_map, holdings)
    total = sum(exposure.values())

    # 前提が無い、または保有と紐づく前提の重みがゼロ。
    # ここで HHI 0.0 を「分散している」と報告すると偽の安心になる。
    if not exposure or total <= 0:
        return {
            "hhi": 0.0,
            "level": "unknown",
            "top_assumption": None,
            "top_share": 0.0,
            "exposure": {},
            "assumption_count": 0,
            "message": (
                "保有銘柄に紐づく前提を thesis から抽出できませんでした。"
                "**前提が分散しているという意味ではありません — 測れていないだけです。**"
                "投資理由をメモに残すと前提集中を測れます。"
            ),
        }
    weights = [v / total for v in exposure.values()] if total > 0 else []
    hhi = compute_hhi(weights) if weights else 0.0

    top_assumption, top_share = max(exposure.items(), key=lambda kv: kv[1])

    if hhi >= HHI_DANGER:
        level = "danger"
        message = (
            f"前提HHI {hhi:.2f} — **危険水準**。「{top_assumption}」に依存が集中しており、"
            f"この仮定が反転するとPFの広い範囲が同時に毀損する。"
            f"セクターが分散していても、これは分散していない。"
        )
    elif hhi >= HHI_WARNING:
        level = "warning"
        message = (
            f"前提HHI {hhi:.2f} — 要注意。「{top_assumption}」への依存が大きい。"
            f"価格相関は前提相関の遅行指標なので、まだ相関に現れていなくても油断できない。"
        )
    else:
        level = "ok"
        message = f"前提HHI {hhi:.2f} — 前提は分散している。"

    return {
        "hhi": round(hhi, 4),
        "level": level,
        "top_assumption": top_assumption,
        "top_share": round(top_share, 4),
        "exposure": {k: round(v, 4) for k, v in exposure.items()},
        "assumption_count": len(exposure),
        "message": message,
    }


def generate_inversion_scenario(
    assumption: str, exposed_symbols: Optional[list[str]] = None
) -> Optional[dict]:
    """「その前提が反転した世界」のシナリオを生成する。

    既存の ``SCENARIOS`` と同じ形で返すので、既存のシナリオ分析にそのまま流せる。
    定義を持たない前提には None を返す（推測で影響度を捏造しない）。
    """
    spec = INVERSION_EFFECTS.get(assumption)
    if spec is None:
        return None

    return {
        "name": f"{spec['inverted']}（あなたの前提「{assumption}」の反転）",
        "trigger": f"投資テーゼが前提としている「{assumption}」が崩れる",
        "base_shock": spec["base_shock"],
        "effects": {
            "primary": list(spec["effects"]),
            "secondary": [],
        },
        # 固定8シナリオと区別するための印
        "source": "assumption_inversion",
        "assumption": assumption,
        "exposed_symbols": list(exposed_symbols or []),
    }


def build_personal_scenarios(
    assumption_map: dict[str, list[str]],
    holdings: Optional[list[dict]] = None,
    limit: int = 3,
) -> list[dict]:
    """共有度の高い前提から順に、反転シナリオを生成する (案2の出口).

    固定8シナリオとは**別枠**。ユーザー固有の信念グラフ由来なので、
    「一般的な暴落」では見えない弱点を突く。
    """
    exposure = assumption_exposure(assumption_map, holdings)
    ranked = sorted(exposure.items(), key=lambda kv: kv[1], reverse=True)

    scenarios: list[dict] = []
    for assumption, _share in ranked:
        scenario = generate_inversion_scenario(
            assumption, assumption_map.get(assumption, [])
        )
        if scenario is not None:
            scenarios.append(scenario)
        if len(scenarios) >= limit:
            break
    return scenarios


# ---------------------------------------------------------------------------
# 前提の衝突 (改善4)
# ---------------------------------------------------------------------------


def build_assumption_records(
    notes: Iterable[dict],
    holdings: Optional[list[dict]] = None,
) -> list[dict]:
    """ノートから符号つき前提レコードを作る。

    thesis → 保有の前提（`role="risk"`。その方向に振れると痛む）
    target → 計画の前提（`role="opportunity"`。その方向を**待っている**）

    Returns
    -------
    list of dict
        {"variable", "direction", "role", "assumption", "holdings",
         "action_depends", "source_note", "symbol"}
    """
    held = {h.get("symbol", "") for h in (holdings or []) if h.get("symbol")}
    records: list[dict] = []

    for note in notes or []:
        note_type = str(note.get("type") or note.get("note_type") or "").strip()
        if note_type in PLAN_NOTE_TYPES:
            role, action_depends = "opportunity", True
        elif note_type in HOLDING_NOTE_TYPES:
            role, action_depends = "risk", False
        else:
            continue

        symbol = (note.get("symbol") or "").strip()
        # 保有の前提は、保有していない銘柄のものを混ぜない（HHI が不当に下がる）。
        # 計画の前提は、まだ保有していないからこそ計画なので落とさない。
        if role == "risk" and held and symbol and symbol not in held:
            continue

        text = f"{note.get('content', '')} {note.get('trigger', '')}"
        for assumption in extract_assumptions(text):
            spec = ASSUMPTION_VARIABLES.get(assumption)
            if spec is None:
                continue
            variable, break_direction = spec
            # risk      … その前提が崩れる方向（break_direction）で痛む
            # opportunity … その前提が実現する方向を待っている。
            #               「円高を待つ」なら前提は「円高転換」で、実現方向は
            #               break_direction の逆。
            direction = break_direction if role == "risk" else _invert(break_direction)
            records.append({
                "variable": variable,
                "direction": direction,
                "role": role,
                "assumption": assumption,
                "symbol": symbol,
                "holdings": [symbol] if symbol else [],
                "action_depends": action_depends,
                "source_note": note.get("id"),
                "note_type": note_type,
            })
    return records


def detect_conflicting_assumptions(assumptions: Iterable[dict]) -> list[dict]:
    """同一変数の同一方向に opportunity と risk が併存する構造を返す (改善4).

    条件:
      1. 同じ (variable, direction) に `role="opportunity"` と `role="risk"` が同居
      2. opportunity 側の `action_depends` が真

    これは「**その行動を実行する条件が揃った瞬間、原資が毀損する**」構造である。

    ## 検出しないもの（誤検出防止）

    - 同方向の集中（通貨の二重ロング等）— opportunity 側が無いので出ない。
      あれは `exposure.describe_tilt()` が見る別種の問題。
    - opportunity 側があっても `action_depends` が偽なら出ない
      （待ってはいるが、それに賭けた行動計画が無い状態）。
    """
    buckets: dict[tuple[str, str], dict[str, list[dict]]] = {}
    for record in assumptions or []:
        variable = record.get("variable")
        direction = record.get("direction")
        role = record.get("role")
        if not variable or direction not in ("up", "down") or role not in ("opportunity", "risk"):
            continue
        buckets.setdefault((variable, direction), {"opportunity": [], "risk": []})[role].append(record)

    conflicts: list[dict] = []
    for (variable, direction), sides in sorted(buckets.items()):
        opportunities = [r for r in sides["opportunity"] if r.get("action_depends")]
        risks = sides["risk"]
        if not opportunities or not risks:
            continue

        label = _direction_label(variable, direction)
        exposed = sorted({s for r in risks for s in (r.get("holdings") or []) if s})
        plans = sorted({r.get("assumption", "") for r in opportunities})
        conflicts.append({
            "variable": variable,
            "direction": direction,
            "direction_label": label,
            "opportunity": opportunities,
            "risk": risks,
            "plans": plans,
            "exposed_symbols": exposed,
            "message": (
                f"**{label}** は計画にとって好機ですが、保有にとっては逆風です"
                f"（{', '.join(exposed) if exposed else '該当保有'}）。"
                f"{label}が来た瞬間、その計画の原資が同時に目減りします。"
                "これは同方向の集中（通貨の二重ロング）とは別種で、"
                "**逆方向の依存が相殺せず、片方の実行が他方を壊す**構造です。"
            ),
            "what_to_check": (
                f"計画の実行条件（{label}の水準）に到達したとき、原資が"
                "いくら残っているかを先に見積もってください。"
                "『条件が揃ったら実行する』は、原資が無事であることを暗黙に前提にしています。"
            ),
        })
    return conflicts


def analyze_assumption_space(
    notes: Optional[Iterable[dict]] = None,
    holdings: Optional[list[dict]] = None,
    limit: int = 3,
) -> dict:
    """前提空間の分析一式。thesis を読んで HHI とシナリオを返す。

    notes 省略時は note_manager から thesis（保有の前提）と target（計画の前提）を読む。
    target も読むのは改善4のため —— **計画の前提が入らないと、前提の衝突は
    原理的に検出できない**（片側しか台帳に無い状態になる）。
    """
    if notes is None:
        collected: list[dict] = []
        try:
            from src.data.note_manager import load_notes

            for note_type in HOLDING_NOTE_TYPES + PLAN_NOTE_TYPES:
                for note in load_notes(note_type=note_type) or []:
                    note.setdefault("type", note_type)
                    collected.append(note)
        except Exception:
            collected = []
        notes = collected

    notes = list(notes)
    # HHI は従来どおり thesis（保有の前提）だけで測る。計画の前提を混ぜると
    # 「保有していない銘柄の前提」が exposure 0 で紛れ込み、HHI が不当に下がる。
    holding_notes = [n for n in notes
                     if str(n.get("type") or n.get("note_type") or "thesis")
                     in HOLDING_NOTE_TYPES]
    assumption_map = restrict_to_holdings(build_assumption_map(holding_notes), holdings)
    concentration = assumption_hhi(assumption_map, holdings)
    scenarios = build_personal_scenarios(assumption_map, holdings, limit=limit)

    records = build_assumption_records(notes, holdings)
    conflicts = detect_conflicting_assumptions(records)

    return {
        "assumption_map": assumption_map,
        "concentration": concentration,
        "scenarios": scenarios,
        "records": records,
        "conflicts": conflicts,
        # 計画の前提が1件も無ければ「衝突なし」ではなく「片側しか無い」。
        "conflict_detectable": any(r["role"] == "opportunity" for r in records),
    }
