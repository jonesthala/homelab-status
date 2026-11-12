import http.client
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import homelab_status as hs

OVERVIEW = {
    "status": "online",
    "node": {
        "name": "pve",
        "cpu_percent": 12.345,
        "cpu_model": "Intel(R) Core(TM) i5-8250U CPU @ 1.60GHz",
        "cpu_threads": 8,
        "mem_total": 8054542336,
        "mem_percent": 43.5,
        "uptime_seconds": 71000,
        "pve_version": "pve-manager/9.2.26/41a0e2e09b9e6f9d",
        "kernel": "Linux 7.0.14-28-pve",
    },
    "storage": [{"name": "local-lvm"}],
    "cpu_history": [{"time": i, "cpu_percent": i / 10} for i in range(60)],
}
GUESTS = {
    "status": "online",
    "guests": [
        {"vmid": 107, "name": "jellyfin", "type": "lxc", "status": "running", "cpu_percent": 1.5,
         "mem_percent": 8.1, "uptime_seconds": 600, "ip": "192.168.0.207"},
        {"vmid": 101, "name": "CT101", "type": "lxc", "status": "running", "cpu_percent": 0.1,
         "mem_percent": 23.8, "uptime_seconds": 37394},
        {"vmid": 102, "name": "media", "type": "lxc", "status": "stopped", "cpu_percent": 5,
         "mem_percent": 5, "uptime_seconds": 99},
        {"vmid": 200, "name": "Win VM <script>", "type": "qemu", "status": "running"},
        {"vmid": "x", "name": "broken"},
        "not a dict",
    ],
}


def cfg(**overrides):
    return hs.Config(**overrides)


class SnapshotTests(unittest.TestCase):
    def test_only_whitelisted_fields(self):
        snap = hs.build_snapshot(OVERVIEW, GUESTS, cfg())
        text = json.dumps(snap)
        for secret in ("pve-manager", "Linux", "192.168", "local-lvm", "kernel", "<script>"):
            self.assertNotIn(secret, text)
        self.assertEqual(set(snap), {"host", "uptime_s", "cpu", "mem", "hw", "cpu_history", "guests", "running", "total"})
        self.assertEqual(set(snap["guests"][0]), {"id", "name", "kind", "status", "uptime_s", "cpu", "mem"})

    def test_values(self):
        snap = hs.build_snapshot(OVERVIEW, GUESTS, cfg())
        self.assertEqual(snap["cpu"], 12.3)
        self.assertEqual(snap["uptime_s"], 71000)
        self.assertEqual(snap["hw"], {"cpu": "i5-8250U", "threads": 8, "mem_gb": 8})
        self.assertEqual(len(snap["cpu_history"]), hs.HISTORY_POINTS)
        self.assertEqual([g["id"] for g in snap["guests"]], [101, 102, 107, 200])
        self.assertEqual(snap["running"], 3)
        self.assertEqual(snap["total"], 4)

    def test_stopped_guest_has_no_load(self):
        media = next(g for g in hs.build_snapshot(OVERVIEW, GUESTS, cfg())["guests"] if g["id"] == 102)
        self.assertEqual((media["status"], media["uptime_s"], media["cpu"], media["mem"]), ("stopped", 0, 0.0, 0.0))

    def test_names_are_cleaned_and_aliased(self):
        snap = hs.build_snapshot(OVERVIEW, GUESTS, cfg(aliases={"CT101": "lab box"}))
        names = {g["id"]: g["name"] for g in snap["guests"]}
        self.assertEqual(names[101], "lab-box")
        self.assertEqual(names[200], "win-vm-script")
        self.assertEqual(next(g["kind"] for g in snap["guests"] if g["id"] == 200), "vm")

    def test_hidden_ids(self):
        snap = hs.build_snapshot(OVERVIEW, GUESTS, cfg(hidden_ids=frozenset({101, 200})))
        self.assertEqual([g["id"] for g in snap["guests"]], [102, 107])

    def test_offline_upstream_raises(self):
        with self.assertRaises(hs.UpstreamError):
            hs.build_snapshot({"status": "offline"}, GUESTS, cfg())
        with self.assertRaises(hs.UpstreamError):
            hs.build_snapshot(OVERVIEW, {"status": "offline"}, cfg())
        with self.assertRaises(hs.UpstreamError):
            hs.build_snapshot({"status": "online"}, GUESTS, cfg())

    def test_helpers(self):
        self.assertEqual(hs.short_cpu("Intel(R) Core(TM) i5-10500T CPU @ 2.30GHz"), "i5-10500T")
        self.assertEqual(hs.short_cpu("AMD Ryzen 7 7735U with Radeon Graphics"), "Ryzen 7 7735U")
        self.assertEqual(hs.short_cpu("Intel(R) N150"), "N150")
        self.assertIsNone(hs.short_cpu(None))
        self.assertEqual(hs.percent("150"), 100.0)
        self.assertIsNone(hs.percent(float("nan")))
        self.assertEqual(hs.whole(-5), 0)
        self.assertEqual(hs.memory_gb(33_400_000_000), 32)
        self.assertEqual(hs.memory_gb(16_600_000_000), 16)
        self.assertIsNone(hs.memory_gb(0))
        self.assertEqual(hs.clean_name("---"), "guest")


class StateTests(unittest.TestCase):
    def test_fresh_and_stale(self):
        now = [1000.0]
        state = hs.State(clock=lambda: now[0])
        empty = state.view(30, "pve")
        self.assertEqual((empty["ok"], empty["updated"], empty["host"]), (False, None, "pve"))
        state.set({"host": "pve", "cpu": 1.0})
        self.assertTrue(state.view(30, "pve")["ok"])
        now[0] += 31
        stale = state.view(30, "pve")
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["cpu"], 1.0)
        self.assertEqual(stale["updated"], 1000)

    def test_meta_cannot_be_overridden_by_data(self):
        state = hs.State(clock=lambda: 5.0)
        state.set({"v": 99, "ok": "yes"})
        view = state.view(30, "pve")
        self.assertEqual((view["v"], view["ok"]), (hs.API_VERSION, True))


class ConfigTests(unittest.TestCase):
    def test_from_env(self):
        c = hs.Config.from_env({"PORT": "9000", "ALIASES": "CT101=lab, media = files", "HIDDEN_IDS": "101,102",
                                "ALLOWED_ORIGINS": "https://a.example", "UPSTREAM": "http://x:1/"})
        self.assertEqual(c.port, 9000)
        self.assertEqual(dict(c.aliases), {"CT101": "lab", "media": "files"})
        self.assertEqual(c.hidden_ids, frozenset({101, 102}))
        self.assertEqual(c.origins, ("https://a.example",))
        self.assertEqual(c.upstream, "http://x:1")

    def test_defaults(self):
        self.assertEqual(hs.Config.from_env({}), hs.Config())

    def test_bad_values(self):
        for env in ({"PORT": "abc"}, {"PORT": "70000"}, {"ALIASES": "nope"}, {"HIDDEN_IDS": "a"},
                    {"UPSTREAM": "ftp://x"}, {"POLL_SECONDS": "0"}, {"RATE_PER_IP": "-1"}):
            with self.assertRaises(ValueError, msg=env):
                hs.Config.from_env(env)


class RateLimitTests(unittest.TestCase):
    def test_burst_then_refill(self):
        now = [0.0]
        limiter = hs.RateLimiter(rate=1, burst=3, global_rate=100, global_burst=100, clock=lambda: now[0])
        self.assertEqual([limiter.allow("a") for _ in range(4)], [True, True, True, False])
        self.assertTrue(limiter.allow("b"))
        now[0] += 1
        self.assertTrue(limiter.allow("a"))
        self.assertFalse(limiter.allow("a"))

    def test_global_cap(self):
        limiter = hs.RateLimiter(rate=100, burst=100, global_rate=0.001, global_burst=2, clock=lambda: 0.0)
        self.assertEqual([limiter.allow(str(i)) for i in range(3)], [True, True, False])

    def test_key_table_is_bounded(self):
        limiter = hs.RateLimiter(rate=1, burst=2, global_rate=1e9, global_burst=1e9, clock=lambda: 0.0, max_keys=10)
        for i in range(50):
            limiter.allow(str(i))
        self.assertLessEqual(len(limiter._keys), 10)

    def test_client_key(self):
        self.assertEqual(hs.client_key("127.0.0.1", "203.0.113.9, 10.0.0.1"), "203.0.113.9")
        self.assertEqual(hs.client_key("127.0.0.1", "garbage"), "127.0.0.1")
        self.assertEqual(hs.client_key("198.51.100.4", "203.0.113.9"), "198.51.100.4")

    def test_allowed_origin(self):
        c = cfg()
        self.assertEqual(hs.allowed_origin("https://jonesthala.ch", c), "https://jonesthala.ch")
        self.assertEqual(hs.allowed_origin("http://localhost:3001", c), "http://localhost:3001")
        self.assertIsNone(hs.allowed_origin("https://evil.example", c))
        self.assertIsNone(hs.allowed_origin("http://localhost.evil.example", c))
        self.assertIsNone(hs.allowed_origin("http://localhost:3001", cfg(allow_local_origins=False)))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.state = hs.State()
        self.state.set(hs.build_snapshot(OVERVIEW, GUESTS, cfg()))
        self.cfg = cfg(port=0)
        self.server = hs.build_server(self.cfg, self.state,
                                      hs.RateLimiter(rate=0.001, burst=5, global_rate=1000, global_burst=1000))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp, json.loads(body) if body else None

    def test_status(self):
        resp, body = self.request("GET", "/v1/status", {"Origin": "https://jonesthala.ch"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), "https://jonesthala.ch")
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")
        self.assertTrue(body["ok"])
        self.assertEqual(body["running"], 3)

    def test_foreign_origin_gets_no_cors_header(self):
        resp, _ = self.request("GET", "/v1/status", {"Origin": "https://evil.example"})
        self.assertEqual(resp.status, 200)
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))

    def test_ping(self):
        resp, body = self.request("GET", "/v1/ping?t=1")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertTrue(body["pong"])

    def test_not_found_and_read_only(self):
        self.assertEqual(self.request("GET", "/api/guests")[0].status, 404)
        resp, body = self.request("POST", "/v1/status")
        self.assertEqual(resp.status, 405)
        self.assertEqual(body["error"], "read-only")
        self.assertEqual(self.request("DELETE", "/v1/status")[0].status, 405)

    def test_options_and_head(self):
        resp, _ = self.request("OPTIONS", "/v1/status", {"Origin": "http://localhost:3001"})
        self.assertEqual(resp.status, 204)
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), "http://localhost:3001")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("HEAD", "/v1/ping")
        head = conn.getresponse()
        self.assertEqual(head.status, 200)
        self.assertEqual(head.read(), b"")
        conn.close()

    def test_rate_limit_uses_forwarded_client(self):
        statuses = [self.request("GET", "/v1/ping", {"X-Forwarded-For": "203.0.113.7"})[0].status for _ in range(6)]
        self.assertEqual(statuses, [200] * 5 + [429])
        self.assertEqual(self.request("GET", "/v1/ping", {"X-Forwarded-For": "203.0.113.8"})[0].status, 200)


class FakeUpstream(BaseHTTPRequestHandler):
    routes = {"/api/overview": OVERVIEW, "/api/guests": GUESTS}

    def do_GET(self):  # noqa: N802
        body = json.dumps(self.routes.get(self.path, {})).encode()
        self.send_response(200 if self.path in self.routes else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class PollTests(unittest.TestCase):
    def test_poll_against_fake_dashboard(self):
        upstream = HTTPServer(("127.0.0.1", 0), FakeUpstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        try:
            c = cfg(upstream=f"http://127.0.0.1:{upstream.server_address[1]}")
            snap = hs.poll_once(c)
            self.assertEqual(snap["total"], 4)
        finally:
            upstream.shutdown()
            upstream.server_close()

    def test_poller_survives_errors(self):
        state, stop, calls = hs.State(), threading.Event(), []

        def source():
            calls.append(1)
            if len(calls) == 1:
                raise hs.UpstreamError("down")
            stop.set()
            return {"host": "pve"}

        thread = threading.Thread(target=hs.run_poller, args=(hs.Config(poll_seconds=1, stale_seconds=5), state, stop, source))
        thread.start()
        thread.join(timeout=5)
        self.assertEqual(len(calls), 2)
        self.assertTrue(state.view(30, "pve")["ok"])

    def test_demo_source(self):
        snap = hs.demo_source(cfg())()
        self.assertGreaterEqual(snap["total"], 8)


if __name__ == "__main__":
    unittest.main()
