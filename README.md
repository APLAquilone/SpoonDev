# SpoonDev

Spoon 日本版の公開配信・リスナー一覧を取得し、配信者とリスナーの関係と温度 (`favorite_temperature`) を時刻付きで SQLite に蓄積します。Python 3.12 以降と標準ライブラリ、バックグラウンド起動には Linux の `flock` を使います。

## 保存する情報

- 数値のアカウントIDを主キーにします。同名の別ユーザーを区別できます。
- プロフィールに表示されるID (`tag`) と表示名 (`nickname`) は別に履歴として保存します。ハンドルが将来変更されても数値IDで追跡します。
- 配信ID、配信者ID、リスナーID、UTC観測日時、取得できた温度を保存します。温度は配信者とリスナーの関係ごとの観測値です。温度の計算方法は断定しません。
- 全ページ取得と部分取得を区別します。取得失敗を「リスナー0人」や退出の証拠にしません。

取得できるのは API が公開するリスナーです。匿名・非表示リスナーの身元は分かりません。ページを取得する間にも入退室が起きるため、完全に同時刻の一覧ではありません。集計の回数は観測回数であり、実際の視聴時間ではありません。

## 開発・検証

既存の `/workspace/SpoonDev` を使います。クラウドタスクは独立した環境なので worktree を追加する必要はありません。依存パッケージの導入やログイン、シークレットは不要です。

```sh
cd /workspace/SpoonDev
python -m unittest discover -s tests -v
python -m spoondev init-db
python -m spoondev collect-spoon --max-rooms 3 --once
```

Spoon の Web クライアントが使う `https://jp-api.spooncast.net/lives/` と `/lives/{id}/listeners/` を実応答で確認しています。`next` の同一ホスト・同一パスのURLを巡回し、全ページのリスナーを取得します。配信終了やエラー時は部分観測か失敗として扱います。音声、メール、認証情報、APIの生レスポンスや room_token は保存しません。

## 定期収集

```sh
python -m spoondev collect-spoon --max-rooms 0 --interval 300 --concurrency 4
```

`--max-rooms 0` は配信一覧に載る全配信が対象です。既定値は10配信なので、全配信には明示的に0を指定します。ページ数の安全上限は各一覧100ページです。上限に達するとエラーを報告するため、全件取得を断言しません。配信ごとに並列取得し、同一ホストへのリクエスト開始には間隔を設けます。最低周期は30秒ですが、通常は5分以上にします。取得ラウンド完了後に指定時間待つため、厳密な時計上の5分周期ではありません。

バックグラウンド起動は以下です。ファイルロックにより同じDBのコレクターが二重起動するのを防ぎます。

```sh
bash scripts/start-collector.sh
```

ログは `data/collector.log`、DBは `data/spoondev.sqlite3` です。直近起動要求のPIDは `data/collector.last-start.pid` にあります。二重起動の要求は終了するため、PIDの存在だけで稼働判定せず、ログとDBに新しい観測があることを確認します。終了には稼働中の `python -m spoondev ... collect-spoon` プロセスにSIGINTかSIGTERMを送ります。クラウド環境の停止・再作成・Publishでプロセスは維持されません。保存した起動手順に従って再起動してください。この開発環境が常時稼働サーバーになる保証はありません。

## 集計と温度履歴

```sh
python -m spoondev report broadcaster NUMERIC_BROADCASTER_ID
python -m spoondev report listener NUMERIC_LISTENER_ID
python -m spoondev history NUMERIC_BROADCASTER_ID NUMERIC_LISTENER_ID
```

`report` は観測回数、初回/最終観測、プロフィールID、最後に取得できた温度を返します。欠測は0と扱いません。温度の取得日時は `history` で確認できます。履歴には未取得の温度も null として現れます。

任意のDBには `python -m spoondev --db /path/to/db.sqlite3 ...` を使います。`examples/snapshot.json` は架空のデータで、本番DBにインポートしないでください。JSONインポートと、検証済みJSONアダプター向けの汎用 `collect --url ...` も利用できます。同じ観測の再インポートは新しい観測として記録されます。

## 接続と運用上の制約

必要な接続先は `www.spooncast.net` と `jp-api.spooncast.net` です。公式Webクライアントが参照する利用規約・プライバシーのホストは `spoonvip.oopy.io` と `spoon-privacy.oopy.io` で、接続許可を設定ドラフトに追加しています。現在、その規約ホストへの接続はプロキシ403で遮断され、規約本文は未確認です。公開APIへのアクセス成功は大規模・長期収集の利用許可を意味しません。長期運用前に利用条件を確認してください。

DBとログはGitから除外しています。個人単位の行動履歴を含むため、アクセス範囲と必要な保存期間を決めて運用してください。データや分析結果は自動公開しません。
