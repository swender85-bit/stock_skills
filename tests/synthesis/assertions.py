"""節が満たすべき性質 -- §16 の8原則を「文章のレベル」で縛る (改善1).

## なぜ必要か

    Python層  : テスト 4,584件
    synthesis層: テスト 0件

`.claude/prompts/weekly_deep.md` の品質は誰も測っていなかった。プロンプトを触っても、
節の質が落ちたことに気づく手段がない。`agent-decomposition` の要点は「分解すること」
ではなく **「分解した各ステップを evals で検証すること」** であり、そこが空いていた。

## 設計方針

1. **返り値は例外ではなく結果オブジェクト。**
   eval harness は「8件中いくつ通ったか」を知りたいので、最初の失敗で止まっては困る。
   pytest 用には `assert_*` の薄い包み（失敗時に送出）を別に用意する。

2. **`skip` を `pass` と混ぜない。**
   これは §16-1（取得失敗を結果と混同しない）を**この harness 自身に適用**したもの。
   節単位では判定できない検査を「通過」と数えると、通過率が嘘になる。

3. **最初は緩く作る。** 文章の性質を部分一致で判定する以上、誤検出・見逃しは避けられない。
   厳しくしすぎると**正しい出力を落として運用が止まる**ので、
   「明らかな違反だけを落とす」方向に倒してある。締めるのは誤検出が出てからでよい。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional


PASS, FAIL, SKIP = "pass", "fail", "skip"


def result(
    name: str,
    status: str,
    principle: str,
    message: str,
    evidence: Optional[list[str]] = None,
) -> dict:
    """検査結果。`status` は pass / fail / skip の3値（2値にしない理由は冒頭参照）。"""
    return {
        "name": name,
        "status": status,
        "principle": principle,
        "message": message,
        "evidence": list(evidence or [])[:5],
    }


# ---------------------------------------------------------------------------
# 語彙
# ---------------------------------------------------------------------------

#: 「取れなかった」を「結果」にすり替える言い回し (§16-1)
ZERO_CLAIM_PATTERNS = (
    "0件", "０件", "なし", "無し", "問題なし", "問題ありません", "健全です",
    "執行率0", "執行率 0", "0%", "０%", "該当なし", "特にありません",
    "異常なし", "すべて正常", "全て正常",
)

#: 取得失敗を取得失敗として書いている印。これがあれば上の語があってもよい。
UNAVAILABLE_DISCLOSURE = (
    "取得できな", "取得でき ", "取得失敗", "未取得", "取れなかった", "取れず",
    "判定不能", "測れて", "測定不能", "測定できて", "未点検", "未記録", "未計測",
    "不明", "データ蓄積中", "蓄積中", "available", "利用できな", "参照できな",
    "確認できな", "算出できな", "計算できな", "無効", "スキップ",
)

#: 循環照合を独立検証と偽らないための開示 (§16-3)
CIRCULAR_DISCLOSURE = (
    "独立検証", "循環", "circular", "生成元", "同一データ", "同じデータ",
    "自己参照", "検証になっていな", "検証にならな",
)

#: 循環なのに「合っている」と言い切る語
MATCH_CLAIM_PATTERNS = (
    "一致", "照合OK", "照合ok", "照合は問題", "差分なし", "ズレなし", "ずれなし",
    "整合しています", "合致",
)

#: 土曜に書いてはならない買い推奨 (§16-5 / 第0原則)
BUY_RECOMMENDATION_PATTERNS = (
    "買い推奨", "買うべき", "今買う", "今が買い", "買い時", "買い場",
    "購入を推奨", "買いを推奨", "買い増しを推奨", "取得を推奨",
    "エントリー推奨", "推奨:買い", "推奨: 買い", "推奨は買い",
    "仕込むべき", "拾うべき",
)

#: 条件付き政策の形になっていれば買いの語があってもよい (第0原則の許容形)
CONDITIONAL_MARKERS = (
    "なら", "ならば", "場合", "条件", "以下で", "以上で", "割れたら", "到達したら",
    "下回ったら", "上回ったら", "とき", "時に", "たら", "限り", "満たせば",
)

#: 否定・戒めの文脈。買いの語があっても推奨ではない。
NEGATION_MARKERS = (
    "書かない", "書くな", "しない", "すべきではない", "べきでない", "禁止",
    "慎重", "戒め", "控える", "見送", "ではありません", "ではない", "не",
)

#: 退避キャッシュの鮮度開示 (§12.2)
CACHE_DISCLOSURE = ("退避", "キャッシュ", "cached", "前週取得", "鮮度", "時間前", "時点のもの")

#: 四半期YoYスパイクに添えるべき期間ラベル (§5.1)
GROWTH_WINDOW_LABELS = (
    "四半期", "yoy", "YoY", "年度", "通期", "前年同期", "期間", "growth_annual",
    "スパイク", "単四半期",
)

#: 孤児ポジションを孤児として書いている印 (§17.2)
ORPHAN_MARKERS = (
    "孤児", "なぜ持って", "保有理由", "thesis", "テーゼが", "政策が", "未記述",
    "記述されて", "書かれていな", "判断基準が",
)

#: 節見出し → 固定骨格上の位置 (§7.1)
SECTION_ORDER_KEYWORDS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (0, ("今週の判定", "判定")),
    (1, ("照合",)),
    (2, ("信念",)),
    (3, ("前方イベント", "前方")),
    (4, ("制約",)),
    (5, ("機会", "過熱")),
    (6, ("事前決定",)),
    (7, ("監査",)),
    (8, ("前提と限界", "系譜")),
)


# ---------------------------------------------------------------------------
# 低レベルユーティリティ
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    return (text or "").replace("　", " ")


def content_lines(text: str) -> list[str]:
    """空行と frontmatter を除いた本文行。分量制御の判定に使う。"""
    out: list[str] = []
    in_fm = False
    for i, raw in enumerate(_norm(text).splitlines()):
        line = raw.strip()
        if line == "---" and (i == 0 or in_fm):
            in_fm = not in_fm
            continue
        if in_fm or not line:
            continue
        out.append(line)
    return out


def sentences(text: str) -> list[str]:
    """句点・改行・箇条書き記号で切る。**行をまたいだ誤検出を避けるため。**"""
    chunks: list[str] = []
    for line in _norm(text).splitlines():
        for part in re.split(r"(?<=[。！？!?])\s*", line):
            part = part.strip()
            if part:
                chunks.append(part)
    return chunks


def has_any(text: str, patterns) -> bool:
    lowered = (text or "").lower()
    return any(p.lower() in lowered for p in patterns)


def matched(text: str, patterns) -> list[str]:
    lowered = (text or "").lower()
    return [p for p in patterns if p.lower() in lowered]


def looks_like_full_report(text: str) -> bool:
    """全体レポートか、1節だけか。分量・順序の検査は全体にしか適用できない。"""
    return len(re.findall(r"^## ", _norm(text), flags=re.MULTILINE)) >= 3


# ---------------------------------------------------------------------------
# パックから「取得できなかったもの」を洗い出す
# ---------------------------------------------------------------------------

#: パック上のパス → レポート本文でその話題を指す語。
#: **別名を持たない項目は検査しない**（何と呼ばれているか分からないものは判定できない）。
SUBJECT_ALIASES: dict[str, tuple[str, ...]] = {
    "execution_audit.survival": ("決定生存率", "生存率", "執行率", "執行監査", "執行の"),
    "execution_audit.shortfall": ("ショートフォール", "執行コスト"),
    "execution_audit.performance": ("売買成績", "執行成績"),
    "model_audit.score": ("模型健全性", "模型監査", "模型の採点", "模型スコア", "模型信頼"),
    "forward.triggers": ("トリガー",),
    "forward.monday_outlook": ("月曜寄付", "月曜の寄り", "先物", "ADR"),
    "narrative.crowding": ("混雑度", "物語混雑"),
    "constraints.loss_harvest": ("含み損", "損出し", "税務価値"),
    "constraints.attention": ("注意予算",),
    "moomoo.fed_watch": ("fedwatch", "FOMC"),
    "moomoo.economic_events": ("経済指標",),
    "moomoo.earnings": ("決算カレンダー",),
    "moomoo.news": ("moomoo",),
    "vol_calibration": ("ボラ較正", "較正", "前提σ"),
}


def _walk(node: Any, path: str = "") -> list[tuple[str, dict]]:
    """dict を再帰的に歩いて (パス, dict) を返す。リスト要素はインデックスを畳む。"""
    found: list[tuple[str, dict]] = []
    if isinstance(node, dict):
        found.append((path, node))
        for key, value in node.items():
            found += _walk(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for item in node:
            found += _walk(item, path)
    return found


def _is_unavailable(node: dict) -> bool:
    if node.get("available") is False:
        return True
    if node.get("fetched") is False:
        return True
    if str(node.get("status") or "") == "unavailable":
        return True
    return False


def unavailable_subjects(pack: dict) -> list[dict]:
    """「取得できなかった項目」を、本文中での呼ばれ方つきで列挙する。

    ここで拾えるのは別名を定義してある項目だけ。**網羅ではない**ので、
    この検査が通ったことは「取得失敗の混同が無い」の証明にはならない。
    """
    subjects: list[dict] = []
    seen: set[str] = set()

    for path, node in _walk(pack):
        if not _is_unavailable(node):
            continue

        # 銘柄単位の日程は、銘柄名・ティッカーがそのまま本文の呼称になる
        if path.startswith("schedule_status"):
            names = [str(node.get(k)) for k in ("symbol", "name") if node.get(k)]
            if names:
                key = f"schedule:{names[0]}"
                if key not in seen:
                    seen.add(key)
                    subjects.append({"path": path, "aliases": tuple(names),
                                     "reason": node.get("label") or node.get("error")})
            continue

        aliases: tuple[str, ...] = ()
        for prefix, names in SUBJECT_ALIASES.items():
            if path == prefix or path.startswith(prefix + "."):
                aliases = names
                break
        if not aliases or path in seen:
            continue
        seen.add(path)
        subjects.append({"path": path, "aliases": aliases,
                         "reason": node.get("reason") or node.get("error")})

    return subjects


# ---------------------------------------------------------------------------
# 検査 8 種
# ---------------------------------------------------------------------------


def check_no_unavailable_as_zero(text: str, pack: dict, **_kw) -> dict:
    """§16-1 取得失敗を結果と混同しない。

    `available=false` の項目について「0件」「なし」「問題なし」「執行率0%」等が、
    取得失敗の明示を伴わずに書かれていたら FAIL。
    """
    name = "no_unavailable_as_zero"
    principle = "§16-1 取得失敗を結果と混同しない"
    subjects = unavailable_subjects(pack)
    if not subjects:
        return result(name, SKIP, principle, "パックに取得失敗項目がありません（検査対象なし）")

    violations: list[str] = []
    for sent in sentences(text):
        if not has_any(sent, ZERO_CLAIM_PATTERNS):
            continue
        if has_any(sent, UNAVAILABLE_DISCLOSURE):
            continue  # 取得失敗として書いてある。正しい。
        for subject in subjects:
            if has_any(sent, subject["aliases"]):
                violations.append(f"[{subject['path']}] {sent}")
                break

    if violations:
        return result(
            name, FAIL, principle,
            f"取得できなかった項目を『結果』として書いています（{len(violations)}件）。"
            "「0件」ではなく「取得できなかった」と書く必要があります。",
            violations,
        )
    return result(name, PASS, principle,
                  f"取得失敗 {len(subjects)}項目を結果として書いていません")


def check_circular_disclosed(text: str, pack: dict, **_kw) -> dict:
    """§16-3 循環照合を「一致」と呼ばない。

    `circular` / `independently_verified=false` なのに一致を言い切り、
    「独立検証ではない」旨が無ければ FAIL。
    """
    name = "circular_disclosed"
    principle = "§16-3 循環照合を一致と呼ばない"
    rec = pack.get("reconciliation") or {}
    circular = rec.get("status") == "circular" or rec.get("independently_verified") is False
    if not circular:
        return result(name, SKIP, principle, "独立検証が成立しているため検査対象外")

    if has_any(text, CIRCULAR_DISCLOSURE):
        return result(name, PASS, principle, "独立検証が成立していない旨を開示しています")

    claims = [s for s in sentences(text) if has_any(s, MATCH_CLAIM_PATTERNS)]
    if claims:
        return result(
            name, FAIL, principle,
            "循環照合（模型の生成元と同一データ）なのに一致を言い切り、"
            "『独立検証ではない』旨がありません。",
            claims,
        )
    return result(name, PASS, principle, "一致の言い切りがありません")


def check_no_buy_recommendation(text: str, pack: Optional[dict] = None, **_kw) -> dict:
    """§16-5 / 第0原則 土曜は買い推奨を出さない。

    条件節（「〜なら」「〜到達したら」）を伴わない買いの断定だけを落とす。
    条件付き政策は**正しい出力形式**なので落としてはならない。
    """
    name = "no_buy_recommendation"
    principle = "§16-5 土曜は買い推奨を出さない（出力形式は条件付き政策）"
    violations: list[str] = []
    for sent in sentences(text):
        hits = matched(sent, BUY_RECOMMENDATION_PATTERNS)
        if not hits:
            continue
        if has_any(sent, CONDITIONAL_MARKERS) or has_any(sent, NEGATION_MARKERS):
            continue
        violations.append(f"{hits[0]} → {sent}")

    if violations:
        return result(
            name, FAIL, principle,
            f"条件節を伴わない買い推奨が {len(violations)}件あります。"
            "土曜の出力は『〈条件〉なら〈行動〉』の条件付き政策でなければなりません。",
            violations,
        )
    return result(name, PASS, principle, "無条件の買い推奨はありません")


QUIET_REPORT_MAX_LINES = 30


def check_quiet_week_length(text: str, pack: dict, **_kw) -> dict:
    """§7.4 静穏週は30行以内。

    節単位のテキストでは全体の分量を判定できないので `skip` を返す。
    ここを `pass` にすると通過率が嘘になる（§16-1 を harness 自身に適用）。
    """
    name = "quiet_week_length"
    principle = "§7.4 分量は情報量に比例させる（静穏週は30行以内）"
    if not (pack.get("information") or {}).get("quiet"):
        return result(name, SKIP, principle, "静穏週ではないため検査対象外")
    if not looks_like_full_report(text):
        return result(name, SKIP, principle,
                      "節単位のテキストでは全体30行の制約を判定できません")

    lines = content_lines(text)
    if len(lines) > QUIET_REPORT_MAX_LINES:
        return result(
            name, FAIL, principle,
            f"静穏週なのに {len(lines)}行あります（上限 {QUIET_REPORT_MAX_LINES}行）。"
            "短く終えることは失敗ではなく正しい出力です。",
            lines[QUIET_REPORT_MAX_LINES:QUIET_REPORT_MAX_LINES + 3],
        )
    return result(name, PASS, principle, f"{len(lines)}行（上限 {QUIET_REPORT_MAX_LINES}行）")


def check_orphan_flagged(text: str, pack: dict, **_kw) -> dict:
    """§17.2 孤児ポジションを孤児として書く。

    「なぜ持っているか未記述」に相当する記述が無ければ FAIL。
    """
    name = "orphan_flagged"
    principle = "§17.2 孤児ポジション（thesis も政策も無い保有）を名指しする"
    orphans = (pack.get("reconciliation") or {}).get("orphans") or []
    if not orphans:
        return result(name, SKIP, principle, "孤児ポジションがないため検査対象外")

    if not has_any(text, ORPHAN_MARKERS):
        labels = [str(o.get("symbol") or o.get("name") or "") for o in orphans[:5]]
        return result(
            name, FAIL, principle,
            f"孤児ポジションが {len(orphans)}件あるのに、"
            "『なぜ持っているか未記述』に相当する記述がありません。",
            labels,
        )

    # 評価額比の併記は「あるべき」だが、無いことを FAIL にはしない（緩く始める）
    burden = (pack.get("reconciliation") or {}).get("orphan_burden_pct")
    note = ""
    if burden is not None and str(round(float(burden))) not in text and f"{burden}" not in text:
        note = f"（評価額比 {burden}% の併記が見当たりません）"
    return result(name, PASS, principle,
                  f"孤児 {len(orphans)}件を孤児として扱っています{note}")


def check_section_order(text: str, pack: Optional[dict] = None, **_kw) -> dict:
    """§7.1 固定骨格。「機会」が「照合」「制約」より前に出たら FAIL。"""
    name = "section_order"
    principle = "§7.1 固定骨格（照合→信念→前方→制約→機会→事前決定→監査）"
    if not looks_like_full_report(text):
        return result(name, SKIP, principle, "節単位のテキストでは順序を判定できません")

    seen: list[tuple[int, str]] = []
    for line in _norm(text).splitlines():
        if not line.startswith("##"):
            continue
        heading = line.lstrip("#").strip()
        num = re.match(r"^(\d)\s*[\.．]", heading)
        if num:
            seen.append((int(num.group(1)), heading))
            continue
        for order, keys in SECTION_ORDER_KEYWORDS:
            if any(k in heading for k in keys):
                seen.append((order, heading))
                break

    if len(seen) < 2:
        return result(name, SKIP, principle, "順序を判定できる見出しが足りません")

    opportunity = next((i for i, (o, _) in enumerate(seen) if o == 5), None)
    if opportunity is not None:
        for later in (1, 4):
            pos = next((i for i, (o, _) in enumerate(seen) if o == later), None)
            if pos is not None and pos > opportunity:
                label = {1: "照合", 4: "制約"}[later]
                return result(
                    name, FAIL, principle,
                    f"「機会」が「{label}」より前に出ています。"
                    "自宅が燃えているかを確認する前に買い物へ行く順序です。",
                    [h for _, h in seen],
                )

    inversions = [f"{seen[i][1]} → {seen[i + 1][1]}"
                  for i in range(len(seen) - 1) if seen[i][0] > seen[i + 1][0]]
    if inversions:
        return result(name, FAIL, principle,
                      f"固定骨格の順序が入れ替わっています（{len(inversions)}箇所）",
                      inversions)
    return result(name, PASS, principle, f"{len(seen)}見出しが固定骨格の順序です")


#: これを超える成長率は四半期YoYのスパイクとみなす（financials.py と同じ考え方）
GROWTH_SPIKE_PCT = 300.0


def _spike_symbols(pack: dict) -> list[str]:
    out: list[str] = []
    for holding in pack.get("holdings") or []:
        fundamentals = holding.get("fundamentals") or {}
        if holding.get("growth_period_warning") or fundamentals.get("growth_period_warning"):
            out.append(str(holding.get("symbol") or holding.get("name") or ""))
            continue
        for key in ("revenue_growth", "earnings_growth"):
            value = fundamentals.get(key)
            if isinstance(value, (int, float)) and abs(value) * 100 > GROWTH_SPIKE_PCT:
                out.append(str(holding.get("symbol") or holding.get("name") or ""))
                break
    return out


def check_growth_window_labeled(text: str, pack: dict, **_kw) -> dict:
    """§5.1 ±300%超の成長率は年度基準を併記する。

    四半期YoYの異常値を期間の注記なしに書いたら FAIL。
    """
    name = "growth_window_labeled"
    principle = "§16-2 窓の違う量を比較しない（四半期YoY vs 年度）"
    symbols = _spike_symbols(pack)
    if not symbols:
        return result(name, SKIP, principle, "スパイク成長率を持つ保有がありません")

    quoted = [s for s in sentences(text)
              if re.search(r"[+\-＋−]?\s*\d{3,}(?:\.\d+)?\s*[%％]", s)
              or re.search(r"\d{2,}\s*倍", s)]
    if not quoted:
        return result(name, PASS, principle, "スパイク値を本文に書いていません")

    unlabeled = [s for s in quoted if not has_any(s, GROWTH_WINDOW_LABELS)]
    if unlabeled:
        return result(
            name, FAIL, principle,
            f"四半期YoYのスパイク値を期間の注記なしに書いています（{len(unlabeled)}件）。"
            f"対象: {', '.join(symbols)}。年度基準（growth_annual）を主として書く必要があります。",
            unlabeled,
        )
    return result(name, PASS, principle, f"スパイク値 {len(quoted)}件すべてに期間注記があります")


def _cached_events(pack: dict) -> list[dict]:
    events: list[dict] = []
    calendar = ((pack.get("forward") or {}).get("calendar") or {})
    for bucket in ("events", "folded"):
        for event in calendar.get(bucket) or []:
            if "cached" in str(event.get("source") or "").lower():
                events.append(event)
    return events


def check_cache_age_disclosed(text: str, pack: dict, **_kw) -> dict:
    """§12.2 退避キャッシュの鮮度を伏せない。

    `source="moomoo(cached)"` の材料を本文で使いながら `cached_age_hours` に
    相当する言及が無ければ FAIL。
    """
    name = "cache_age_disclosed"
    principle = "§12.2 退避キャッシュの鮮度を伏せない"
    events = _cached_events(pack)
    if not events:
        return result(name, SKIP, principle, "退避キャッシュ由来の材料がありません")

    titles = [str(e.get("title") or "") for e in events if e.get("title")]
    kinds = {str(e.get("kind") or "") for e in events}
    mentioned = any(t and t[:6] in text for t in titles)
    if not mentioned and "fomc" in kinds:
        mentioned = "FOMC" in text
    if not mentioned and "economic" in kinds:
        mentioned = "経済指標" in text
    if not mentioned:
        return result(name, PASS, principle, "退避材料を本文で使っていません")

    if not has_any(text, CACHE_DISCLOSURE):
        ages = [e.get("cached_age_hours") for e in events if e.get("cached_age_hours") is not None]
        return result(
            name, FAIL, principle,
            "退避キャッシュ由来のマクロ材料を、鮮度を伏せて最新であるかのように"
            f"書いています（cached_age_hours={ages[0] if ages else '不明'}）。",
            titles[:3],
        )
    return result(name, PASS, principle, "退避であることと鮮度を開示しています")


# ---------------------------------------------------------------------------
# レジストリ
# ---------------------------------------------------------------------------

#: 検査名 → (関数, 適用する節の kind。None は全節)
#: `weekly_deep_driver.build_sections()` の kind と対応する。
CHECKS: dict[str, tuple[Callable[..., dict], Optional[frozenset]]] = {
    "no_unavailable_as_zero": (check_no_unavailable_as_zero, None),
    "circular_disclosed": (check_circular_disclosed, None),
    "no_buy_recommendation": (check_no_buy_recommendation, None),
    "quiet_week_length": (check_quiet_week_length, frozenset({"report"})),
    "orphan_flagged": (check_orphan_flagged, frozenset({"reconcile", "report"})),
    "section_order": (check_section_order, frozenset({"report"})),
    "growth_window_labeled": (check_growth_window_labeled,
                              frozenset({"holding", "heat", "report"})),
    "cache_age_disclosed": (check_cache_age_disclosed,
                            frozenset({"forward", "macro", "schedule", "report"})),
}


def run_checks(
    text: str, pack: dict, section_kind: str = "report",
    only: Optional[list[str]] = None,
) -> list[dict]:
    """該当する検査を全部走らせる。**失敗しても止めない。**

    Parameters
    ----------
    section_kind : str
        `weekly_deep_driver` の節 kind。`"report"` は全体レポート。
        節に適用対象でない検査は結果に含めない（skip ですらない＝無関係）。
    """
    results: list[dict] = []
    for name, (fn, kinds) in CHECKS.items():
        if only and name not in only:
            continue
        if kinds is not None and section_kind not in kinds:
            continue
        try:
            results.append(fn(text, pack))
        except Exception as exc:  # 検査自身の事故でレポート評価を止めない
            results.append(result(name, SKIP, "harness",
                                  f"検査が例外で落ちました: {type(exc).__name__}: {exc}"))
    return results


def summarize(results: list[dict]) -> dict:
    """通過率。**skip は分母から外す**（判定していないものを通過に数えない）。"""
    passed = sum(1 for r in results if r["status"] == PASS)
    failed = sum(1 for r in results if r["status"] == FAIL)
    skipped = sum(1 for r in results if r["status"] == SKIP)
    judged = passed + failed
    return {
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "judged": judged,
        "pass_rate": (passed / judged) if judged else None,
        "ok": failed == 0,
        "label": f"{passed}/{judged} 通過" + (f"（{skipped}件は判定対象外）" if skipped else ""),
    }


# ---------------------------------------------------------------------------
# pytest 用の送出ラッパ（仕様書の assert_* 名を保つ）
# ---------------------------------------------------------------------------


def _raise_if_failed(res: dict) -> dict:
    if res["status"] == FAIL:
        evidence = "\n  - ".join(res["evidence"]) if res["evidence"] else "(なし)"
        raise AssertionError(
            f"[{res['name']}] {res['principle']}\n{res['message']}\n  - {evidence}"
        )
    return res


def assert_no_unavailable_as_zero(text: str, pack: dict) -> dict:
    return _raise_if_failed(check_no_unavailable_as_zero(text, pack))


def assert_circular_disclosed(text: str, pack: dict) -> dict:
    return _raise_if_failed(check_circular_disclosed(text, pack))


def assert_no_buy_recommendation(text: str, pack: Optional[dict] = None) -> dict:
    return _raise_if_failed(check_no_buy_recommendation(text, pack or {}))


def assert_quiet_week_length(text: str, pack: dict) -> dict:
    return _raise_if_failed(check_quiet_week_length(text, pack))


def assert_orphan_flagged(text: str, pack: dict) -> dict:
    return _raise_if_failed(check_orphan_flagged(text, pack))


def assert_section_order(text: str, pack: Optional[dict] = None) -> dict:
    return _raise_if_failed(check_section_order(text, pack or {}))


def assert_growth_window_labeled(text: str, pack: dict) -> dict:
    return _raise_if_failed(check_growth_window_labeled(text, pack))


def assert_cache_age_disclosed(text: str, pack: dict) -> dict:
    return _raise_if_failed(check_cache_age_disclosed(text, pack))


def assert_all(text: str, pack: dict, section_kind: str = "report") -> list[dict]:
    """全検査を走らせ、1件でも FAIL があればまとめて送出する。"""
    results = run_checks(text, pack, section_kind)
    failures = [r for r in results if r["status"] == FAIL]
    if failures:
        body = "\n\n".join(
            f"[{r['name']}] {r['principle']}\n{r['message']}\n  - "
            + "\n  - ".join(r["evidence"] or ["(証拠なし)"])
            for r in failures
        )
        raise AssertionError(f"{len(failures)}件の検査に失敗しました:\n\n{body}")
    return results
