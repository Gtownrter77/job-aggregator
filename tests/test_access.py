"""LAN access token for the phone app (aggregator/access.py + the gate in aggregator/web.py).

Offline; touches no database (only /healthz, /static and /phone are requested).
Run: python -m unittest discover -s tests -v   (or: python -m pytest tests)
"""
import os
import unittest
import warnings

from aggregator import access

warnings.filterwarnings("ignore", category=DeprecationWarning)

try:
    from starlette.testclient import TestClient

    from aggregator import web
except Exception:  # noqa: BLE001 - web deps missing: skip the HTTP tests
    web = None

PHONE = ("192.168.1.23", 50000)
LOCAL = ("127.0.0.1", 50000)
TOKEN = "abcd-efgh-jkmn-pqrs"
STATIC = "/static/htmx.min.js"


class Helpers(unittest.TestCase):
    def test_loopback(self):
        for h in ("127.0.0.1", "::1", "[::1]", "localhost", "::ffff:127.0.0.1", "127.0.0.5"):
            self.assertTrue(access.is_loopback(h), h)
        for h in ("192.168.1.5", "10.0.0.2", "0.0.0.0", "", None, "example.com", "testclient"):
            self.assertFalse(access.is_loopback(h), h)

    def test_new_token_shape(self):
        t = access.new_token()
        self.assertRegex(t, r"^[a-z2-9]{4}(-[a-z2-9]{4}){3}$")
        self.assertNotEqual(t, access.new_token())

    def test_check(self):
        self.assertTrue(access.check(None, []))
        self.assertTrue(access.check(TOKEN, [None, " " + TOKEN + " "]))
        self.assertFalse(access.check(TOKEN, [None, "", "nope", "ünïcode"]))

    def test_token_file_created_once_and_private(self):
        import tempfile
        from pathlib import Path
        old_file, old_env = access.TOKEN_FILE, {k: os.environ.pop(k, None) for k in ("AGGREGATOR_TOKEN", "AGGREGATOR_NO_TOKEN")}
        with tempfile.TemporaryDirectory() as d:
            access.TOKEN_FILE = str(Path(d) / "sub" / "access_token.txt")
            try:
                self.assertIsNone(access.resolve_token({}))
                t = access.create_token_file()
                self.assertEqual(access.create_token_file(), t)              # stable
                self.assertEqual(access.resolve_token({}), t)
                self.assertNotEqual(access.create_token_file(force=True), t)  # --new
                if os.name == "posix":
                    self.assertEqual(os.stat(access.TOKEN_FILE).st_mode & 0o777, 0o600)
            finally:
                access.TOKEN_FILE = old_file
                for k, v in old_env.items():
                    if v is not None:
                        os.environ[k] = v

    def test_resolve_order(self):
        old = {k: os.environ.pop(k, None) for k in ("AGGREGATOR_TOKEN", "AGGREGATOR_NO_TOKEN")}
        try:
            cfg = {"server": {"access_token": "from-config"}}
            self.assertEqual(access.resolve_token(cfg), "from-config")
            os.environ["AGGREGATOR_TOKEN"] = "from-env"
            self.assertEqual(access.resolve_token(cfg), "from-env")
            os.environ["AGGREGATOR_NO_TOKEN"] = "1"
            self.assertIsNone(access.resolve_token(cfg))
        finally:
            for k, v in old.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


@unittest.skipIf(web is None, "fastapi/starlette not installed")
class Gate(unittest.TestCase):
    def setUp(self):
        self._old = web.ACCESS_TOKEN
        web.ACCESS_TOKEN = TOKEN

    def tearDown(self):
        web.ACCESS_TOKEN = self._old

    def client(self, addr, **kw):
        return TestClient(web.app, client=addr, **kw)

    def test_local_needs_no_token(self):
        self.assertEqual(self.client(LOCAL).get(STATIC).status_code, 200)

    def test_phone_without_token_is_refused(self):
        c = self.client(PHONE)
        r = c.get(STATIC)
        self.assertEqual(r.status_code, 401)
        self.assertIn("access token", r.text)
        self.assertEqual(c.get("/api/stats").status_code, 401)
        self.assertEqual(c.post("/followups/x/approve").status_code, 401)
        self.assertEqual(c.get(STATIC, headers={"X-Access-Token": "wrong"}).status_code, 401)

    def test_healthz_is_open_and_says_token_required(self):
        r = self.client(PHONE).get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["token_required"])

    def test_header_bearer_and_cookie(self):
        c = self.client(PHONE)
        self.assertEqual(c.get(STATIC, headers={"X-Access-Token": TOKEN}).status_code, 200)
        self.assertEqual(c.cookies.get(access.COOKIE), TOKEN)  # header sets the cookie for htmx requests
        self.assertEqual(c.get(STATIC).status_code, 200)        # ... which now works on its own
        c2 = self.client(PHONE)
        self.assertEqual(c2.get(STATIC, headers={"Authorization": "Bearer " + TOKEN}).status_code, 200)
        c3 = self.client(PHONE, cookies={access.COOKIE: TOKEN})
        self.assertEqual(c3.get(STATIC).status_code, 200)

    def test_query_token_redirects_without_it(self):
        c = self.client(PHONE, follow_redirects=False)
        r = c.get(STATIC + "?token=" + TOKEN + "&v=2")
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], STATIC + "?v=2")
        self.assertIn(access.COOKIE + "=" + TOKEN, r.headers["set-cookie"])

    def test_no_token_configured_means_open(self):
        web.ACCESS_TOKEN = None
        self.assertEqual(self.client(PHONE).get(STATIC).status_code, 200)

    def test_phone_page_shows_token_only_locally(self):
        self.assertIn(TOKEN, self.client(LOCAL).get("/phone").text)
        r = self.client(PHONE).get("/phone", headers={"X-Access-Token": TOKEN})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(TOKEN, r.text)


if __name__ == "__main__":
    unittest.main()
