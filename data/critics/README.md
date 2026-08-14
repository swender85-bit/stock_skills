# 批評家較正台帳 (改善5)

外部言説（ニュース・アナリスト・X上の論者）の**過去の的中率をドメイン別に**持つ台帳。

## なぜ要るか

系譜会計（`src/core/provenance.py`）は主張を四系譜に分けるが、
**「外部言説」に、その情報源が過去どれだけ当たったかという重みがない。**
需給読みで9回当てた情報源と、初出の情報源が同じ扱いになる。

## いまの状態（重要）

**判定への使用は保留しています。** 採点済みのテーゼが足りず、ドメイン別の重みを
出せる情報源がまだありません。`domain_weight()` は `available=False` を返し、
これは **「当たらない」ではなく「まだ測れていない」** として扱われます。

台帳の形式を先に作るのは、蓄積が始まらないと永遠に使えるようにならないためです。
効果が出るのは蓄積後です。

## ファイル形式

`data/critics/<source_id>.json`（`_` 始まりは雛形として無視されます）

```json
{
  "source_id": "critic_a",
  "name": "批評家A",
  "theses": [
    {
      "date": "2026-07-16",
      "claim": "規制の事前織り込みでSKHY急落",
      "domain": "supply_demand",
      "verified_on": "2026-07-16",
      "score": "hit_exact",
      "note": ""
    }
  ]
}
```

## 採点

| score | 重み | 意味 |
|:---|---:|:---|
| `hit_exact` | 1.0 | 方向も水準も当たった |
| `hit_direction` | 0.7 | 方向は当たった |
| `partial` | 0.4 | 部分的に当たった |
| `refuted` | 0.0 | 反証された |
| `pending` | — | **未検証。分母に入れない**（未検証と誤りは別物） |

**採点には `verified_on` が必須です。** 検証日の無い採点は、結果を見てから
付けた後知恵と区別できません。

## ドメイン

`supply_demand`（需給・資金フロー）/ `price_level`（価格水準の断言）/
`timing` / `fundamentals` / `macro` / `regulation` / `technology` / `sentiment`

**採点は主張ごと、重みはドメインごと。** 一人の批評家が需給には強く価格水準には
弱い、という実態は珍しくありません。情報源そのものを「信頼できる／できない」で
二値化すると、この構造が潰れて使い物になりません。

## 使う側の規約

`.claude/rules/provenance.md` に記載:

> 外部言説をレポート本文の根拠に使えるのは、その情報源のそのドメインの重みが
> **0.6 以上**のときのみ。それ未満は「◯◯氏の見解（当該ドメインでの過去的中率: n/m）」
> として引用形式で書く。

重みを出すには同一ドメインで**5件以上**の採点が必要です。少数の的中で
「この人は当たる」と決めると、偶然を実力と誤認します。

## 使い方

```python
from src.core.critic_calibration import add_thesis, build_thesis, domain_weight, profile

# 主張を記録する（結果が出る前）
add_thesis("critic_a", build_thesis("規制の事前織り込みで急落", "supply_demand"))

# 後日、採点する
critic = load_critic("critic_a")
critic["theses"][-1].update({"score": "hit_exact", "verified_on": "2026-07-16"})
save_critic(critic)

# 重みを見る
domain_weight("critic_a", "supply_demand")
profile("critic_a")            # ドメイン別の得手不得手
```

---

## 参照している情報源（X / Twitter）— 2026-08-05 登録

`config/critics.yaml` に5アカウント登録済み:

| source_id | X |
|:---|:---|
| `pirania0630` | https://x.com/pirania0630/ |
| `noirinvestor` | https://x.com/noirinvestor |
| `kokko_coco` | https://x.com/kokko_coco |
| `imuvill` | https://x.com/imuvill |
| `noatake1127` | https://x.com/noatake1127 |

**得意分野は先に決めていません。** 5人とも話す内容が違い、しかも一人の中でも
分野によって的中率が違います（需給には強いが価格水準の断言は外す、等）。
`config/critics.yaml` に「この人は◯◯の人」と書くと、実測がその先入観に
上書きされます。だから **分野は発言ごとに分類し、重みは分野ごとに実測** します。

## 運用（3ステップ）

```bash
# 1. 取り込む（週1回。XAI_API_KEY が要る）
python scripts/fetch_critics.py --days 7            # プレビュー（何も書かない）
python scripts/fetch_critics.py --days 7 --apply    # 台帳に追記

# 2. 採点する（検証期限が来た言明を実測で自動採点）
python scripts/score_critics.py --apply

# 3. 手で採点すべきものを見る（定性的な主張は機械で判定できない）
python scripts/score_critics.py --show-manual
```

### 自動採点できるもの / できないもの

| 発言 | 採点 |
|:---|:---|
| 「NVDAは$250まで行く」 | ✅ 自動（価格ターゲット） |
| 「7203.T は来週上がる」 | ✅ 自動（方向 + 期間） |
| 「今の地合いは良くない」 | ❌ 手動（定性的） |

- **要約しない。** 原文をそのまま台帳に入れます。要約した時点で自己推論が混ざり、
  後から「本人が何と言ったか」を検証できなくなります。
- 価格が取れなかった場合は **`refuted` にしません**。取得失敗を「外れた」と
  記録すると台帳そのものが汚染されます。
- 取得に失敗した週を **「発言が無かった」と報告しません**（§16-1）。

## レポートでの扱い（重みが出るまで）

重みが未測定のあいだ、5人の発言は週次レポートに
**「◯◯氏の見解（当該分野の的中率 未測定）」** という引用形式でのみ出ます。
**その見解を前提に判断を組み立てることはしません。**

重みが 0.6 以上になった分野から順に、本文の根拠として使えるようになります。
重みを出すには同一分野で5件以上の採点が要ります。
