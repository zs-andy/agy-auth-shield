#!/usr/bin/env python3
"""agy-auth-shield: Local Cloud Code HTTP/1.1 gateway daemon for Antigravity CLI & T3 Code.

Solves mid-turn 401 UNAUTHENTICATED aborts caused by `agy` capturing a short-lived
(60-minute) OAuth2 access token per turn without retrying after hourly refresh.

Security & boundary guarantees:
  - Binds strictly to 127.0.0.1.
  - Keeps observed Bearer tokens strictly in RAM (never written to disk or logs).
  - Never accesses macOS Keychain or protected T3 Code profile files directly;
    on-demand refreshes delegate exclusively to the official `agy` CLI over
    documented interfaces (`agy models` / `agy acp` initialize+authenticate).
  - Control endpoints require a per-install 0600 random secret token.
"""

from __future__ import annotations

import base64
from collections import OrderedDict
from dataclasses import asdict, dataclass
import gzip
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import secrets
import select
import shutil
import signal
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse


def _load_version() -> str:
    here = Path(__file__).resolve().parent
    for candidate in (here / "VERSION", here.parent / "VERSION"):
        try:
            if candidate.is_file():
                val = candidate.read_text(encoding="utf-8").strip()
                if val:
                    return val
        except OSError:
            pass
    return "1.2.0"


VERSION = _load_version()
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18765
DEFAULT_UPSTREAM = "https://daily-cloudcode-pa.googleapis.com"

BASE_DIR = Path(
    os.environ.get("AGY_AUTH_SHIELD_HOME", Path.home() / ".local" / "share" / "agy-auth-shield")
)
RUN_DIR = BASE_DIR / "run"
TOKEN_FILE = RUN_DIR / "control.token"
PID_FILE = RUN_DIR / "daemon.pid"
SCOPES_FILE = RUN_DIR / "scopes.json"
LOG_FILE = BASE_DIR / "shield.log"

MAX_REQUEST_BODY_BYTES = 64 * 1024 * 1024  # 64 MiB
CONNECT_TIMEOUT_SEC = 30.0
WRITE_STALL_TIMEOUT_SEC = 30.0  # per-chunk upload stall timeout (prevents 10-min frozen writes)
READ_TIMEOUT_SEC = 600.0  # 10 min for extended thinking / large prefill SSE pauses
MAX_RETRY_ELAPSED_SEC = 120.0  # never stack retries past 2 min of elapsed time
MAX_TRANSIENT_RETRIES = 2  # up to 3 total attempts for fast connect/TLS/send resets
UPLOAD_CHUNK_BYTES = 65536  # 64 KiB upload chunks for stall & disconnect checks
GZIP_MIN_BODY_BYTES = 16384  # transparently gzip uncompressed JSON bodies >= 16 KiB
SLOW_LOG_SEND_MS = 5000
SLOW_LOG_REQ_BYTES = 512 * 1024
PROBE_TIMEOUT_SEC = 18.0
PROBE_FAIL_COOLDOWN_SEC = 20.0
VALID_TOKEN_TTL_SEC = 3300.0  # 55 min (< Google's 60-min OAuth2 lifetime)
DEAD_TOKEN_TTL_SEC = 7200.0
MAX_TRACKED_TOKENS = 64
MAX_VALID_TOKENS = 8


class _ClientDisconnected(Exception):
    """Raised when the local client closed its socket while upstream work is in flight."""


def _configure_upstream_socket(sock: socket.socket, timeout_sec: float) -> None:
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPALIVE"):  # macOS
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 30)
        elif hasattr(socket, "TCP_KEEPIDLE"):  # Linux
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
        if hasattr(socket, "TCP_KEEPINTVL"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
        if hasattr(socket, "TCP_KEEPCNT"):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
    except OSError:
        pass
    sock.settimeout(timeout_sec)

HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


def _setup_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(log_path.parent, 0o700)
    except OSError:
        pass
    logger = logging.getLogger("agy-auth-shield")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            str(log_path), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S%z")
        )
        logger.addHandler(handler)
        try:
            os.chmod(log_path, 0o600)
        except OSError:
            pass
    return logger


def _ensure_control_token(token_path: Path) -> str:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(token_path.parent, 0o700)
    except OSError:
        pass
    if token_path.exists():
        try:
            existing = token_path.read_text(encoding="utf-8").strip()
            if len(existing) >= 32:
                os.chmod(token_path, 0o600)
                return existing
        except OSError:
            pass
    token = secrets.token_hex(32)
    fd = os.open(str(token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (token + "\n").encode("ascii"))
    finally:
        os.close(fd)
    return token


def _build_ssl_context() -> ssl.SSLContext:
    candidates: List[str] = []
    env_ca = os.environ.get("SSL_CERT_FILE", "").strip()
    if env_ca:
        candidates.append(env_ca)
    candidates.extend(
        [
            "/etc/ssl/cert.pem",  # macOS / Alpine / FreeBSD
            "/etc/ssl/certs/ca-certificates.crt",  # Debian / Ubuntu / Arch
            "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL / Fedora / Rocky
            "/etc/ssl/ca-bundle.pem",  # openSUSE
        ]
    )
    try:
        import certifi  # type: ignore

        candidates.append(certifi.where())
    except Exception:
        pass
    for ca_path in candidates:
        if ca_path and os.path.isfile(ca_path):
            try:
                ctx = ssl.create_default_context(cafile=ca_path)
                ctx.check_hostname = True
                ctx.verify_mode = ssl.CERT_REQUIRED
                return ctx
            except Exception:
                continue
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _is_valid_agy_binary(path_str: str) -> bool:
    if not path_str:
        return False
    p = Path(path_str)
    if not p.is_absolute():
        return False
    name = p.name.lower()
    if name not in ("agy", "antigravity"):
        return False
    return p.is_file() and os.access(str(p), os.X_OK)


def _default_agy_binary() -> Optional[str]:
    candidates = [
        str(Path.home() / ".local" / "bin" / "agy"),
        "/opt/homebrew/bin/agy",
        "/usr/local/bin/agy",
        "/usr/bin/agy",
    ]
    for cand in candidates:
        if _is_valid_agy_binary(cand):
            return cand
    return None


def _host_matches_no_proxy(host: str, no_proxy_val: str) -> bool:
    if not no_proxy_val:
        return False
    host_l = host.lower().strip("[]")
    for raw in no_proxy_val.split(","):
        entry = raw.strip().lower()
        if not entry:
            continue
        if entry == "*":
            return True
        if ":" in entry and not entry.startswith("["):
            entry = entry.split(":", 1)[0]
        if entry.startswith("."):
            if host_l.endswith(entry) or host_l == entry[1:]:
                return True
        elif host_l == entry or host_l.endswith("." + entry):
            return True
    return False


def _resolve_upstream_proxy(upstream_scheme: str, upstream_host: str) -> Optional[str]:
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    if _host_matches_no_proxy(upstream_host, no_proxy):
        return None
    keys = (
        ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
        if upstream_scheme == "https"
        else ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
    )
    for key in keys:
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return None


@dataclass(frozen=True)
class ProbeSpec:
    """Non-secret scope registration metadata used for on-demand `agy` probes."""

    mode: str  # "cli" or "acp"
    real_agy: str
    gemini_dir: str = ""
    app_data_dir: str = ""
    gemini_home: str = ""
    ssh_connection: str = ""
    force_file_storage: bool = False


@dataclass
class _ValidTokenEntry:
    seq: int
    first_seen: float
    last_ok: float
    expires_at: float
    ok_seq: int


class ScopeState:
    """In-memory per-account/per-profile OAuth token state (tokens are never persisted)."""

    def __init__(self, scope_id: str, valid_token_ttl_sec: float = VALID_TOKEN_TTL_SEC) -> None:
        self.scope_id = scope_id
        self.valid_token_ttl_sec = valid_token_ttl_sec
        self.lock = threading.Lock()
        self.probe_lock = threading.Lock()
        self.token_cond = threading.Condition(self.lock)

        self.token_seq: "OrderedDict[str, int]" = OrderedDict()
        self.token_first_seen: "OrderedDict[str, float]" = OrderedDict()
        self._next_seq = 0
        self._next_ok_seq = 0
        self.valid_tokens: "OrderedDict[str, _ValidTokenEntry]" = OrderedDict()
        # token -> marked_dead_mono
        self.dead_tokens: "OrderedDict[str, float]" = OrderedDict()

        self.probe_spec: Optional[ProbeSpec] = None
        self.last_probe_ok_at: float = 0.0
        self.last_probe_failed_at: float = 0.0
        self.last_ok_at: float = 0.0

    def _prune_locked(self, now: float) -> None:
        while len(self.token_seq) > MAX_TRACKED_TOKENS:
            self.token_seq.popitem(last=False)
        while len(self.token_first_seen) > MAX_TRACKED_TOKENS:
            self.token_first_seen.popitem(last=False)
        expired_dead = [
            tok for tok, ts in self.dead_tokens.items() if now - ts > DEAD_TOKEN_TTL_SEC
        ]
        for tok in expired_dead:
            self.dead_tokens.pop(tok, None)
        while len(self.dead_tokens) > MAX_TRACKED_TOKENS:
            self.dead_tokens.popitem(last=False)
        expired_valid = [
            tok for tok, entry in self.valid_tokens.items() if now > entry.expires_at
        ]
        for tok in expired_valid:
            self.valid_tokens.pop(tok, None)
        while len(self.valid_tokens) > MAX_VALID_TOKENS:
            # Drop the token with the smallest sequence number (oldest).
            oldest = min(self.valid_tokens.items(), key=lambda kv: kv[1].seq)[0]
            self.valid_tokens.pop(oldest, None)

    def _seq_locked(self, token: str) -> int:
        seq = self.token_seq.get(token)
        if seq is None:
            self._next_seq += 1
            seq = self._next_seq
            self.token_seq[token] = seq
        else:
            self.token_seq.move_to_end(token)
        return seq

    def _best_valid_locked(
        self, exclude: Optional[Set[str]] = None, min_ok_seq: int = 0
    ) -> Optional[str]:
        best_tok: Optional[str] = None
        best_key: Tuple[int, float] = (-1, -1.0)
        for tok, entry in self.valid_tokens.items():
            if tok in self.dead_tokens:
                continue
            if exclude and tok in exclude:
                continue
            if min_ok_seq > 0 and entry.ok_seq <= min_ok_seq:
                continue
            key = (entry.seq, entry.last_ok)
            if key > best_key:
                best_key = key
                best_tok = tok
        return best_tok

    def current_ok_seq(self) -> int:
        with self.lock:
            return self._next_ok_seq

    def select_token_for_request(self, req_token: str) -> Tuple[str, bool]:
        """Return (token_to_send, was_proactively_swapped)."""
        now = time.monotonic()
        with self.lock:
            self._prune_locked(now)
            req_seq = self._seq_locked(req_token)
            best_tok = self._best_valid_locked()
            if best_tok is None or best_tok == req_token:
                return req_token, False
            if req_token in self.dead_tokens:
                return best_tok, True
            best_seq = self.valid_tokens[best_tok].seq
            if req_seq < best_seq:
                return best_tok, True
            return req_token, False

    def record_ok(self, token: str, from_probe: bool = False) -> None:
        now = time.monotonic()
        with self.lock:
            seq = self._seq_locked(token)
            first_seen = self.token_first_seen.get(token)
            if first_seen is None:
                first_seen = now
                self.token_first_seen[token] = first_seen
            else:
                self.token_first_seen.move_to_end(token)
            self.dead_tokens.pop(token, None)
            expires_at = first_seen + self.valid_token_ttl_sec
            if from_probe:
                # Probe just confirmed this token with upstream right now; allow
                # immediate capture with a brief grace window even if near TTL.
                expires_at = max(expires_at, now + min(self.valid_token_ttl_sec, 60.0))
            self._next_ok_seq += 1
            if now <= expires_at:
                self.valid_tokens[token] = _ValidTokenEntry(
                    seq=seq,
                    first_seen=first_seen,
                    last_ok=now,
                    expires_at=expires_at,
                    ok_seq=self._next_ok_seq,
                )
                self.valid_tokens.move_to_end(token)
            else:
                self.valid_tokens.pop(token, None)
            self.last_ok_at = now
            self._prune_locked(now)
            self.token_cond.notify_all()

    def record_dead(self, token: str) -> None:
        now = time.monotonic()
        with self.lock:
            self._seq_locked(token)
            dead_entry = self.valid_tokens.pop(token, None)
            self.dead_tokens[token] = now
            self.dead_tokens.move_to_end(token)
            if dead_entry is not None:
                # When a previously valid token expires, evict strictly older
                # cached tokens whose last success predates this token's first sight.
                stale_older = [
                    tok
                    for tok, entry in self.valid_tokens.items()
                    if entry.seq < dead_entry.seq and entry.last_ok < dead_entry.first_seen
                ]
                for tok in stale_older:
                    self.valid_tokens.pop(tok, None)
            self._prune_locked(now)

    def get_fresh_token(self, exclude: Set[str], min_ok_seq: int = 0) -> Optional[str]:
        now = time.monotonic()
        with self.lock:
            self._prune_locked(now)
            return self._best_valid_locked(exclude=exclude, min_ok_seq=min_ok_seq)

    def reset_tokens(self) -> None:
        with self.lock:
            self.valid_tokens.clear()
            self.dead_tokens.clear()
            self.token_first_seen.clear()
            self.last_probe_failed_at = 0.0

    def status_summary(self, now: float) -> Dict[str, object]:
        with self.lock:
            self._prune_locked(now)
            best_tok = self._best_valid_locked()
            token_age: Optional[float] = None
            if best_tok is not None:
                token_age = round(now - self.valid_tokens[best_tok].first_seen, 1)
            return {
                "mode": self.probe_spec.mode if self.probe_spec else "unregistered",
                "has_valid_token": best_tok is not None,
                "valid_tokens_count": len(self.valid_tokens),
                "dead_tokens_count": len(self.dead_tokens),
                "token_age_sec": token_age,
                "last_ok_ago_sec": round(now - self.last_ok_at, 1) if self.last_ok_at else None,
                "last_probe_ok_ago_sec": (
                    round(now - self.last_probe_ok_at, 1) if self.last_probe_ok_at else None
                ),
            }


class ShieldEngine:
    def __init__(
        self,
        port: int,
        upstream_url: str,
        control_token: str,
        logger: logging.Logger,
        scopes_file: Optional[Path] = None,
    ) -> None:
        self.port = port
        self.upstream_url = upstream_url.rstrip("/")
        parsed = urlparse(self.upstream_url)
        self.upstream_scheme = parsed.scheme or "https"
        self.upstream_host = parsed.hostname or "daily-cloudcode-pa.googleapis.com"
        self.upstream_port = parsed.port or (443 if self.upstream_scheme == "https" else 80)
        self.upstream_base_path = parsed.path.rstrip("/")
        self.control_token = control_token
        self.logger = logger
        self.scopes_file = scopes_file
        self.ssl_ctx = _build_ssl_context()
        self.started_at = time.monotonic()

        self.write_timeout_sec = max(
            1.0,
            float(os.environ.get("AGY_AUTH_SHIELD_WRITE_TIMEOUT_SEC", str(WRITE_STALL_TIMEOUT_SEC))),
        )
        self.read_timeout_sec = max(
            1.0,
            float(os.environ.get("AGY_AUTH_SHIELD_READ_TIMEOUT_SEC", str(READ_TIMEOUT_SEC))),
        )
        self.valid_token_ttl_sec = max(
            0.05,
            float(os.environ.get("AGY_AUTH_SHIELD_VALID_TOKEN_TTL_SEC", str(VALID_TOKEN_TTL_SEC))),
        )
        self._gzip_lock = threading.Lock()
        self._gzip_enabled = os.environ.get("AGY_AUTH_SHIELD_GZIP_REQUESTS", "1").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )

        self._scopes_lock = threading.Lock()
        self._scopes: Dict[str, ScopeState] = {}
        self._persist_lock = threading.Lock()

        self._stats_lock = threading.Lock()
        self.stats: Dict[str, int] = {
            "requests_total": 0,
            "proactive_swaps": 0,
            "healed_401_cache": 0,
            "healed_401_probe": 0,
            "unhealed_401": 0,
            "probe_attempts": 0,
            "probe_successes": 0,
            "probe_failures": 0,
            "network_retries": 0,
            "gzip_compressed_requests": 0,
            "gzip_bytes_saved": 0,
            "client_aborts": 0,
            "write_timeouts": 0,
        }
        self._load_persisted_specs()

    def is_gzip_enabled(self) -> bool:
        with self._gzip_lock:
            return self._gzip_enabled

    def disable_gzip(self) -> None:
        with self._gzip_lock:
            self._gzip_enabled = False

    def _load_persisted_specs(self) -> None:
        """Restore non-secret ProbeSpecs across daemon restarts so active sessions can still heal."""
        if not self.scopes_file or not self.scopes_file.is_file():
            return
        try:
            raw = json.loads(self.scopes_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            restored = 0
            for scope_id, data in raw.items():
                if not isinstance(scope_id, str) or not isinstance(data, dict):
                    continue
                mode = str(data.get("mode", "")).strip()
                real_agy = str(data.get("real_agy", "")).strip()
                if mode not in ("cli", "acp") or not _is_valid_agy_binary(real_agy):
                    continue
                spec = ProbeSpec(
                    mode=mode,
                    real_agy=real_agy,
                    gemini_dir=str(data.get("gemini_dir", "") or ""),
                    app_data_dir=str(data.get("app_data_dir", "") or ""),
                    gemini_home=str(data.get("gemini_home", "") or ""),
                    ssh_connection=str(data.get("ssh_connection", "") or ""),
                    force_file_storage=bool(data.get("force_file_storage", False)),
                )
                st = self.get_scope(scope_id)
                with st.lock:
                    st.probe_spec = spec
                restored += 1
            if restored:
                self.logger.info("restored_scope_specs count=%d", restored)
        except Exception as exc:
            self.logger.warning("load_scope_specs_failed err=%s", type(exc).__name__)

    def _persist_specs(self) -> None:
        if not self.scopes_file:
            return
        with self._persist_lock:
            with self._scopes_lock:
                items = list(self._scopes.items())
            payload: Dict[str, Dict[str, object]] = {}
            for sid, st in items:
                with st.lock:
                    if st.probe_spec is not None:
                        payload[sid] = asdict(st.probe_spec)
            try:
                self.scopes_file.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_path = tempfile.mkstemp(
                    prefix=".scopes.", suffix=".tmp", dir=str(self.scopes_file.parent)
                )
                try:
                    os.chmod(tmp_path, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(payload, f, indent=2)
                        f.write("\n")
                    os.replace(tmp_path, str(self.scopes_file))
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
            except Exception as exc:
                self.logger.warning("persist_scope_specs_failed err=%s", type(exc).__name__)

    def inc_stat(self, key: str, delta: int = 1) -> None:
        with self._stats_lock:
            self.stats[key] = self.stats.get(key, 0) + delta

    def get_stats(self) -> Dict[str, int]:
        with self._stats_lock:
            return dict(self.stats)

    def get_scope(self, scope_id: str) -> ScopeState:
        with self._scopes_lock:
            st = self._scopes.get(scope_id)
            if st is None:
                st = ScopeState(scope_id, valid_token_ttl_sec=self.valid_token_ttl_sec)
                self._scopes[scope_id] = st
            return st

    def register_scope(self, scope_id: str, spec: ProbeSpec) -> None:
        st = self.get_scope(scope_id)
        changed = False
        with st.lock:
            if st.probe_spec != spec:
                st.probe_spec = spec
                changed = True
        if changed:
            self._persist_specs()
        self.logger.info(
            "register scope=%s mode=%s force_file=%s",
            scope_id,
            spec.mode,
            spec.force_file_storage,
        )

    def reset_scope(self, scope_id: str) -> None:
        st = self.get_scope(scope_id)
        st.reset_tokens()
        self.logger.info("reset scope=%s", scope_id)

    def health_payload(self) -> Dict[str, object]:
        with self._scopes_lock:
            scopes_count = len(self._scopes)
        return {
            "ok": True,
            "version": VERSION,
            "pid": os.getpid(),
            "port": self.port,
            "uptime_sec": round(time.monotonic() - self.started_at, 1),
            "scopes_count": scopes_count,
            "stats": self.get_stats(),
        }

    def status_payload(self) -> Dict[str, object]:
        now = time.monotonic()
        with self._scopes_lock:
            scope_items = list(self._scopes.items())
        return {
            **self.health_payload(),
            "platform": platform.system().lower(),
            "python": sys.version.split()[0],
            "openssl": ssl.OPENSSL_VERSION,
            "upstream": f"{self.upstream_scheme}://{self.upstream_host}:{self.upstream_port}",
            "scopes": {sid: st.status_summary(now) for sid, st in scope_items},
        }

    def open_upstream_connection(self) -> http.client.HTTPConnection:
        proxy_url = _resolve_upstream_proxy(self.upstream_scheme, self.upstream_host)
        if proxy_url:
            p = urlparse(proxy_url)
            if p.scheme in ("http", "https") and p.hostname:
                p_port = p.port or (443 if p.scheme == "https" else 8080)
                if self.upstream_scheme == "https":
                    conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                        p.hostname,
                        p_port,
                        timeout=CONNECT_TIMEOUT_SEC,
                        context=self.ssl_ctx,
                    )
                    tunnel_headers: Dict[str, str] = {}
                    if p.username is not None:
                        creds = f"{p.username}:{p.password or ''}".encode("utf-8")
                        tunnel_headers["Proxy-Authorization"] = (
                            "Basic " + base64.b64encode(creds).decode("ascii")
                        )
                    conn.set_tunnel(
                        self.upstream_host,
                        self.upstream_port,
                        headers=tunnel_headers or None,
                    )
                else:
                    conn = http.client.HTTPConnection(
                        p.hostname, p_port, timeout=CONNECT_TIMEOUT_SEC
                    )
                conn.connect()
                if conn.sock is not None:
                    _configure_upstream_socket(conn.sock, self.write_timeout_sec)
                return conn
            else:
                self.logger.warning(
                    "unsupported_proxy_scheme scheme=%s falling_back_direct", p.scheme
                )

        if self.upstream_scheme == "https":
            conn = http.client.HTTPSConnection(
                self.upstream_host,
                self.upstream_port,
                timeout=CONNECT_TIMEOUT_SEC,
                context=self.ssl_ctx,
            )
        else:
            conn = http.client.HTTPConnection(
                self.upstream_host,
                self.upstream_port,
                timeout=CONNECT_TIMEOUT_SEC,
            )
        conn.connect()
        if conn.sock is not None:
            _configure_upstream_socket(conn.sock, self.write_timeout_sec)
        return conn

    def _build_probe_env(self, spec: ProbeSpec, probe_url: str, tmpdir: str) -> Dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("AGY_AUTH_SHIELD_")
        }
        env["CLOUD_CODE_URL"] = probe_url
        env["GODEBUG"] = "http2client=0"
        existing_no_proxy = env.get("NO_PROXY") or env.get("no_proxy") or ""
        parts = [p.strip() for p in existing_no_proxy.split(",") if p.strip()]
        for loopback in ("127.0.0.1", "localhost"):
            if loopback not in parts:
                parts.append(loopback)
        no_proxy_str = ",".join(parts)
        env["NO_PROXY"] = no_proxy_str
        env["no_proxy"] = no_proxy_str
        env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "1"
        env["BROWSER"] = shutil.which("false") or "/usr/bin/false"
        env["TMPDIR"] = tmpdir
        env.pop("DISPLAY", None)

        if spec.gemini_home:
            env["GEMINI_HOME"] = spec.gemini_home
        else:
            env.pop("GEMINI_HOME", None)

        if spec.ssh_connection:
            env["SSH_CONNECTION"] = spec.ssh_connection
        else:
            env.pop("SSH_CONNECTION", None)

        if spec.force_file_storage:
            env["AGY_ACP_FORCE_FILE_STORAGE"] = "1"
        else:
            env.pop("AGY_ACP_FORCE_FILE_STORAGE", None)

        return env

    def _run_probe_subprocess(
        self,
        scope: ScopeState,
        spec: ProbeSpec,
        failed_tokens: Set[str],
        min_ok_seq: int = 0,
    ) -> Optional[str]:
        probe_url = f"http://{DEFAULT_HOST}:{self.port}/probe/{scope.scope_id}"
        tmpdir = tempfile.mkdtemp(prefix="agy-shield-probe-")
        proc: Optional[subprocess.Popen[bytes]] = None
        try:
            env = self._build_probe_env(spec, probe_url, tmpdir)
            if spec.mode == "acp":
                cmd = [spec.real_agy, "acp"]
                if spec.gemini_dir:
                    cmd.extend(["--gemini-dir", spec.gemini_dir])
                if spec.app_data_dir:
                    cmd.extend(["--app-data-dir", spec.app_data_dir])
                cmd.append(f"--cloud_code_endpoint={probe_url}")
            else:
                cmd = [spec.real_agy, "models"]

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if spec.mode == "acp" else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            init_done = threading.Event()
            proc_exited = threading.Event()

            def _drain_stderr(stream) -> None:
                try:
                    while stream.read(4096):
                        pass
                except Exception:
                    pass

            def _drain_stdout(stream) -> None:
                try:
                    for raw_line in stream:
                        if spec.mode == "acp" and not init_done.is_set():
                            try:
                                msg = json.loads(raw_line.decode("utf-8", errors="ignore"))
                                if isinstance(msg, dict) and msg.get("id") == 1:
                                    init_done.set()
                            except Exception:
                                pass
                except Exception:
                    pass

            def _wait_proc() -> None:
                try:
                    proc.wait()
                finally:
                    proc_exited.set()
                    with scope.lock:
                        scope.token_cond.notify_all()

            threading.Thread(target=_drain_stderr, args=(proc.stderr,), daemon=True).start()
            threading.Thread(target=_drain_stdout, args=(proc.stdout,), daemon=True).start()
            threading.Thread(target=_wait_proc, daemon=True).start()

            if spec.mode == "acp" and proc.stdin is not None:

                def _drive_acp() -> None:
                    try:
                        init_req = {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {
                                "protocolVersion": 1,
                                "clientInfo": {"name": "agy-auth-shield", "version": VERSION},
                                "clientCapabilities": {},
                            },
                        }
                        proc.stdin.write((json.dumps(init_req) + "\n").encode("utf-8"))
                        proc.stdin.flush()
                        init_done.wait(timeout=3.0)
                        if proc.poll() is not None:
                            return
                        auth_req = {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "authenticate",
                            "params": {"methodId": "oauth-personal"},
                        }
                        proc.stdin.write((json.dumps(auth_req) + "\n").encode("utf-8"))
                        proc.stdin.flush()
                    except Exception:
                        pass

                threading.Thread(target=_drive_acp, daemon=True).start()

            deadline = time.monotonic() + PROBE_TIMEOUT_SEC
            with scope.lock:
                while True:
                    scope._prune_locked(time.monotonic())
                    fresh = scope._best_valid_locked(exclude=failed_tokens, min_ok_seq=min_ok_seq)
                    if fresh is not None:
                        return fresh
                    if proc_exited.is_set():
                        return scope._best_valid_locked(
                            exclude=failed_tokens, min_ok_seq=min_ok_seq
                        )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    scope.token_cond.wait(timeout=min(remaining, 0.5))
        except Exception as exc:
            self.logger.warning(
                "probe_error scope=%s mode=%s err=%s",
                scope.scope_id,
                spec.mode,
                type(exc).__name__,
            )
            return None
        finally:
            if proc is not None:
                try:
                    if proc.stdin is not None:
                        proc.stdin.close()
                except Exception:
                    pass
                if proc.poll() is None:
                    try:
                        proc.terminate()
                        proc.wait(timeout=2.0)
                    except Exception:
                        try:
                            proc.kill()
                            proc.wait(timeout=1.0)
                        except Exception:
                            pass
            shutil.rmtree(tmpdir, ignore_errors=True)

    def ensure_fresh_token(
        self, scope: ScopeState, failed_tokens: Set[str], min_ok_seq: int = 0
    ) -> Optional[str]:
        """Singleflight per-scope on-demand OAuth refresh via official `agy` probe."""
        with scope.probe_lock:
            existing = scope.get_fresh_token(exclude=failed_tokens, min_ok_seq=min_ok_seq)
            if existing is not None:
                return existing

            now = time.monotonic()
            with scope.lock:
                if now - scope.last_probe_failed_at < PROBE_FAIL_COOLDOWN_SEC:
                    self.logger.info("probe_cooldown_skip scope=%s", scope.scope_id)
                    return None
                spec = scope.probe_spec

            if spec is None:
                default_agy = _default_agy_binary()
                if scope.scope_id.startswith("cli-") and default_agy:
                    spec = ProbeSpec(mode="cli", real_agy=default_agy)
                else:
                    self.logger.warning("probe_no_spec scope=%s", scope.scope_id)
                    return None

            if not _is_valid_agy_binary(spec.real_agy):
                self.logger.warning("probe_invalid_binary scope=%s", scope.scope_id)
                return None

            self.inc_stat("probe_attempts")
            t0 = time.monotonic()
            self.logger.info("probe_start scope=%s mode=%s", scope.scope_id, spec.mode)
            fresh = self._run_probe_subprocess(scope, spec, failed_tokens, min_ok_seq=min_ok_seq)
            elapsed_ms = int((time.monotonic() - t0) * 1000)

            if fresh is not None:
                with scope.lock:
                    scope.last_probe_ok_at = time.monotonic()
                    scope.last_probe_failed_at = 0.0
                self.inc_stat("probe_successes")
                self.logger.info(
                    "probe_ok scope=%s mode=%s elapsed_ms=%d",
                    scope.scope_id,
                    spec.mode,
                    elapsed_ms,
                )
                return fresh

            with scope.lock:
                scope.last_probe_failed_at = time.monotonic()
            self.inc_stat("probe_failures")
            self.logger.warning(
                "probe_fail scope=%s mode=%s elapsed_ms=%d",
                scope.scope_id,
                spec.mode,
                elapsed_ms,
            )
            return None


def _parse_bearer(auth_header: Optional[str]) -> Optional[str]:
    if not auth_header:
        return None
    parts = auth_header.strip().split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1]:
        return parts[1].strip()
    return None


def _extract_hop_by_hop(headers_items: List[Tuple[str, str]]) -> Set[str]:
    hop = set(HOP_BY_HOP_HEADERS)
    for k, v in headers_items:
        if k.lower() == "connection":
            for token in v.split(","):
                t = token.strip().lower()
                if t:
                    hop.add(t)
    return hop


def _parse_route(raw_path: str) -> Tuple[str, str, bool]:
    """Return (scope_id, upstream_path, is_probe_request)."""
    if raw_path.startswith("/probe/"):
        rest = raw_path[len("/probe/") :]
        if "/" in rest:
            scope_id, tail = rest.split("/", 1)
            return scope_id or "default", "/" + tail, True
        return rest or "default", "/", True
    if raw_path.startswith("/scope/"):
        rest = raw_path[len("/scope/") :]
        if "/" in rest:
            scope_id, tail = rest.split("/", 1)
            return scope_id or "default", "/" + tail, False
        return rest or "default", "/", False
    return "default", raw_path, False


class ShieldHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(
        self,
        server_address: Tuple[str, int],
        engine: ShieldEngine,
    ) -> None:
        self.engine = engine
        super().__init__(server_address, ShieldRequestHandler)

    def server_bind(self) -> None:
        # Skip HTTPServer.server_bind()'s socket.getfqdn() reverse-DNS lookup,
        # which can stall daemon startup for 15-30s on macOS/VPN/offline networks.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def process_request(self, request, client_address) -> None:
        try:
            request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        super().process_request(request, client_address)


class ShieldRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ShieldHTTPServer

    def log_message(self, format: str, *args) -> None:
        # Suppress default stderr logging (never leak paths/headers to stderr).
        return

    def _send_json(self, status: int, payload: Dict[str, object]) -> None:
        body = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    def _check_control_auth(self) -> bool:
        got = self.headers.get("X-Shield-Token", "")
        if not got or not secrets.compare_digest(got, self.server.engine.control_token):
            self._send_json(403, {"ok": False, "error": "forbidden"})
            return False
        return True

    def _read_request_body(self) -> Optional[bytes]:
        if self.headers.get("Expect", "").strip().lower() == "100-continue":
            self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            self.wfile.flush()

        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks: List[bytes] = []
            total = 0
            while True:
                line = self.rfile.readline(65536)
                if not line:
                    raise ValueError("unexpected EOF in chunked body")
                size_str = line.split(b";", 1)[0].strip()
                if not size_str:
                    raise ValueError("empty chunk size line")
                size = int(size_str, 16)
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(65536)
                        if not trailer or trailer in (b"\r\n", b"\n"):
                            break
                    break
                total += size
                if total > MAX_REQUEST_BODY_BYTES:
                    return None
                data = self.rfile.read(size)
                if len(data) != size:
                    raise ValueError("truncated chunk")
                chunks.append(data)
                crlf = self.rfile.readline(4)
                if crlf not in (b"\r\n", b"\n"):
                    raise ValueError("missing chunk CRLF")
            return b"".join(chunks)

        cl_raw = self.headers.get("Content-Length")
        if not cl_raw:
            return b""
        length = int(cl_raw.strip())
        if length < 0:
            raise ValueError("negative Content-Length")
        if length > MAX_REQUEST_BODY_BYTES:
            return None
        if length == 0:
            return b""
        return self.rfile.read(length)

    def _handle_control(self) -> bool:
        path_only = self.path.split("?", 1)[0]
        if path_only == "/__healthz" and self.command == "GET":
            self._send_json(200, self.server.engine.health_payload())
            return True

        if path_only == "/__status" and self.command == "GET":
            if not self._check_control_auth():
                return True
            self._send_json(200, self.server.engine.status_payload())
            return True

        if path_only == "/__register" and self.command == "POST":
            if not self._check_control_auth():
                return True
            try:
                body = self._read_request_body()
                if body is None:
                    self._send_json(413, {"ok": False, "error": "payload_too_large"})
                    return True
                data = json.loads(body.decode("utf-8"))
                scope_id = str(data.get("scope_id", "")).strip()
                mode = str(data.get("mode", "cli")).strip()
                real_agy = str(data.get("real_agy", "")).strip()
                if not scope_id or mode not in ("cli", "acp") or not _is_valid_agy_binary(real_agy):
                    self._send_json(400, {"ok": False, "error": "invalid_registration"})
                    return True
                spec = ProbeSpec(
                    mode=mode,
                    real_agy=real_agy,
                    gemini_dir=str(data.get("gemini_dir", "") or ""),
                    app_data_dir=str(data.get("app_data_dir", "") or ""),
                    gemini_home=str(data.get("gemini_home", "") or ""),
                    ssh_connection=str(data.get("ssh_connection", "") or ""),
                    force_file_storage=bool(data.get("force_file_storage", False)),
                )
                self.server.engine.register_scope(scope_id, spec)
                self._send_json(200, {"ok": True, "scope_id": scope_id})
            except Exception as exc:
                self._send_json(400, {"ok": False, "error": type(exc).__name__})
            return True

        if path_only == "/__reset_scope" and self.command == "POST":
            if not self._check_control_auth():
                return True
            try:
                body = self._read_request_body() or b"{}"
                data = json.loads(body.decode("utf-8"))
                scope_id = str(data.get("scope_id", "")).strip()
                if scope_id:
                    self.server.engine.reset_scope(scope_id)
                self._send_json(200, {"ok": True})
            except Exception as exc:
                self._send_json(400, {"ok": False, "error": type(exc).__name__})
            return True

        if path_only == "/__shutdown" and self.command == "POST":
            if not self._check_control_auth():
                return True
            self._send_json(200, {"ok": True, "shutting_down": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return True

        return False

    def _is_client_alive(self) -> bool:
        """Non-blocking check whether the local client socket is still open."""
        sock = self.connection
        if sock is None:
            return False
        try:
            rlist, _, _ = select.select([sock], [], [], 0.0)
            if not rlist:
                return True
            peek_flags = socket.MSG_PEEK | getattr(socket, "MSG_DONTWAIT", 0)
            peek = sock.recv(1, peek_flags)
            return len(peek) > 0
        except BlockingIOError:
            return True
        except OSError:
            return False

    def _maybe_compress_body(self, body: bytes) -> Tuple[bytes, bool]:
        """Transparently gzip large uncompressed JSON bodies to slash slow-proxy upload time."""
        engine = self.server.engine
        if (
            not body
            or len(body) < GZIP_MIN_BODY_BYTES
            or not engine.is_gzip_enabled()
            or self.command not in ("POST", "PUT", "PATCH")
        ):
            return body, False
        if self.headers.get("Content-Encoding"):
            return body, False
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "json" not in ctype:
            return body, False
        try:
            compressed = gzip.compress(body, compresslevel=1)
        except Exception:
            return body, False
        if len(compressed) < int(len(body) * 0.92):
            return compressed, True
        return body, False

    def _build_upstream_headers(
        self,
        body: bytes,
        override_bearer: Optional[str],
        gzip_encoded: bool = False,
    ) -> Dict[str, str]:
        req_items = list(self.headers.items())
        hop = _extract_hop_by_hop(req_items)
        out: Dict[str, str] = {}
        host_val = self.server.engine.upstream_host
        if (
            self.server.engine.upstream_scheme == "https"
            and self.server.engine.upstream_port != 443
        ) or (
            self.server.engine.upstream_scheme == "http"
            and self.server.engine.upstream_port != 80
        ):
            host_val = f"{host_val}:{self.server.engine.upstream_port}"

        for k, v in req_items:
            kl = k.lower()
            if kl in hop or kl in ("host", "content-length", "expect"):
                continue
            if override_bearer is not None and kl == "authorization":
                continue
            if gzip_encoded and kl == "content-encoding":
                continue
            out[k] = v
        out["Host"] = host_val
        if override_bearer is not None:
            out["Authorization"] = f"Bearer {override_bearer}"
        if gzip_encoded:
            out["Content-Encoding"] = "gzip"
        if body or self.command in ("POST", "PUT", "PATCH"):
            out["Content-Length"] = str(len(body))
        out["Connection"] = "close"
        return out

    def _send_upstream_once(
        self,
        upstream_path: str,
        body: bytes,
        override_bearer: Optional[str],
        allow_gzip: bool = True,
    ) -> Tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        engine = self.server.engine
        wire_body, gzip_used = (
            self._maybe_compress_body(body) if allow_gzip else (body, False)
        )
        headers = self._build_upstream_headers(
            wire_body, override_bearer, gzip_encoded=gzip_used
        )
        full_path = engine.upstream_base_path + upstream_path
        log_path = upstream_path.split("?", 1)[0]
        start_mono = time.monotonic()
        last_exc: Optional[Exception] = None

        for attempt in range(MAX_TRANSIENT_RETRIES + 1):
            if not self._is_client_alive():
                raise _ClientDisconnected()
            conn: Optional[http.client.HTTPConnection] = None
            stage = "connect"
            t_attempt = time.monotonic()
            send_ms = 0
            try:
                conn = engine.open_upstream_connection()
                stage = "send"
                conn.putrequest(self.command, full_path, skip_host=True, skip_accept_encoding=True)
                for hk, hv in headers.items():
                    conn.putheader(hk, hv)
                conn.endheaders()
                if wire_body:
                    view = memoryview(wire_body)
                    total_wire = len(wire_body)
                    offset = 0
                    while offset < total_wire:
                        if offset > 0 and not self._is_client_alive():
                            raise _ClientDisconnected()
                        end = min(offset + UPLOAD_CHUNK_BYTES, total_wire)
                        conn.send(view[offset:end])
                        offset = end
                send_ms = int((time.monotonic() - t_attempt) * 1000)
                if conn.sock is not None:
                    conn.sock.settimeout(engine.read_timeout_sec)
                stage = "headers"
                resp = conn.getresponse()

                # Auto-fallback if upstream ever rejects Content-Encoding: gzip
                if gzip_used and resp.status in (400, 415):
                    err_probe = b""
                    try:
                        err_probe = resp.read(4096)
                    except Exception:
                        pass
                    low_err = err_probe.decode("utf-8", errors="ignore").lower()
                    if resp.status == 415 or any(
                        tok in low_err for tok in ("gzip", "content-encoding", "encoding", "decompress")
                    ):
                        try:
                            conn.close()
                        except Exception:
                            pass
                        engine.disable_gzip()
                        engine.logger.warning(
                            "gzip_fallback_disabled path=%s status=%d req_bytes=%d wire_bytes=%d",
                            log_path,
                            resp.status,
                            len(body),
                            len(wire_body),
                        )
                        return self._send_upstream_once(
                            upstream_path, body, override_bearer, allow_gzip=False
                        )
                    # Put back buffered 400 body via fresh uncompressed retry only if not gzip-related:
                    # instead, return an already-read failure only if needed; simplest & safest:
                    # replay uncompressed once on 400 when gzip was added by us so we never lose the body.
                    try:
                        conn.close()
                    except Exception:
                        pass
                    return self._send_upstream_once(
                        upstream_path, body, override_bearer, allow_gzip=False
                    )

                if gzip_used:
                    engine.inc_stat("gzip_compressed_requests")
                    engine.inc_stat("gzip_bytes_saved", max(0, len(body) - len(wire_body)))

                total_ms = int((time.monotonic() - start_mono) * 1000)
                if send_ms >= SLOW_LOG_SEND_MS or len(body) >= SLOW_LOG_REQ_BYTES:
                    engine.logger.info(
                        "upstream_req path=%s status=%d req_bytes=%d wire_bytes=%d gzip=%d send_ms=%d ttfb_ms=%d attempts=%d",
                        log_path,
                        resp.status,
                        len(body),
                        len(wire_body),
                        1 if gzip_used else 0,
                        send_ms,
                        total_ms,
                        attempt + 1,
                    )
                return conn, resp
            except _ClientDisconnected:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                raise
            except (
                http.client.RemoteDisconnected,
                http.client.BadStatusLine,
                ConnectionResetError,
                BrokenPipeError,
                ssl.SSLEOFError,
                TimeoutError,
            ) as exc:
                last_exc = exc
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                if isinstance(exc, TimeoutError) and stage == "send":
                    engine.inc_stat("write_timeouts")
                elapsed = time.monotonic() - start_mono
                # Never blindly retry after a full header read timeout (e.g. 600s) or dead client
                can_retry = (
                    attempt < MAX_TRANSIENT_RETRIES
                    and not (isinstance(exc, TimeoutError) and stage == "headers")
                    and elapsed < MAX_RETRY_ELAPSED_SEC
                    and self._is_client_alive()
                )
                if can_retry:
                    engine.inc_stat("network_retries")
                    engine.logger.warning(
                        "net_retry path=%s stage=%s attempt=%d req_bytes=%d wire_bytes=%d elapsed_ms=%d err=%s",
                        log_path,
                        stage,
                        attempt + 1,
                        len(body),
                        len(wire_body),
                        int(elapsed * 1000),
                        type(exc).__name__,
                    )
                    time.sleep(0.3 * (attempt + 1))
                    continue
                raise
            except Exception:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                raise
        assert last_exc is not None
        raise last_exc

    def _stream_response_to_client(
        self, conn: http.client.HTTPConnection, resp: http.client.HTTPResponse
    ) -> None:
        try:
            resp_headers = resp.getheaders()
            hop = _extract_hop_by_hop(resp_headers)
            content_type = (resp.getheader("Content-Type") or "").lower()
            is_sse = "text/event-stream" in content_type
            no_body = (
                self.command == "HEAD"
                or resp.status in (204, 304)
                or (100 <= resp.status < 200)
            )
            cl_header = resp.getheader("Content-Length")
            use_fixed_length = (
                not no_body
                and not is_sse
                and not resp.chunked
                and cl_header is not None
                and cl_header.strip().isdigit()
            )
            use_chunked = not no_body and not use_fixed_length

            self.send_response(resp.status, resp.reason)
            for k, v in resp_headers:
                kl = k.lower()
                if kl in hop:
                    continue
                if use_chunked and kl == "content-length":
                    continue
                self.send_header(k, v)
            if use_chunked:
                self.send_header("Transfer-Encoding", "chunked")
            elif no_body and not cl_header:
                self.send_header("Content-Length", "0")
            self.end_headers()

            if no_body:
                self.wfile.flush()
                return

            expected_len = int(cl_header.strip()) if use_fixed_length and cl_header else None
            written = 0
            while True:
                chunk = resp.read1(16384)
                if not chunk:
                    break
                written += len(chunk)
                if use_chunked:
                    self.wfile.write(f"{len(chunk):x}\r\n".encode("ascii"))
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()

            if use_chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            elif expected_len is not None and written != expected_len:
                self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            self.close_connection = True
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _stream_response_safely(
        self,
        conn: http.client.HTTPConnection,
        resp: http.client.HTTPResponse,
        scope_id: str,
        log_path: str,
    ) -> None:
        try:
            self._stream_response_to_client(conn, resp)
        except Exception as exc:
            self.server.engine.logger.warning(
                "stream_error scope=%s path=%s err=%s",
                scope_id,
                log_path,
                type(exc).__name__,
            )

    def _send_buffered_response(
        self, status: int, reason: str, headers: List[Tuple[str, str]], body: bytes
    ) -> None:
        hop = _extract_hop_by_hop(headers)
        self.send_response(status, reason)
        for k, v in headers:
            kl = k.lower()
            if kl in hop or kl == "content-length":
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)
        self.wfile.flush()

    def _proxy_request(self) -> None:
        if self._handle_control():
            return

        engine = self.server.engine
        engine.inc_stat("requests_total")
        scope_id, upstream_path, is_probe = _parse_route(self.path)
        log_path = upstream_path.split("?", 1)[0]
        scope = engine.get_scope(scope_id)

        try:
            body = self._read_request_body()
        except Exception as exc:
            self.close_connection = True
            self._send_json(400, {"error": f"bad_request_body:{type(exc).__name__}"})
            return

        if body is None:
            self.close_connection = True
            self._send_json(413, {"error": "request_body_too_large"})
            return

        req_token = _parse_bearer(self.headers.get("Authorization"))
        attempt_token = req_token
        proactively_swapped = False

        if req_token and not is_probe:
            attempt_token, proactively_swapped = scope.select_token_for_request(req_token)
            if proactively_swapped:
                engine.inc_stat("proactive_swaps")
                engine.logger.info("proactive_swap scope=%s path=%s", scope_id, log_path)

        t0 = time.monotonic()
        try:
            conn, resp = self._send_upstream_once(
                upstream_path, body, attempt_token if proactively_swapped else None
            )
        except _ClientDisconnected:
            engine.inc_stat("client_aborts")
            engine.logger.info(
                "client_aborted scope=%s path=%s req_bytes=%d elapsed_ms=%d",
                scope_id,
                log_path,
                len(body),
                int((time.monotonic() - t0) * 1000),
            )
            self.close_connection = True
            return
        except Exception as exc:
            engine.logger.warning(
                "upstream_error scope=%s path=%s req_bytes=%d elapsed_ms=%d err=%s",
                scope_id,
                log_path,
                len(body),
                int((time.monotonic() - t0) * 1000),
                type(exc).__name__,
            )
            self.close_connection = True
            status_code = 504 if isinstance(exc, TimeoutError) else 502
            try:
                self._send_json(status_code, {"error": f"upstream_unreachable:{type(exc).__name__}"})
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return

        # Fast path: success or non-401 or probe request or unauthenticated request
        if resp.status != 401 or not attempt_token or is_probe:
            if attempt_token:
                if 200 <= resp.status < 300:
                    scope.record_ok(attempt_token, from_probe=is_probe)
                    if is_probe:
                        engine.logger.info(
                            "probe_token_captured scope=%s path=%s status=%d",
                            scope_id,
                            log_path,
                            resp.status,
                        )
                elif resp.status == 401:
                    scope.record_dead(attempt_token)
            self._stream_response_safely(conn, resp, scope_id, log_path)
            return

        # Reactive 401 healing path
        orig_status = resp.status
        orig_reason = resp.reason
        orig_headers = resp.getheaders()
        try:
            orig_body = resp.read()
        except Exception:
            orig_body = b""
        finally:
            try:
                conn.close()
            except Exception:
                pass

        # Snapshot ok_seq before Tier 1/2 so older stale cached tokens can never
        # short-circuit Tier 2 probing, while newer tokens arriving concurrently
        # (including singleflight probe peers) are still reused immediately.
        since_ok_seq = scope.current_ok_seq()

        failed_tokens: Set[str] = {attempt_token}
        scope.record_dead(attempt_token)
        if req_token and req_token != attempt_token:
            failed_tokens.add(req_token)
            scope.record_dead(req_token)

        if not self._is_client_alive():
            engine.inc_stat("client_aborts")
            self.close_connection = True
            return

        # Tier 1: Try another already-known non-dead valid token in this scope
        alt_token = scope.get_fresh_token(exclude=failed_tokens)
        if alt_token is not None:
            try:
                rconn, rresp = self._send_upstream_once(upstream_path, body, alt_token)
            except _ClientDisconnected:
                engine.inc_stat("client_aborts")
                self.close_connection = True
                return
            except Exception as exc:
                engine.logger.warning(
                    "heal_cache_retry_err scope=%s path=%s elapsed_ms=%d err=%s",
                    scope_id,
                    log_path,
                    int((time.monotonic() - t0) * 1000),
                    type(exc).__name__,
                )
                self.close_connection = True
                status_code = 504 if isinstance(exc, TimeoutError) else 502
                self._send_json(status_code, {"error": f"upstream_unreachable:{type(exc).__name__}"})
                return

            if rresp.status != 401:
                if 200 <= rresp.status < 300:
                    scope.record_ok(alt_token)
                engine.inc_stat("healed_401_cache")
                engine.logger.info(
                    "heal_401_cache scope=%s path=%s status=%d elapsed_ms=%d",
                    scope_id,
                    log_path,
                    rresp.status,
                    int((time.monotonic() - t0) * 1000),
                )
                self._stream_response_safely(rconn, rresp, scope_id, log_path)
                return
            try:
                rresp.read()
            except Exception:
                pass
            finally:
                try:
                    rconn.close()
                except Exception:
                    pass
            failed_tokens.add(alt_token)
            scope.record_dead(alt_token)

        if not self._is_client_alive():
            engine.inc_stat("client_aborts")
            self.close_connection = True
            return

        # Tier 2: Singleflight on-demand official `agy` probe
        fresh_token = engine.ensure_fresh_token(scope, failed_tokens, min_ok_seq=since_ok_seq)
        if not self._is_client_alive():
            engine.inc_stat("client_aborts")
            self.close_connection = True
            return
        if fresh_token is not None:
            try:
                pconn, presp = self._send_upstream_once(upstream_path, body, fresh_token)
            except _ClientDisconnected:
                engine.inc_stat("client_aborts")
                self.close_connection = True
                return
            except Exception as exc:
                engine.logger.warning(
                    "heal_probe_retry_err scope=%s path=%s elapsed_ms=%d err=%s",
                    scope_id,
                    log_path,
                    int((time.monotonic() - t0) * 1000),
                    type(exc).__name__,
                )
                self.close_connection = True
                status_code = 504 if isinstance(exc, TimeoutError) else 502
                self._send_json(status_code, {"error": f"upstream_unreachable:{type(exc).__name__}"})
                return

            if presp.status != 401:
                if 200 <= presp.status < 300:
                    scope.record_ok(fresh_token)
                engine.inc_stat("healed_401_probe")
                engine.logger.info(
                    "heal_401_probe scope=%s path=%s status=%d elapsed_ms=%d",
                    scope_id,
                    log_path,
                    presp.status,
                    int((time.monotonic() - t0) * 1000),
                )
                self._stream_response_safely(pconn, presp, scope_id, log_path)
                return
            scope.record_dead(fresh_token)
            engine.inc_stat("unhealed_401")
            engine.logger.warning(
                "unhealed_401_after_probe scope=%s path=%s", scope_id, log_path
            )
            self._stream_response_safely(pconn, presp, scope_id, log_path)
            return

        engine.inc_stat("unhealed_401")
        engine.logger.warning(
            "unhealed_401 scope=%s path=%s elapsed_ms=%d",
            scope_id,
            log_path,
            int((time.monotonic() - t0) * 1000),
        )
        try:
            self._send_buffered_response(orig_status, orig_reason, orig_headers, orig_body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    def do_GET(self) -> None:
        self._proxy_request()

    def do_POST(self) -> None:
        self._proxy_request()

    def do_PUT(self) -> None:
        self._proxy_request()

    def do_DELETE(self) -> None:
        self._proxy_request()

    def do_PATCH(self) -> None:
        self._proxy_request()

    def do_HEAD(self) -> None:
        self._proxy_request()

    def do_OPTIONS(self) -> None:
        self._proxy_request()


def _write_pid(pid_path: Path) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(pid_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    finally:
        os.close(fd)


def main() -> int:
    port = int(os.environ.get("AGY_AUTH_SHIELD_PORT", str(DEFAULT_PORT)))
    upstream = os.environ.get("AGY_AUTH_SHIELD_UPSTREAM", DEFAULT_UPSTREAM)
    logger = _setup_logging(LOG_FILE)
    control_token = _ensure_control_token(TOKEN_FILE)
    engine = ShieldEngine(
        port=port,
        upstream_url=upstream,
        control_token=control_token,
        logger=logger,
        scopes_file=SCOPES_FILE,
    )

    try:
        server = ShieldHTTPServer((DEFAULT_HOST, port), engine)
    except OSError as exc:
        logger.error("bind_failed host=%s port=%d err=%s", DEFAULT_HOST, port, exc)
        sys.stderr.write(f"agy-auth-shield: failed to bind {DEFAULT_HOST}:{port}: {exc}\n")
        return 1

    _write_pid(PID_FILE)

    def _on_signal(signum: int, _frame) -> None:
        logger.info("signal_received signum=%d shutting_down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    logger.info(
        "started version=%s pid=%d host=%s port=%d upstream=%s python=%s ssl=%s",
        VERSION,
        os.getpid(),
        DEFAULT_HOST,
        port,
        f"{engine.upstream_scheme}://{engine.upstream_host}:{engine.upstream_port}",
        sys.version.split()[0],
        ssl.OPENSSL_VERSION,
    )
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        try:
            if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
                PID_FILE.unlink()
        except OSError:
            pass
        logger.info("stopped pid=%d", os.getpid())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
