# tower

Cloudflare の「トークン」「データ置き場（KV・D1・R2）」「DNS」「公開ワークフロー」を、このリポ1つでまとめて面倒を見る仕組みです。
TestFlight 用の [runway](https://github.com/kajisho5/runway) の Cloudflare 版です。

- アプリごとに **必要な権限だけ** を持つトークンを自動で作り、そのアプリのリポの Secrets に入れます
- 期限が近づいたら（21日前）自動で作り直して入れ替えます
- KV・D1・R2・DNS レコードを、設定ファイルに書いたとおりに用意します（消すことはしません）
- 各アプリのリポからは、共通の公開ワークフローを数行で呼ぶだけで Workers / Pages に公開できます
- 困ったことがあれば、このリポに Issue を立てて知らせます（GitHub アプリに通知が来ます）

鍵はすべて GitHub の Secrets に入り、リポの中身には書かれません。公開リポにしても鍵は漏れません。

## しくみ

```
tower リポ（Secrets に親トークン）
  │  main に反映 / 毎週月曜
  ▼
GitHub Actions
  ├─ 1時間だけ使える作業用トークンを作る → KV・D1・R2・DNS を用意 → 作業用トークンを消す
  └─ アプリごとのトークンを作る → 各リポの Secrets に入れる → 古いトークンを消す
```

親トークンが持つのは「トークンを作る権限」だけです。データや DNS を触るときは、その都度1時間で切れる作業用トークンを作って使い、終わったら消します。

## 最初の準備（1回だけ）

### 1. Cloudflare で親トークンを作る

1. 次の URL を開く（権限が選ばれた状態で開きます）
   https://dash.cloudflare.com/?to=/:account/api-tokens&permissionGroupKeys=%5B%7B%22key%22%3A%22account_api_tokens%22%2C%22type%22%3A%22edit%22%7D%5D&name=tower-master
2. アカウントを選ぶ画面が出たら、自分のアカウントを押す
3. 権限が「Account API Tokens（アカウント API トークン）」の「Edit（編集）」になっていることを確かめる。それ以外は足さない
4. 「Token expiration（期限）」は1年後くらいにする（tower が残り21日で Issue で知らせます）
5. 「Continue to summary」→「Create Token」を押す
6. 出てきた値をコピーする（この画面でしか見られません）

### 2. アカウント ID を控える

https://dash.cloudflare.com/ を開いたときの URL `dash.cloudflare.com/` のすぐ後ろにある32文字の英数字がアカウント ID です。

### 3. GitHub の鍵を作る（各リポの Secrets に書き込むため）

1. https://github.com/settings/personal-access-tokens/new を開く
2. 「Token name」に `tower` と入れる
3. 「Expiration」は1年にする
4. 「Repository access」で「All repositories」を選ぶ
5. 「Permissions」の「Repository permissions」で次の2つを「Read and write」にする
   - Secrets
   - Variables
6. 一番下の「Generate token」を押し、出てきた値をコピーする

### 4. tower リポの Secrets に3つ入れる

https://github.com/kajisho5/tower/settings/secrets/actions/new を開き、「Name」と「Secret」を入れて「Add secret」を押す、を3回くり返します。

| Name | 中身 |
| --- | --- |
| `CLOUDFLARE_API_TOKEN` | 1 の親トークン |
| `CLOUDFLARE_ACCOUNT_ID` | 2 のアカウント ID |
| `TOWER_GITHUB_TOKEN` | 3 の GitHub の鍵 |

### 5. 動くか確かめる

https://github.com/kajisho5/tower/actions/workflows/apply.yml を開き、右の「Run workflow」を押して「予定だけ表示して何も変えない」にチェックを入れ、緑の「Run workflow」を押します。緑の丸になれば準備完了です。

## アプリを足す

1. `projects/_sample.toml` を複製して `projects/アプリ名.toml` にする
2. `repo` と `permissions` などを書き換える（書き方はファイル内の説明を参照）
3. main に反映する → 自動で反映が走り、そのアプリのリポの Secrets に
   `CLOUDFLARE_API_TOKEN` と `CLOUDFLARE_ACCOUNT_ID` が入ります
4. アプリのリポに `examples/deploy-workers.yml`（Pages なら `examples/deploy-pages.yml`）を
   `.github/workflows/deploy.yml` として置く → main に反映するたびに公開されます

作った KV・D1 の ID は、アプリのリポの Variables に `CF_KV_名前_ID` / `CF_D1_名前_ID` として入ります。

### 使える権限のあだ名

| あだ名 | できること | 範囲 |
| --- | --- | --- |
| `workers` | Workers の公開 | アカウント |
| `workers_routes` | Workers をドメインにつなぐ | `zones` のドメインだけ |
| `pages` | Pages の公開 | アカウント |
| `kv` / `d1` / `r2` | それぞれのデータの読み書き | アカウント |
| `dns` | DNS の書き換え | `zones` のドメインだけ |

あだ名のほかに、Cloudflare の正式な権限名（例: `"Queues Write"`）も書けます。正式な名前の一覧は
https://github.com/kajisho5/tower/actions/workflows/permissions.yml の「Run workflow」で出せます。

## ワークフロー

| 名前 | いつ動くか | すること |
| --- | --- | --- |
| apply | `projects/` を main に反映したとき・手動 | リソース・DNS・トークンを反映 |
| watch | 毎週月曜 9:17（日本時間）・手動 | 期限が近いトークンを作り直す。親トークンと GitHub の鍵の期限を確かめる |
| permissions | 手動 | 使える権限名の一覧 |
| validate | プルリクエスト・main への反映 | 設定ファイルの書き方とテスト（鍵は使わない） |
| deploy-workers / deploy-pages | 各アプリのリポから呼ばれたとき | 公開 |

## 安全のための決まり

- 消すのはトークンだけ。KV・D1・R2・DNS は作る・書き換えるだけで、消しません
- 設定から消したアプリのトークンも自動では消さず、Issue で知らせます
- プルリクエストでは鍵を使うワークフローは動きません（validate だけ）
- 新しいトークンを配れなかったときは、そのトークンをすぐ消します

## 手元で試す

```sh
python3 tools/tower.py validate          # 設定の書き方だけ確かめる
python3 -m unittest discover -s tests    # 偽の Cloudflare を相手にしたテスト
```

Python 3.11 以降（標準ライブラリだけ）で動きます。

## ライセンス

MIT
