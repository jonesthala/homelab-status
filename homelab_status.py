#!/usr/bin/env python3
"""homelab-status: a public, read-only status endpoint for my Proxmox homelab.

It polls the private homelab-dashboard API on the LAN, keeps a small sanitized
snapshot in memory and serves it as JSON:

    GET /v1/status   host uptime, CPU, RAM and containers (refreshed every few seconds)
    GET /v1/ping     tiny reply for round-trip measurements

Nothing in here can start, stop or change anything. Standard library only.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import math
import os
import random
import re
import signal
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

API_VERSION = 1
HISTORY_POINTS = 30
MAX_UPSTREAM_BYTES = 2_000_000
MEMORY_SIZES_GB = (2, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256)
LOCAL_ORIGIN = re.compile(r"^http://(localhost|127\.0\.0\.1|\[::1\])(:\d{1,5})?$")

log = logging.getLogger("homelab-status")


class UpstreamError(Exception):
    """The dashboard API did not deliver usable data."""


# ---------------------------------------------------------------- config


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


@dataclass(frozen=True)
class Config:
    upstream: str = "http://192.168.0.206:8000"
    bind: str = "127.0.0.1"
    port: int = 8080
    poll_seconds: float = 5.0
    stale_seconds: float = 30.0
    host_label: str = "pve"
    origins: tuple[str, ...] = (
        "https://jonesthala.ch",
        "https://www.jonesthala.ch",
        "https://jonesthala.github.io",
    )
    allow_local_origins: bool = True
    hidden_ids: frozenset[int] = frozenset()
    aliases: Mapping[str, str] = field(default_factory=dict)
    ip_rate: float = 3.0
    ip_burst: float = 30.0
    global_rate: float = 40.0
    global_burst: float = 80.0
    max_connections: int = 32

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Config":
        """Read settings from environment variables. Raises ValueError on bad input."""
        default = cls()

        def get(name: str, fallback: Any, conv: Callable[[str], Any] = str) -> Any:
            raw = env.get(name, "").strip()
            if not raw:
                return fallback
            try:
                return conv(raw)
            except ValueError as exc:
                raise ValueError(f"{name}: invalid value {raw!r}") from exc

        aliases: dict[str, str] = {}
        for pair in _csv(env.get("ALIASES", "")):
            name, sep, alias = pair.partition("=")
            if not sep or not name.strip() or not alias.strip():
                raise ValueError(f"ALIASES: expected name=alias, got {pair!r}")
            aliases[name.strip()] = alias.strip()

        try:
            hidden = frozenset(int(x) for x in _csv(env.get("HIDDEN_IDS", "")))
        except ValueError as exc:
            raise ValueError("HIDDEN_IDS: expected comma-separated VMIDs") from exc

        cfg = cls(
            upstream=get("UPSTREAM", default.upstream).rstrip("/"),
            bind=get("BIND", default.bind),
            port=get("PORT", default.port, int),
            poll_seconds=get("POLL_SECONDS", default.poll_seconds, float),
            stale_seconds=get("STALE_SECONDS", default.stale_seconds, float),
            host_label=get("HOST_LABEL", default.host_label),
            origins=tuple(_csv(env["ALLOWED_ORIGINS"])) if env.get("ALLOWED_ORIGINS", "").strip() else default.origins,
            allow_local_origins=get("ALLOW_LOCAL_ORIGINS", "1").lower() not in ("0", "false", "no"),
            hidden_ids=hidden,
            aliases=aliases,
            ip_rate=get("RATE_PER_IP", default.ip_rate, float),
            ip_burst=get("BURST_PER_IP", default.ip_burst, float),
            global_rate=get("RATE_GLOBAL", default.global_rate, float),
            global_burst=get("BURST_GLOBAL", default.global_burst, float),
            max_connections=get("MAX_CONNECTIONS", default.max_connections, int),
        )
        if not 1 <= cfg.port <= 65535:
            raise ValueError("PORT: must be 1-65535")
        if cfg.poll_seconds < 1 or cfg.stale_seconds < cfg.poll_seconds:
            raise ValueError("POLL_SECONDS must be >= 1 and STALE_SECONDS >= POLL_SECONDS")
        if min(cfg.ip_rate, cfg.ip_burst, cfg.global_rate, cfg.global_burst) <= 0 or cfg.max_connections < 1:
            raise ValueError("rate limits and MAX_CONNECTIONS must be positive")
        if not cfg.upstream.startswith(("http://", "https://")):
            raise ValueError("UPSTREAM: must start with http:// or https://")
        return cfg


# ---------------------------------------------------------------- snapshot

_NAME_BAD = re.compile(r"[^a-z0-9-]+")
_CPU_MODEL = re.compile(
    r"\b(i[3579]-\d{4,5}[A-Z]{0,2}|Ryzen \d+ (?:PRO )?\d{4}[A-Z]{0,2}|Core Ultra \d+ \d{3}[A-Z]?|N\d{2,3})\b"
)


def clean_name(name: object) -> str:
    """Lowercase, [a-z0-9-] only, at most 24 characters."""
    text = _NAME_BAD.sub("-", str(name).lower()).strip("-")
    return text[:24].rstrip("-") or "guest"


def short_cpu(model: object) -> str | None:
    """'Intel(R) Core(TM) i5-8250U CPU @ 1.60GHz' -> 'i5-8250U'."""
    if not isinstance(model, str) or not model.strip():
        return None
    match = _CPU_MODEL.search(model)
    if match:
        return match.group(1)
    text = re.sub(r"\((?:R|TM)\)|\bCPU\b|@.*$", "", model)
    text = re.sub(r"[^A-Za-z0-9 .-]", "", text)
    return " ".join(text.split())[:24] or None


def percent(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return round(min(100.0, max(0.0, number)), 1)


def whole(value: object) -> int:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    if math.isnan(number) or math.isinf(number):
        return 0
    return max(0, int(number))


def memory_gb(total_bytes: object) -> int | None:
    """Usable bytes -> the nominal module size people know (7.5 GiB -> 8)."""
    gib = whole(total_bytes) / 2**30
    if gib <= 0:
        return None
    for size in MEMORY_SIZES_GB:
        if gib >= size * 0.85 and gib <= size * 1.02:
            return size
    return round(gib)


def build_snapshot(overview: object, guests: object, cfg: Config) -> dict[str, Any]:
    """Turn the dashboard's API answers into the small public snapshot.

    Only whitelisted fields leave this function: no IPs, no kernel or Proxmox
    versions, no storage layout.
    """
    if not isinstance(overview, dict) or overview.get("status") != "online":
        raise UpstreamError("dashboard reports Proxmox offline (overview)")
    if not isinstance(guests, dict) or guests.get("status") != "online":
        raise UpstreamError("dashboard reports Proxmox offline (guests)")
    node = overview.get("node")
    if not isinstance(node, dict):
        raise UpstreamError("overview without node data")

    items: list[dict[str, Any]] = []
    raw_guests = guests.get("guests")
    for guest in raw_guests if isinstance(raw_guests, list) else []:
        if not isinstance(guest, dict):
            continue
        try:
            vmid = int(guest.get("vmid"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if vmid in cfg.hidden_ids:
            continue
        raw_name = str(guest.get("name") or "")
        running = guest.get("status") == "running"
        items.append({
            "id": vmid,
            "name": clean_name(cfg.aliases.get(raw_name, raw_name)),
            "kind": "vm" if guest.get("type") == "qemu" else "ct",
            "status": "running" if running else "stopped",
            "uptime_s": whole(guest.get("uptime_seconds")) if running else 0,
            "cpu": percent(guest.get("cpu_percent")) if running else 0.0,
            "mem": percent(guest.get("mem_percent")) if running else 0.0,
        })
    items.sort(key=lambda item: item["id"])

    raw_history = overview.get("cpu_history")
    history = [
        percent(point.get("cpu_percent"))
        for point in (raw_history if isinstance(raw_history, list) else [])[-HISTORY_POINTS:]
        if isinstance(point, dict)
    ]

    return {
        "host": cfg.host_label,
        "uptime_s": whole(node.get("uptime_seconds")),
        "cpu": percent(node.get("cpu_percent")),
        "mem": percent(node.get("mem_percent")),
        "hw": {
            "cpu": short_cpu(node.get("cpu_model")),
            "threads": whole(node.get("cpu_threads")) or None,
            "mem_gb": memory_gb(node.get("mem_total")),
        },
        "cpu_history": [h for h in history if h is not None],
        "guests": items,
        "running": sum(1 for item in items if item["status"] == "running"),
        "total": len(items),
    }


class State:
    """The latest snapshot, shared between the poller and the request threads."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None
        self._updated: float | None = None
        self._clock = clock

    def set(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._data = data
            self._updated = self._clock()

    def view(self, stale_seconds: float, host_label: str) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            data, updated = self._data, self._updated
        fresh = updated is not None and now - updated <= stale_seconds
        body: dict[str, Any] = dict(data) if data else {"host": host_label}
        body.update({
            "v": API_VERSION,
            "ok": fresh,
            "now": int(now),
            "updated": int(updated) if updated is not None else None,
        })
        return body


# ---------------------------------------------------------------- upstream


def fetch_json(url: str, timeout: float = 4.0) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "homelab-status"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (URL comes from config)
        raw = response.read(MAX_UPSTREAM_BYTES + 1)
    if len(raw) > MAX_UPSTREAM_BYTES:
        raise UpstreamError("upstream response too large")
    return json.loads(raw)


def poll_once(cfg: Config, fetch: Callable[[str], Any] = fetch_json) -> dict[str, Any]:
    return build_snapshot(fetch(cfg.upstream + "/api/overview"), fetch(cfg.upstream + "/api/guests"), cfg)


def run_poller(cfg: Config, state: State, stop: threading.Event, source: Callable[[], dict[str, Any]]) -> None:
    """Refresh the snapshot every poll_seconds. Logs only when the state flips."""
    healthy: bool | None = None
    while True:
        try:
            state.set(source())
            if healthy is not True:
                log.info("upstream ok")
                healthy = True
        except Exception as exc:  # noqa: BLE001 - keep polling whatever happens
            if healthy is not False:
                log.warning("upstream unavailable: %s", exc)
                healthy = False
        if stop.wait(cfg.poll_seconds):
            return


def demo_source(cfg: Config) -> Callable[[], dict[str, Any]]:
    """Generated data for local development (no network access)."""
    started = time.time() - 21 * 86400 - 3 * 3600
    names = {100: "pihole", 101: "ct101", 102: "media", 104: "tailscale", 105: "uptime-kuma",
             106: "dashboard", 107: "jellyfin", 108: "fileshare"}
    rng = random.Random()

    def source() -> dict[str, Any]:
        now = time.time()
        guests = dict(names)
        if int(now / 40) % 2:  # a Minecraft server comes and goes every 40 s
            guests[103] = "minecraft"
        overview = {
            "status": "online",
            "node": {
                "cpu_percent": 6 + 5 * math.sin(now / 25) + rng.random() * 4 + (25 if 103 in guests else 0),
                "mem_percent": 43 + rng.random() * 2 + (30 if 103 in guests else 0),
                "mem_total": 8054542336,
                "cpu_threads": 8,
                "cpu_model": "Intel(R) Core(TM) i5-8250U CPU @ 1.60GHz",
                "uptime_seconds": now - started,
            },
            "cpu_history": [{"cpu_percent": 6 + 5 * math.sin((now - 60 * i) / 25)} for i in range(HISTORY_POINTS, 0, -1)],
        }
        guest_list = {
            "status": "online",
            "guests": [
                {"vmid": vmid, "name": name, "type": "lxc", "status": "running",
                 "uptime_seconds": now - started - vmid * 600, "cpu_percent": rng.random() * 3,
                 "mem_percent": 10 + rng.random() * 30}
                for vmid, name in guests.items()
            ],
        }
        return build_snapshot(overview, guest_list, cfg)

    return source


# ---------------------------------------------------------------- rate limit


class _Bucket:
    __slots__ = ("tokens", "stamp")

    def __init__(self, tokens: float, stamp: float) -> None:
        self.tokens = tokens
        self.stamp = stamp


class RateLimiter:
    """Token buckets: one per client plus one shared by everybody."""

    def __init__(self, rate: float, burst: float, global_rate: float, global_burst: float,
                 clock: Callable[[], float] = time.monotonic, max_keys: int = 4096) -> None:
        self.rate, self.burst = rate, burst
        self.global_rate, self.global_burst = global_rate, global_burst
        self._clock = clock
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._keys: dict[str, _Bucket] = {}
        self._global = _Bucket(global_burst, clock())

    @staticmethod
    def _take(bucket: _Bucket, rate: float, burst: float, now: float) -> bool:
        bucket.tokens = min(burst, bucket.tokens + (now - bucket.stamp) * rate)
        bucket.stamp = now
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return True
        return False

    def _prune(self, now: float) -> None:
        self._keys = {
            key: b for key, b in self._keys.items() if b.tokens + (now - b.stamp) * self.rate < self.burst
        }
        if len(self._keys) >= self._max_keys:
            self._keys.clear()

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            bucket = self._keys.get(key)
            if bucket is None:
                if len(self._keys) >= self._max_keys:
                    self._prune(now)
                bucket = self._keys[key] = _Bucket(self.burst, now)
            if not self._take(bucket, self.rate, self.burst, now):
                return False
            return self._take(self._global, self.global_rate, self.global_burst, now)


def client_key(peer_ip: str, forwarded_for: str | None) -> str:
    """Behind Tailscale Funnel every request comes from localhost; the visitor is in X-Forwarded-For."""
    if forwarded_for and peer_ip in ("127.0.0.1", "::1"):
        first = forwarded_for.split(",")[0].strip()
        try:
            return str(ipaddress.ip_address(first))
        except ValueError:
            pass
    return peer_ip


def allowed_origin(origin: str | None, cfg: Config) -> str | None:
    if not origin:
        return None
    if origin in cfg.origins:
        return origin
    if cfg.allow_local_origins and LOCAL_ORIGIN.match(origin):
        return origin
    return None


# ---------------------------------------------------------------- HTTP


def make_handler(cfg: Config, state: State, limiter: RateLimiter) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "homelab-status"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = 10

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            """No access log: visitors' addresses are not stored."""

        def _send(self, status: int, body: dict[str, Any], cache: str = "no-store",
                  head: bool = False, extra: Mapping[str, str] | None = None) -> None:
            data = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "cross-origin")
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self._cors()
            self.end_headers()
            if not head:
                self.wfile.write(data)

        def _cors(self) -> None:
            origin = allowed_origin(self.headers.get("Origin"), cfg)
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

        def _limited(self, head: bool) -> bool:
            key = client_key(self.client_address[0], self.headers.get("X-Forwarded-For"))
            if limiter.allow(key):
                return False
            self._send(429, {"v": API_VERSION, "error": "too many requests"}, head=head, extra={"Retry-After": "2"})
            return True

        def _route(self, head: bool) -> None:
            path = urlsplit(self.path).path
            if self._limited(head):
                return
            if path == "/v1/status":
                self._send(200, state.view(cfg.stale_seconds, cfg.host_label), cache="no-cache", head=head)
            elif path == "/v1/ping":
                self._send(200, {"v": API_VERSION, "pong": True, "host": cfg.host_label,
                                 "now_ms": int(time.time() * 1000)}, head=head)
            elif path == "/":
                self._send(200, {"service": "homelab-status", "read_only": True,
                                 "endpoints": ["/v1/status", "/v1/ping"]}, cache="max-age=300", head=head)
            else:
                self._send(404, {"v": API_VERSION, "error": "not found"}, head=head)

        def do_GET(self) -> None:  # noqa: N802
            self._route(head=False)

        def do_HEAD(self) -> None:  # noqa: N802
            self._route(head=True)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self._cors()
            self.end_headers()

        def _not_allowed(self) -> None:
            self.close_connection = True
            self._send(405, {"v": API_VERSION, "error": "read-only"}, extra={"Allow": "GET, HEAD, OPTIONS"})

        do_POST = do_PUT = do_PATCH = do_DELETE = _not_allowed  # noqa: N815

    return Handler


class StatusServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a cap on simultaneous connections."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], max_connections: int) -> None:
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        log.debug("request failed", exc_info=True)


def build_server(cfg: Config, state: State, limiter: RateLimiter | None = None) -> StatusServer:
    limiter = limiter or RateLimiter(cfg.ip_rate, cfg.ip_burst, cfg.global_rate, cfg.global_burst)
    return StatusServer((cfg.bind, cfg.port), make_handler(cfg, state, limiter), cfg.max_connections)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Public, read-only status endpoint for a Proxmox homelab.")
    parser.add_argument("--demo", action="store_true", help="serve generated data instead of polling the dashboard")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), format="%(levelname)s %(message)s")

    try:
        cfg = Config.from_env()
    except ValueError as exc:
        log.error("config: %s", exc)
        return 2

    state, stop = State(), threading.Event()
    source = demo_source(cfg) if args.demo else (lambda: poll_once(cfg))
    threading.Thread(target=run_poller, args=(cfg, state, stop, source), daemon=True, name="poller").start()
    server = build_server(cfg, state)

    def on_signal(signum: int, frame: Any) -> None:
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    log.info("listening on %s:%d, source: %s", cfg.bind, cfg.port, "demo" if args.demo else cfg.upstream)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
