# SpoonDev

配信者とリスナーの関係を、ユーザーIDと観測日時をキーに SQLite に蓄積する Python 3.12 以降のツールです。表示名が同じ別ユーザーを区別し、名前変更履歴を保存します。集計は観測回数であり、視聴時間を意味しません。

## 現在の状態

DB、JSONインポート、双方向集計、制限付き並列HTTP取得、定期実行を実装しています。**Spoon のリスナー一覧APIは未検証で、Spoon固有の取得アダプターは未実装です。現時点で実サイトからの自動収集は完成していません。** サイトへの接続がネットワークプロキシで遮断され、公開範囲・IDフィールド・ページング・利用条件を確認できませんでした。

プロフィールに表示されるIDと内部IDが同じか、不変かも未確認です。取得元を検証してから一意で安定したアカウントIDを `id` に対応付けます。表示名やコメント投稿者、視聴者数からリスナーを推測しません。認証・閲覧制限の回避は行いません。

## 開発と動作確認

既存のチェックアウト `/workspace/SpoonDev` を使います。クラウドタスクは独立した環境なので worktree の追加は不要です。外部ランタイム依存パッケージやシークレットはありません。

```sh
cd /workspace/SpoonDev
python -m unittest discover -s tests -v
python -m spoondev --db /tmp/spoondev-demo.sqlite3 init-db
python -m spoondev --db /tmp/spoondev-demo.sqlite3 import examples/snapshot.json
python -m spoondev --db /tmp/spoondev-demo.sqlite3 report broadcaster fixture-host-1
python -m spoondev --db /tmp/spoondev-demo.sqlite3 report listener fixture-listener-1
```

サンプルは架空のデータです。本番DBに投入しないでください。同じ観測の再インポートは新しい観測として保存されるため、運用では取得イベントを重複投入しないでください。

## 収集アダプターの契約

`examples/snapshot.json` の形式のJSONを返す、検証済みの取得アダプターを用意します。1URLにつき1配信の観測です。`observed_at` は実際の取得時刻とタイムゾーンを含め、`complete` は全ページの取得が成功した場合のみ true とします。部分的に見えた参加者は false として保存できます。失敗は空一覧として保存しません。

```sh
python -m spoondev collect --url https://YOUR-VERIFIED-ADAPTER.example/snapshot --once
python -m spoondev collect --url https://YOUR-VERIFIED-ADAPTER.example/snapshot --interval 300 --concurrency 4
```

上のURLは例示であり実在する取得先ではありません。実サイトのURLや未確認のAPIをそのまま指定しても動きません。並列数は1〜16、取得周期は30秒以上です。実際にはサービスの利用条件・レート制限に従い、必要な対象と頻度に絞ってください。HTTPエラーのラウンドは保存せず、次の周期で再取得します。

DBには `data/spoondev.sqlite3` を使い、Gitから除外します。アクセス権と保存期間は運用前に決め、不要な個人単位の履歴を保存し続けないでください。公開前提のデータでも分析結果の公開範囲は慎重に設定してください。

## 実データ取得までの残作業

環境設定で `www.spooncast.net` と候補APIホスト `jp-api.spooncast.net` の接続許可を反映後、サイトの公開リスナー一覧、安定ID、ページング、利用条件を確認します。APIホストは未検証の候補です。その実応答に合わせてアダプターを実装し、少数の配信で機能検証してから対象を増やします。
