# AGENTS.md

- 本体は `tools/tower.py`（Python 3.11+ 標準ライブラリのみ）。依存を足さない
- 変更したら `python3 -m unittest discover -s tests` と `python3 tools/tower.py validate` を通す
- 鍵の値をログ・ファイル・コミットに出さない。作ったトークンは `mask()` で伏せ字にする
- KV・D1・R2・DNS を消す処理は入れない（消すのはトークンだけ）
- 鍵を使うワークフローを `pull_request` / `pull_request_target` で動かさない
- サンプル・テストのデータは架空の名前（otter など）にする
- 報告・README は日本語で、「版」ではなく「バージョン」と書く
