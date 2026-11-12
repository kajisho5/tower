"""tower のテスト。偽の Cloudflare API と偽の gh コマンドを立てて、本物には一切触れない。"""
import datetime as dt
import importlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
import uuid
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))

ACCOUNT = "acc0000000000000000000000000test"
MASTER = "cfat_master_fake"

GROUPS = [
    {"id": "g-workers", "name": "Workers Scripts Write", "scopes": ["com.cloudflare.api.account"]},
    {"id": "g-acctset", "name": "Account Settings Read", "scopes": ["com.cloudflare.api.account"]},
    {"id": "g-kv", "name": "Workers KV Storage Write", "scopes": ["com.cloudflare.api.account"]},
    {"id": "g-d1", "name": "D1 Edit", "scopes": ["com.cloudflare.api.account"]},  # Edit 表記でも拾えるか
    {"id": "g-r2", "name": "Workers R2 Storage Write", "scopes": ["com.cloudflare.api.account"]},
    {"id": "g-dns", "name": "DNS Write", "scopes": ["com.cloudflare.api.account.zone"]},
    {"id": "g-zone", "name": "Zone Read", "scopes": ["com.cloudflare.api.account.zone"]},
    {"id": "g-routes", "name": "Workers Routes Write", "scopes": ["com.cloudflare.api.account.zone"]},
]


class FakeCF:
    def __init__(self):
        self.tokens = {"master": {"id": "master", "name": "tower master", "status": "active",
                                  "value": MASTER, "expires_on": None, "policies": []}}
        self.kv, self.d1, self.r2 = [], [], []
        self.zones = [{"id": "zone-fake", "name": "otter.example"}]
        self.dns = []
        self.calls = []


class Handler(BaseHTTPRequestHandler):
    cf: FakeCF = None

    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        b = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b)

    def _ok(self, result, info=None):
        p = {"success": True, "errors": [], "messages": [], "result": result}
        if info:
            p["result_info"] = info
        self._send(200, p)

    def _err(self, code, msg):
        self._send(code, {"success": False, "errors": [{"code": code, "message": msg}], "result": None})

    def _auth(self):
        tok = self.headers.get("Authorization", "").removeprefix("Bearer ")
        for t in self.cf.tokens.values():
            if t["value"] == tok:
                return t
        return None

    def _handle(self, method):
        cf = self.cf
        u = urlparse(self.path)
        path = u.path.removeprefix("/client/v4")
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
        who = self._auth()
        cf.calls.append((method, path, who["name"] if who else None))
        if not who:
            return self._err(401, "bad token")
        A = f"/accounts/{ACCOUNT}"
        is_master = who["id"] == "master"
        if path.startswith(A + "/tokens") and not is_master:
            return self._err(403, "only master manages tokens")
        if not path.startswith(A + "/tokens") and is_master:
            return self._err(403, "master has only token permissions")
        if path == A + "/tokens/permission_groups":
            return self._ok(GROUPS)
        if path == A + "/tokens/verify":
            return self._ok({"id": who["id"], "status": "active", "expires_on": who.get("expires_on")})
        if path == A + "/tokens" and method == "GET":
            items = [{k: v for k, v in t.items() if k != "value"} for t in cf.tokens.values()]
            per, page = int(q.get("per_page", 50)), int(q.get("page", 1))
            return self._ok(items[(page - 1) * per: page * per],
                            {"total_pages": max(1, -(-len(items) // per)), "page": page})
        if path == A + "/tokens" and method == "POST":
            tid = uuid.uuid4().hex
            t = {"id": tid, "name": body["name"], "status": "active", "value": "cfat_" + uuid.uuid4().hex,
                 "expires_on": body.get("expires_on"), "policies": body["policies"]}
            cf.tokens[tid] = t
            return self._ok(dict(t))
        m = re.fullmatch(A + r"/tokens/(\w+)", path)
        if m and method == "DELETE":
            cf.tokens.pop(m.group(1), None)
            return self._ok({"id": m.group(1)})
        # 一時トークンの権限を確かめる
        names = {g["id"] for p in who["policies"] for g in p["permission_groups"]}
        need = None
        if "/storage/kv" in path: need = "g-kv"
        elif "/d1/" in path: need = "g-d1"
        elif "/r2/" in path: need = "g-r2"
        elif "/dns_records" in path: need = "g-dns"
        elif path == "/zones": need = "g-zone"
        if need and need not in names:
            return self._err(403, f"missing {need}")
        if path == A + "/storage/kv/namespaces":
            if method == "GET":
                return self._ok(cf.kv, {"total_pages": 1})
            ns = {"id": uuid.uuid4().hex, "title": body["title"]}
            cf.kv.append(ns)
            return self._ok(ns)
        if path == A + "/d1/database":
            if method == "GET":
                return self._ok(cf.d1, {"total_pages": 1})
            db = {"uuid": uuid.uuid4().hex, "name": body["name"]}
            cf.d1.append(db)
            return self._ok(db)
        m = re.fullmatch(A + r"/r2/buckets/(.+)", path)
        if m:
            if m.group(1) in cf.r2:
                return self._ok({"name": m.group(1)})
            return self._err(404, "no bucket")
        if path == A + "/r2/buckets" and method == "POST":
            cf.r2.append(body["name"])
            return self._ok({"name": body["name"]})
        if path == "/zones":
            return self._ok([z for z in cf.zones if z["name"] == q.get("name")])
        m = re.fullmatch(r"/zones/([\w-]+)/dns_records(?:/(\w+))?", path)
        if m:
            if method == "GET":
                return self._ok([r for r in cf.dns if r["type"] == q["type"] and r["name"] == q["name"]])
            if method == "POST":
                r = dict(body, id=uuid.uuid4().hex)
                cf.dns.append(r)
                return self._ok(r)
            if method == "PUT":
                for r in cf.dns:
                    if r["id"] == m.group(2):
                        r.update(body)
                return self._ok({})
        return self._err(404, f"unknown {method} {path}")

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_PUT(self): self._handle("PUT")
    def do_DELETE(self): self._handle("DELETE")


FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
data = sys.stdin.read()
if os.environ.get("FAKE_GH_FAIL"):
    sys.stderr.write("HTTP 403: Resource not accessible\\n"); sys.exit(1)
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps({"args": sys.argv[1:], "stdin": data}) + "\\n")
"""


class TowerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        Handler.cf = self.cf = FakeCF()
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "gh"), "w") as f:
            f.write(FAKE_GH)
        os.chmod(os.path.join(bindir, "gh"), 0o755)
        self.ghlog = os.path.join(self.tmp, "gh.log")
        self.projdir = os.path.join(self.tmp, "projects")
        os.makedirs(self.projdir)
        self.env = {
            "CF_API_BASE": f"http://127.0.0.1:{self.srv.server_port}/client/v4",
            "CLOUDFLARE_API_TOKEN": MASTER, "CLOUDFLARE_ACCOUNT_ID": ACCOUNT,
            "GH_TOKEN": "fake", "TOWER_SKIP_GH_EXPIRY": "1",
            "FAKE_GH_LOG": self.ghlog, "TOWER_PROJECTS": os.path.join(self.projdir, "*.toml"),
            "PATH": bindir + os.pathsep + os.environ["PATH"],
            "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1",
        }
        self._old = {k: os.environ.get(k) for k in list(self.env) + ["FAKE_GH_FAIL", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]}
        os.environ.update(self.env)
        for k in ("FAKE_GH_FAIL", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ.pop(k, None)
        import tower
        self.t = importlib.reload(tower)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def project(self, name, text):
        with open(os.path.join(self.projdir, name), "w") as f:
            f.write(text)

    def run_tower(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = self.t.main(list(args))
        return code, buf.getvalue()

    def gh_calls(self):
        if not os.path.exists(self.ghlog):
            return []
        with open(self.ghlog) as f:
            return [json.loads(x) for x in f]

    def app_tokens(self):
        return [t for t in self.cf.tokens.values() if t["name"].startswith("tower:")]

    BASIC = '''
repo = "kajisho5/sample-otter-app"
permissions = ["workers", "kv", "d1"]
[[kv]]
title = "otter-cache"
[[d1]]
name = "otter-db"
[[r2]]
name = "otter-files"
[[dns]]
zone = "otter.example"
type = "CNAME"
name = "app"
content = "otter.example.workers.dev"
proxied = true
'''

    def test_sample_file_is_valid(self):
        root = os.path.dirname(HERE)
        os.environ["TOWER_PROJECTS"] = os.path.join(root, "projects", "_sample.toml")
        # 見本は _ 始まりなので読み飛ばされる。中身だけ別名で読ませて確かめる
        import shutil
        shutil.copy(os.path.join(root, "projects", "_sample.toml"), os.path.join(self.projdir, "sample.toml"))
        os.environ["TOWER_PROJECTS"] = os.path.join(self.projdir, "*.toml")
        code, out = self.run_tower("validate")
        self.assertEqual(code, 0, out)

    def test_validate_catches_mistakes(self):
        self.project("bad.toml", 'repo = "no-slash"\npermissions = ["dns", "wrokers"]\nexpires_days = 999\n')
        code, out = self.run_tower("validate")
        self.assertEqual(code, 1)
        self.assertIn("持ち主/リポ名", out)
        self.assertIn("zones", out)
        self.assertIn("wrokers", out)
        self.assertIn("1〜365", out)

    def test_apply_creates_everything_once(self):
        self.project("otter.toml", self.BASIC)
        code, out = self.run_tower("apply")
        self.assertEqual(code, 0, out)
        self.assertEqual([k["title"] for k in self.cf.kv], ["otter-cache"])
        self.assertEqual([d["name"] for d in self.cf.d1], ["otter-db"])
        self.assertEqual(self.cf.r2, ["otter-files"])
        self.assertEqual(len(self.cf.dns), 1)
        self.assertEqual(self.cf.dns[0]["name"], "app.otter.example")
        toks = self.app_tokens()
        self.assertEqual(len(toks), 1)
        ids = {g["id"] for p in toks[0]["policies"] for g in p["permission_groups"]}
        self.assertEqual(ids, {"g-workers", "g-acctset", "g-kv", "g-d1"})
        # 一時トークンは残っていない
        self.assertFalse([t for t in self.cf.tokens.values() if t["name"].startswith("tower-temp:")])
        calls = self.gh_calls()
        secrets = {c["args"][2]: c["stdin"] for c in calls if c["args"][:2] == ["secret", "set"]}
        self.assertEqual(secrets["CLOUDFLARE_API_TOKEN"], toks[0]["value"])
        self.assertEqual(secrets["CLOUDFLARE_ACCOUNT_ID"], ACCOUNT)
        vars_ = {c["args"][2] for c in calls if c["args"][:2] == ["variable", "set"]}
        self.assertEqual(vars_, {"CF_KV_OTTER_CACHE_ID", "CF_D1_OTTER_DB_ID"})
        # トークンの値がログに出ていない
        self.assertNotIn(toks[0]["value"], out)

        # 2回目は何も増えない
        before = toks[0]["id"]
        code, out = self.run_tower("apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.cf.kv), 1)
        self.assertEqual(len(self.cf.dns), 1)
        self.assertEqual([t["id"] for t in self.app_tokens()], [before])

    def test_permission_change_rotates(self):
        self.project("otter.toml", 'repo = "kajisho5/sample-otter-app"\npermissions = ["workers"]\n')
        self.run_tower("apply")
        old = self.app_tokens()[0]["id"]
        self.project("otter.toml", 'repo = "kajisho5/sample-otter-app"\npermissions = ["workers", "dns"]\nzones = ["otter.example"]\n')
        code, out = self.run_tower("apply")
        self.assertEqual(code, 0, out)
        toks = self.app_tokens()
        self.assertEqual(len(toks), 1)
        self.assertNotEqual(toks[0]["id"], old)
        zone_pol = [p for p in toks[0]["policies"] if "com.cloudflare.api.account.zone.zone-fake" in p["resources"]]
        self.assertEqual({g["id"] for g in zone_pol[0]["permission_groups"]}, {"g-dns", "g-zone"})
        self.assertIn("権限が変わった", out)

    def test_watch_renews_expiring_token(self):
        self.project("otter.toml", 'repo = "kajisho5/sample-otter-app"\npermissions = ["workers"]\n')
        self.run_tower("apply")
        tok = self.app_tokens()[0]
        soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3)
        tok["expires_on"] = soon.strftime("%Y-%m-%dT%H:%M:%SZ")
        code, out = self.run_tower("watch")
        self.assertEqual(code, 0, out)
        toks = self.app_tokens()
        self.assertEqual(len(toks), 1)
        self.assertNotEqual(toks[0]["id"], tok["id"])
        self.assertIn("期限が近い", out)
        self.assertIn("親トークンは有効", out)

    def test_watch_warns_when_master_expiring(self):
        self.cf.tokens["master"]["expires_on"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        problems = os.path.join(self.tmp, "p.md")
        os.environ["TOWER_PROBLEMS_FILE"] = problems
        try:
            code, out = self.run_tower("watch")
        finally:
            os.environ.pop("TOWER_PROBLEMS_FILE")
        self.assertEqual(code, 1)
        with open(problems) as f:
            self.assertIn("親トークンの期限", f.read())

    def test_dry_run_changes_nothing(self):
        self.project("otter.toml", self.BASIC)
        code, out = self.run_tower("apply", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(list(self.cf.tokens), ["master"])
        self.assertFalse(self.cf.kv or self.cf.d1 or self.cf.dns)
        self.assertEqual(self.gh_calls(), [])
        self.assertIn("[予定]", out)

    def test_github_failure_leaves_no_orphan_token(self):
        self.project("otter.toml", 'repo = "kajisho5/sample-otter-app"\npermissions = ["workers"]\n')
        os.environ["FAKE_GH_FAIL"] = "1"
        code, out = self.run_tower("apply")
        self.assertEqual(code, 1)
        self.assertEqual(self.app_tokens(), [])
        self.assertIn("gh secret set", out)

    def test_unknown_permission_name_is_reported(self):
        self.project("otter.toml", 'repo = "kajisho5/sample-otter-app"\npermissions = ["Imaginary Thing Write"]\n')
        code, out = self.run_tower("apply")
        self.assertEqual(code, 1)
        self.assertIn("見つからない権限", out)
        self.assertEqual(self.app_tokens(), [])

    def test_leftover_repo_token_is_reported_not_deleted(self):
        self.cf.tokens["old"] = {"id": "old", "name": "tower:kajisho5/gone-app:deadbeef", "status": "active",
                                 "value": "x", "expires_on": None, "policies": []}
        code, out = self.run_tower("apply")
        self.assertIn("old", self.cf.tokens)
        self.assertIn("設定にないリポ", out)

    def test_missing_secrets_message(self):
        os.environ.pop("CLOUDFLARE_API_TOKEN")
        code, out = self.run_tower("apply")
        self.assertEqual(code, 1)
        self.assertIn("CLOUDFLARE_API_TOKEN", out)


if __name__ == "__main__":
    unittest.main()
