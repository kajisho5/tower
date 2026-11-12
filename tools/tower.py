#!/usr/bin/env python3
"""tower — Cloudflare のトークン・リソース・DNS を GitHub Actions からまとめて面倒を見る道具。

Python 標準ライブラリだけで動く。GitHub 側の Secrets / Variables への書き込みは gh コマンドを使う。

使い方:
  python3 tools/tower.py validate            設定ファイルの書き方だけ確かめる（鍵は不要）
  python3 tools/tower.py apply [--dry-run]   リソース作成 → DNS → トークン発行と配布
  python3 tools/tower.py watch [--dry-run]   期限切れが近いトークンを作り直し、親トークンの期限も確かめる
  python3 tools/tower.py permissions         使える権限の名前を一覧表示

必要な環境変数（validate 以外）:
  CLOUDFLARE_API_TOKEN   親トークン（Account API Tokens: Edit だけを持つアカウントトークン）
  CLOUDFLARE_ACCOUNT_ID  Cloudflare のアカウント ID
  GH_TOKEN               配布先リポの Secrets / Variables に書き込める GitHub の鍵
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("CF_API_BASE", "https://api.cloudflare.com/client/v4").rstrip("/")
PREFIX = "tower:"
TEMP_PREFIX = "tower-temp:"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 権限の「あだ名」→ 実際の権限名の候補（上から順に、見つかった最初のものを使う）。
# Cloudflare は権限名を Edit / Write の両方で表記していることがあるため、両方を候補に入れている。
# 第2要素はかかる範囲: account（アカウント全体）か zone（指定したドメインだけ）。
PRESETS: dict[str, tuple[list[list[str]], str]] = {
    # 新しい Worker を初めて作るときは Workers Admin が要る（Editor や旧来の Scripts Write は既存のものだけ）
    "workers": ([["Workers Admin", "Workers Scripts Write", "Workers Scripts Edit"],
                 ["Account Settings Read"]], "account"),
    "workers_routes": ([["Workers Routes Write", "Workers Routes Edit"]], "zone"),
    "pages": ([["Pages Write", "Pages Edit", "Cloudflare Pages Write", "Cloudflare Pages Edit"],
               ["Account Settings Read"]], "account"),
    "kv": ([["Workers KV Storage Write", "Workers KV Storage Edit"]], "account"),
    "d1": ([["D1 Write", "D1 Edit"]], "account"),
    "r2": ([["Workers R2 Storage Write", "Workers R2 Storage Edit"]], "account"),
    "dns": ([["DNS Write", "DNS Edit"], ["Zone Read"]], "zone"),
}
# tower 自身が作業する間だけ使う一時トークンの権限
WORK_PRESETS = ["kv", "d1", "r2", "dns"]

SECRET_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class TowerError(Exception):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def mask(value: str) -> None:
    # GitHub Actions のログで値を伏せ字にする
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{value}", flush=True)


# ---------------------------------------------------------------- 設定ファイル

def load_projects(pattern: str | None = None) -> list[dict]:
    pattern = pattern or os.environ.get("TOWER_PROJECTS") or os.path.join(ROOT, "projects", "*.toml")
    projects = []
    for path in sorted(glob.glob(pattern)):
        name = os.path.basename(path)
        if name.startswith("_"):
            continue
        with open(path, "rb") as f:
            try:
                data = tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                raise TowerError(f"{name}: 書き方の誤り（{e}）")
        data["_file"] = name
        projects.append(data)
    return projects


def validate_project(p: dict) -> list[str]:
    errs = []
    f = p.get("_file", "?")
    repo = p.get("repo")
    if not isinstance(repo, str) or not REPO_RE.match(repo):
        errs.append(f"{f}: repo は「持ち主/リポ名」の形で書いてください")
    elif repo == os.environ.get("GITHUB_REPOSITORY", "kajisho5/tower"):
        errs.append(f"{f}: tower 自身を配布先にはできません（親トークンが上書きされます）")
    perms = p.get("permissions", [])
    if not isinstance(perms, list) or not all(isinstance(x, str) for x in perms):
        errs.append(f"{f}: permissions は文字の並び（例: [\"workers\", \"kv\"]）で書いてください")
        perms = []
    zones = p.get("zones", [])
    if not isinstance(zones, list):
        errs.append(f"{f}: zones は並びで書いてください")
        zones = []
    if any(x in PRESETS and PRESETS[x][1] == "zone" for x in perms) and not zones:
        errs.append(f"{f}: dns や workers_routes を使うときは zones にドメインを書いてください")
    for key in ("secret_token", "secret_account"):
        v = p.get(key)
        if v is not None and (not isinstance(v, str) or not SECRET_RE.match(v)):
            errs.append(f"{f}: {key} は英大文字・数字・_ だけで書いてください")
    days = p.get("expires_days", 90)
    if not isinstance(days, int) or not 1 <= days <= 365:
        errs.append(f"{f}: expires_days は 1〜365 の数字にしてください")
    for kind, field in (("kv", "title"), ("d1", "name"), ("r2", "name")):
        for item in p.get(kind, []):
            if not isinstance(item, dict) or not isinstance(item.get(field), str) or not item[field]:
                errs.append(f"{f}: [[{kind}]] には {field} が必要です")
    for rec in p.get("dns", []):
        if not isinstance(rec, dict) or not all(isinstance(rec.get(k), str) for k in ("zone", "type", "name", "content")):
            errs.append(f"{f}: [[dns]] には zone・type・name・content が必要です")
    unknown = [x for x in perms if x not in PRESETS and x.lower() == x and " " not in x]
    if unknown:
        errs.append(f"{f}: 知らないあだ名 {unknown}（使えるのは {', '.join(PRESETS)}、"
                    "または「D1 Write」のような正式な権限名）")
    return errs


def validate_all(projects: list[dict]) -> list[str]:
    errs = []
    seen = {}
    for p in projects:
        errs += validate_project(p)
        r = p.get("repo")
        if r in seen:
            errs.append(f"{p['_file']}: repo {r} は {seen[r]} と重なっています")
        seen[r] = p["_file"]
    return errs


def spec_hash(p: dict) -> str:
    spec = {"permissions": sorted(p.get("permissions", [])), "zones": sorted(p.get("zones", []))}
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:8]


def token_name(p: dict) -> str:
    return f"{PREFIX}{p['repo']}:{spec_hash(p)}"


def env_name(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", s.upper())


# ---------------------------------------------------------------- Cloudflare API

class Cloudflare:
    def __init__(self, token: str, account: str):
        self.token = token
        self.account = account

    def call(self, method: str, path: str, body: dict | None = None, token: str | None = None):
        url = API + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token or self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                payload = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read() or b"{}")
            except ValueError:
                payload = {}
            msgs = "; ".join(f"{x.get('code')}: {x.get('message')}" for x in payload.get("errors", []))
            raise TowerError(f"Cloudflare {method} {path} が失敗（{e.code}）{msgs}")
        if not payload.get("success", True):
            raise TowerError(f"Cloudflare {method} {path} が失敗: {payload.get('errors')}")
        return payload

    def all_pages(self, path: str, token: str | None = None, per_page: int = 50) -> list:
        out, page = [], 1
        sep = "&" if "?" in path else "?"
        while True:
            pl = self.call("GET", f"{path}{sep}per_page={per_page}&page={page}", token=token)
            res = pl.get("result") or []
            out += res
            info = pl.get("result_info") or {}
            if page >= int(info.get("total_pages") or 1) or not res:
                return out
            page += 1

    # トークン
    def tokens(self) -> list[dict]:
        return self.all_pages(f"/accounts/{self.account}/tokens")

    def create_token(self, name: str, policies: list, days: float) -> dict:
        exp = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).replace(microsecond=0)
        body = {"name": name, "policies": policies, "expires_on": exp.strftime("%Y-%m-%dT%H:%M:%SZ")}
        res = self.call("POST", f"/accounts/{self.account}/tokens", body)["result"]
        if res.get("value"):
            mask(res["value"])
        return res

    def delete_token(self, token_id: str) -> None:
        self.call("DELETE", f"/accounts/{self.account}/tokens/{token_id}")

    def permission_groups(self) -> list[dict]:
        return self.call("GET", f"/accounts/{self.account}/tokens/permission_groups")["result"]

    def verify(self) -> dict:
        return self.call("GET", f"/accounts/{self.account}/tokens/verify")["result"]


def find_group(groups: list[dict], candidates: list[str]) -> dict | None:
    by = {g.get("name", "").lower(): g for g in groups}
    for c in candidates:
        if c.lower() in by:
            return by[c.lower()]
    return None


def group_scope(g: dict, default: str) -> str:
    scopes = g.get("scopes") or []
    if any(s.endswith(".zone") for s in scopes):
        return "zone"
    if scopes:
        return "account"
    return default


def resolve_permissions(groups: list[dict], names: list[str]) -> list[tuple[dict, str]]:
    """あだ名・権限名を (権限グループ, 範囲) の並びに直す。"""
    out, missing = [], []
    for n in names:
        if n in PRESETS:
            sets, default = PRESETS[n]
            for candidates in sets:
                g = find_group(groups, candidates)
                if g is None:
                    missing.append(f"{n}（候補: {' / '.join(candidates)}）")
                else:
                    out.append((g, group_scope(g, default)))
        else:
            g = find_group(groups, [n])
            if g is None:
                missing.append(n)
            else:
                out.append((g, group_scope(g, "account")))
    if missing:
        raise TowerError("見つからない権限: " + ", ".join(missing)
                         + "。permissions ワークフローで正しい名前を確かめてください")
    uniq = {}
    for g, s in out:
        uniq[g["id"]] = (g, s)
    return list(uniq.values())


def build_policies(account: str, perms: list[tuple[dict, str]], zone_ids: list[str] | None) -> list:
    acct = [{"id": g["id"]} for g, s in perms if s == "account"]
    zone = [{"id": g["id"]} for g, s in perms if s == "zone"]
    pols = []
    if acct:
        pols.append({"effect": "allow", "resources": {f"com.cloudflare.api.account.{account}": "*"},
                     "permission_groups": acct})
    if zone:
        if zone_ids is None:  # アカウント内のすべてのドメイン
            res = {f"com.cloudflare.api.account.{account}": {"com.cloudflare.api.account.zone.*": "*"}}
        else:
            res = {f"com.cloudflare.api.account.zone.{z}": "*" for z in zone_ids}
        pols.append({"effect": "allow", "resources": res, "permission_groups": zone})
    return pols


# ---------------------------------------------------------------- GitHub（gh コマンド）

class GitHub:
    def __init__(self, dry: bool):
        self.dry = dry

    def _run(self, args: list[str], body: str) -> None:
        if self.dry:
            return
        r = subprocess.run(["gh", *args], input=body, text=True, capture_output=True)
        if r.returncode != 0:
            raise TowerError(f"gh {' '.join(args[:3])} が失敗: {r.stderr.strip()}")

    def set_secret(self, repo: str, name: str, value: str) -> None:
        self._run(["secret", "set", name, "--repo", repo], value)

    def set_variable(self, repo: str, name: str, value: str) -> None:
        self._run(["variable", "set", name, "--repo", repo, "--body", value], "")


# ---------------------------------------------------------------- 作業の本体

class Report:
    def __init__(self):
        self.lines: list[str] = []
        self.problems: list[str] = []

    def ok(self, s: str) -> None:
        log("  ✓ " + s)
        self.lines.append("- " + s)

    def bad(self, s: str) -> None:
        log("  ✗ " + s)
        self.problems.append("- " + s)

    def write(self, path: str | None) -> None:
        if not path:
            return
        with open(path, "a", encoding="utf-8") as f:
            f.write("## tower の結果\n\n")
            f.write("\n".join(self.lines) or "- 変更なし")
            if self.problems:
                f.write("\n\n### 困ったこと\n\n" + "\n".join(self.problems))
            f.write("\n")


class Temp:
    """作業の間だけ使う一時トークン（1時間で切れる。終わったら消す）。"""

    def __init__(self, cf: Cloudflare, groups: list[dict], dry: bool):
        self.cf, self.groups, self.dry = cf, groups, dry
        self.tok: dict | None = None

    def __enter__(self):
        if self.dry:
            return self
        perms = resolve_permissions(self.groups, WORK_PRESETS)
        run = os.environ.get("GITHUB_RUN_ID", "local")
        self.tok = self.cf.create_token(f"{TEMP_PREFIX}{run}", build_policies(self.cf.account, perms, None), 1 / 24)
        return self

    @property
    def value(self) -> str | None:
        return self.tok["value"] if self.tok else None

    def __exit__(self, *exc):
        if self.tok:
            try:
                self.cf.delete_token(self.tok["id"])
            except TowerError as e:
                log(f"  ! 一時トークンを消せませんでした（1時間で自動で切れます）: {e}")
        return False


def zone_ids(cf: Cloudflare, temp: Temp, names: list[str], cache: dict) -> list[str]:
    out = []
    for n in names:
        if n not in cache:
            if temp.dry:
                cache[n] = f"(dry-run:{n})"
            else:
                q = urllib.parse.urlencode({"name": n, "account.id": cf.account})
                res = cf.call("GET", f"/zones?{q}", token=temp.value)["result"]
                if not res:
                    raise TowerError(f"ドメイン {n} が Cloudflare に見つかりません")
                cache[n] = res[0]["id"]
        out.append(cache[n])
    return out


def ensure_resources(cf: Cloudflare, temp: Temp, gh: GitHub, p: dict, rep: Report, zcache: dict) -> None:
    a, t, repo = cf.account, temp.value, p["repo"]

    def remember(kind: str, label: str, rid: str):
        var = f"CF_{kind}_{env_name(label)}_ID"
        gh.set_variable(repo, var, rid)

    for item in p.get("kv", []):
        title = item["title"]
        if temp.dry:
            rep.ok(f"[予定] KV「{title}」を用意"); continue
        found = [n for n in cf.all_pages(f"/accounts/{a}/storage/kv/namespaces", token=t, per_page=100)
                 if n.get("title") == title]
        if found:
            rid = found[0]["id"]
        else:
            rid = cf.call("POST", f"/accounts/{a}/storage/kv/namespaces", {"title": title}, token=t)["result"]["id"]
            rep.ok(f"KV「{title}」を作成")
        remember("KV", title, rid)

    for item in p.get("d1", []):
        name = item["name"]
        if temp.dry:
            rep.ok(f"[予定] D1「{name}」を用意"); continue
        found = [d for d in cf.all_pages(f"/accounts/{a}/d1/database", token=t) if d.get("name") == name]
        if found:
            rid = found[0].get("uuid") or found[0].get("id")
        else:
            body = {"name": name}
            if item.get("location"):
                body["primary_location_hint"] = item["location"]
            res = cf.call("POST", f"/accounts/{a}/d1/database", body, token=t)["result"]
            rid = res.get("uuid") or res.get("id")
            rep.ok(f"D1「{name}」を作成")
        remember("D1", name, rid)

    for item in p.get("r2", []):
        name = item["name"]
        if temp.dry:
            rep.ok(f"[予定] R2「{name}」を用意"); continue
        try:
            cf.call("GET", f"/accounts/{a}/r2/buckets/{urllib.parse.quote(name)}", token=t)
        except TowerError:
            body = {"name": name}
            if item.get("location"):
                body["locationHint"] = item["location"]
            cf.call("POST", f"/accounts/{a}/r2/buckets", body, token=t)
            rep.ok(f"R2「{name}」を作成")

    for rec in p.get("dns", []):
        zid = zone_ids(cf, temp, [rec["zone"]], zcache)[0]
        fqdn = rec["zone"] if rec["name"] in ("@", "") else f"{rec['name']}.{rec['zone']}"
        if rec["name"].endswith("." + rec["zone"]):
            fqdn = rec["name"]
        want = {"type": rec["type"].upper(), "name": fqdn, "content": rec["content"],
                "ttl": int(rec.get("ttl", 1)), "proxied": bool(rec.get("proxied", False))}
        if rec.get("comment"):
            want["comment"] = rec["comment"]
        if temp.dry:
            rep.ok(f"[予定] DNS {want['type']} {fqdn} → {want['content']}"); continue
        q = urllib.parse.urlencode({"type": want["type"], "name": fqdn})
        cur = cf.call("GET", f"/zones/{zid}/dns_records?{q}", token=t)["result"]
        if not cur:
            cf.call("POST", f"/zones/{zid}/dns_records", want, token=t)
            rep.ok(f"DNS {want['type']} {fqdn} を追加")
        elif want["type"] in ("A", "AAAA", "CNAME") or len(cur) == 1:
            c = cur[0]
            if (c.get("content"), bool(c.get("proxied")), int(c.get("ttl", 1))) != (want["content"], want["proxied"], want["ttl"]):
                cf.call("PUT", f"/zones/{zid}/dns_records/{c['id']}", want, token=t)
                rep.ok(f"DNS {want['type']} {fqdn} を書き換え")
        elif not any(c.get("content") == want["content"] for c in cur):
            cf.call("POST", f"/zones/{zid}/dns_records", want, token=t)
            rep.ok(f"DNS {want['type']} {fqdn} を追加")


def issue_token(cf: Cloudflare, gh: GitHub, groups: list[dict], temp: Temp, p: dict,
                existing: list[dict], rep: Report, zcache: dict, renew_days: int, force: bool = False) -> None:
    repo = p["repo"]
    perms_names = p.get("permissions", [])
    if not perms_names:
        return
    want = token_name(p)
    mine = [t for t in existing if t.get("name", "").startswith(f"{PREFIX}{repo}:")]
    current = [t for t in mine if t.get("name") == want and t.get("status", "active") == "active"]
    now = dt.datetime.now(dt.timezone.utc)

    def expiring(t):
        e = t.get("expires_on")
        if not e:
            return False
        return dt.datetime.fromisoformat(e.replace("Z", "+00:00")) - now < dt.timedelta(days=renew_days)

    if current and not force and not any(expiring(t) for t in current):
        return
    reason = "権限が変わった" if mine and not current else ("期限が近い" if current else "新規")
    if gh.dry:
        rep.ok(f"[予定] {repo} にトークンを発行（{reason}）"); return
    perms = resolve_permissions(groups, perms_names)
    zids = zone_ids(cf, temp, p.get("zones", []), zcache) if p.get("zones") else []
    tok = cf.create_token(want, build_policies(cf.account, perms, zids), p.get("expires_days", 90))
    try:
        gh.set_secret(repo, p.get("secret_token", "CLOUDFLARE_API_TOKEN"), tok["value"])
        gh.set_secret(repo, p.get("secret_account", "CLOUDFLARE_ACCOUNT_ID"), cf.account)
    except TowerError:
        cf.delete_token(tok["id"])  # 配れなかった新トークンは残さない
        raise
    for old in mine:
        cf.delete_token(old["id"])
    rep.ok(f"{repo} にトークンを発行して Secrets に入れた（{reason}）")


def cmd_apply(args, cf: Cloudflare, gh: GitHub, rep: Report, only_tokens: bool = False) -> int:
    projects = load_projects()
    errs = validate_all(projects)
    if errs:
        for e in errs:
            rep.bad(e)
        return 1
    groups = cf.permission_groups()
    existing = cf.tokens()
    zcache: dict = {}
    failed = 0
    with Temp(cf, groups, gh.dry) as temp:
        for p in projects:
            log(f"■ {p['repo']}（{p['_file']}）")
            try:
                if not only_tokens:
                    ensure_resources(cf, temp, gh, p, rep, zcache)
                issue_token(cf, gh, groups, temp, p, existing, rep, zcache, args.renew_days)
            except TowerError as e:
                failed += 1
                rep.bad(f"{p['repo']}: {e}")
    # 設定ファイルから消えたリポのトークンは、消さずに知らせるだけ
    known = {p["repo"] for p in projects}
    for t in existing:
        n = t.get("name", "")
        if n.startswith(PREFIX):
            repo = n[len(PREFIX):].rsplit(":", 1)[0]
            if repo not in known:
                rep.bad(f"設定にないリポのトークンが残っています: {n}（不要なら Cloudflare の画面で削除）")
        elif n.startswith(TEMP_PREFIX) and not gh.dry and (temp.tok is None or t.get("id") != temp.tok.get("id")):
            try:
                cf.delete_token(t["id"])
                rep.ok(f"残っていた一時トークン {n} を削除")
            except TowerError:
                pass
    return 1 if failed else 0


def cmd_watch(args, cf: Cloudflare, gh: GitHub, rep: Report) -> int:
    code = cmd_apply(args, cf, gh, rep, only_tokens=True)
    # 親トークンの期限
    try:
        me = cf.verify()
        exp = me.get("expires_on")
        if not exp:
            for t in cf.tokens():
                if t.get("id") == me.get("id"):
                    exp = t.get("expires_on")
        if exp:
            left = dt.datetime.fromisoformat(exp.replace("Z", "+00:00")) - dt.datetime.now(dt.timezone.utc)
            if left < dt.timedelta(days=args.renew_days):
                rep.bad(f"親トークンの期限まで残り {left.days} 日です。作り直してください")
                code = 1
            else:
                rep.ok(f"親トークンは有効（残り {left.days} 日）")
        else:
            rep.ok("親トークンは有効（期限なし）")
    except TowerError as e:
        rep.bad(f"親トークンを確かめられませんでした: {e}")
        code = 1
    # GitHub の鍵の期限（細かい権限の鍵は応答ヘッダーで期限が分かる）
    gh_exp = github_token_expiry()
    if gh_exp:
        left = gh_exp - dt.datetime.now(dt.timezone.utc)
        if left < dt.timedelta(days=args.renew_days):
            rep.bad(f"GitHub の鍵（TOWER_GITHUB_TOKEN）の期限まで残り {left.days} 日です")
            code = 1
        else:
            rep.ok(f"GitHub の鍵は有効（残り {left.days} 日）")
    return code


def github_token_expiry() -> dt.datetime | None:
    tok = os.environ.get("GH_TOKEN")
    if not tok or os.environ.get("TOWER_SKIP_GH_EXPIRY"):
        return None
    req = urllib.request.Request("https://api.github.com/rate_limit", method="GET")
    req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            h = r.headers.get("github-authentication-token-expiration")
    except (urllib.error.URLError, OSError):
        return None
    if not h:
        return None
    # 例: "2026-12-31 00:00:00 UTC"
    try:
        return dt.datetime.strptime(h.replace(" UTC", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def cmd_permissions(cf: Cloudflare) -> int:
    for g in sorted(cf.permission_groups(), key=lambda g: g.get("name", "")):
        scope = group_scope(g, "?")
        log(f"{g.get('name')}\t{scope}\t{g.get('id')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Cloudflare のトークン・リソース・DNS をまとめて管理")
    ap.add_argument("command", choices=["validate", "apply", "watch", "permissions"])
    ap.add_argument("--dry-run", action="store_true", help="何も変えずに予定だけ表示")
    ap.add_argument("--renew-days", type=int, default=int(os.environ.get("TOWER_RENEW_DAYS", "21")),
                    help="期限まで何日を切ったら作り直すか（既定 21 日）")
    args = ap.parse_args(argv)
    rep = Report()
    try:
        if args.command == "validate":
            errs = validate_all(load_projects())
            for e in errs:
                rep.bad(e)
            if not errs:
                rep.ok(f"設定ファイル {len(load_projects())} 件、書き方に問題なし")
            code = 1 if errs else 0
        else:
            token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
            account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
            if not token or not account:
                raise TowerError("CLOUDFLARE_API_TOKEN と CLOUDFLARE_ACCOUNT_ID が必要です（tower リポの Secrets を確認）")
            cf = Cloudflare(token, account)
            gh = GitHub(args.dry_run)
            if args.command != "permissions" and not args.dry_run and not os.environ.get("GH_TOKEN"):
                raise TowerError("GH_TOKEN が必要です（tower リポの Secrets に TOWER_GITHUB_TOKEN を入れてください）")
            if args.command == "apply":
                code = cmd_apply(args, cf, gh, rep)
            elif args.command == "watch":
                code = cmd_watch(args, cf, gh, rep)
            else:
                code = cmd_permissions(cf)
    except TowerError as e:
        rep.bad(str(e))
        code = 1
    rep.write(os.environ.get("GITHUB_STEP_SUMMARY"))
    if os.environ.get("TOWER_PROBLEMS_FILE"):
        with open(os.environ["TOWER_PROBLEMS_FILE"], "w", encoding="utf-8") as f:
            f.write("\n".join(rep.problems))
    return code


if __name__ == "__main__":
    sys.exit(main())
