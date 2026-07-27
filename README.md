# EA-check-account

Myfxbook API巡回によるEA稼働確認（一次スクリーニング層）。

- ダッシュボード: https://mflab-inc.github.io/EA-check-account/
- 機械可読フィード: https://mflab-inc.github.io/EA-check-account/data/status.json

## 検知対象と限界

| 検知できる | 検知できない |
|---|---|
| ターミナル停止・接続断（Myfxbook更新停止） | 自動売買OFF・EAがチャートから外れた状態（※） |
| 取引頻度の異常（無取引期間） | EAの内部エラー（次の取引失敗まで潜伏） |
| グリッド系のポジション構成長期不変 | Myfxbook更新間隔ぶんのラグ |
| 浮動DDの拡大（balance/equity乖離） | |

※この穴は第二層（ターミナル側ハートビートEA）で塞ぐ予定。

## 公開データのマスク方針

リポジトリはPublic（Pages利用のため）。公開される `data/status.json` には
**口座番号・残高・エクイティ・損益額・gain・drawdownを含めない**。
出力するのは稼働確認に必要な項目のみ：
口座名／状態レベル／最終更新／更新停止時間／無取引日数／保有ポジション数／
浮動DD比率（%のみ、金額なし）／判定理由。
Actionsのジョブサマリーも同基準（Publicリポジトリではログ・サマリーも公開のため）。

## Myfxbook API認証について

APIキーは存在しない。Myfxbookの通常ログイン（メール＋パスワード）で
`login.json` を叩きセッションIDを得る方式（公式: https://www.myfxbook.com/api）。
事前テスト:

```bash
curl "https://www.myfxbook.com/api/login.json?email=MAIL&password=PASS"
# {"error":false,...,"session":"..."} が返ればOK
```

`error:true` の場合はパスワード誤り、または該当Myfxbookアカウントの
2段階認証が有効（APIログイン不可のためOFFにする必要あり）。

## セットアップ

1. リポジトリを mflab-inc に `EA-check-account`（Public）として作成
2. Settings → Secrets and variables → Actions → New repository secret
   - Name: `MYFXBOOK_CREDENTIALS`
   - Value: 下記形式のJSON（1行でも整形済みでも可）

```json
{
  "investar1":  {"email": "kentasaku78@yahoo.co.jp",  "password": "regista1119"},
  "investar2":  {"email": "kazumon453@yahoo.co.jp",   "password": "regista1119"},
  "investar3":  {"email": "simobbk@yahoo.co.jp",      "password": "regista1119"},
  "investar12": {"email": "itimisb@yahoo.co.jp",      "password": "regista1119"},
  "investar17": {"email": "salmon_san@yahoo.co.jp",   "password": "regista1119"},
  "investar17": {"email": "zack14574@yahoo.co.jp",   "password": "regista1119"},
  "investar17": {"email": "a7703370@yahoo.co.jp",   "password": "regista1119"}
}
```

   巡回対象にしたいMyfxbookアカウントだけ記載すればよい（記載分のみログインする）。

3. Settings → Pages → Deploy from a branch → main / (root)
4. Actions → 「EA稼働確認」→ Run workflow で初回手動実行
5. `data/status.json` が生成され、ダッシュボードに反映されることを確認

## スケジュール

- 平日 07:00 JST / 15:00 JST（土日はブローカー停止による誤検知を避けるため実行しない）
- 手動実行: workflow_dispatch

## 閾値

`config/thresholds.yaml` を参照。初期値：

- 更新停止: 6h→WARN / 24h→ALERT
- 無取引: 7日→WATCH / 14日→WARN
- ポジション構成不変: 7日→WATCH
- 浮動DD: 残高比20%→WARN

無取引閾値は当面固定値。運用データが溜まった段階でEA別の取引間隔
95パーセンタイル基準へ移行する（Ideal Standard等の低頻度EAの誤検知対策）。

## 注意事項

- Myfxbook APIは公式ドキュメント（https://www.myfxbook.com/api）準拠。
  レート制限への配慮としてアカウント間にsleepを挟んでいる。
- `get-history.json` は全履歴を返すため、口座数×履歴量によっては
  実行時間が延びる。timeout 30分で足りない場合は履歴取得の省略化を検討。
- 週明け月曜07:00 JSTの実行は、週末をまたいだ「更新停止」を
  誤検知する可能性が理論上ある（lastUpdateが金曜クローズ時刻のままの場合）。
  初週の実行結果を見て、月曜朝のみ閾値を緩める等の調整を行う。
