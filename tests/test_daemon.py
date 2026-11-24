#!/usr/bin/env python3
"""Isolated integration tests for agy_auth_shield.py (no network or real `agy` required)."""

from __future__ import annotations

import concurrent.futures
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
DAEMON_SCRIPT = REPO_ROOT / "src" / "agy_auth_shield.py"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_json(port: int, method: str, path: str, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        hdrs = dict(headers or {})
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        if payload is not None:
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, json.loads(raw.decode("utf-8")) if raw else None
    finally:
        conn.close()


class FastThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class DaemonIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agy-shield-daemon-test-"))
        self.mock_port = free_port()
        self.shield_port = free_port()
        self.shield_home = self.tmp / "shield_home"

        self.state_lock = threading.Lock()
        self.valid_tokens = {"tok_v1", "tok_v2"}
        self.eof_once_done = {"done": False}

        test_case = self

        class MockUpstream(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                return

            def _check_auth(self):
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer "):
                    return None
                tok = auth[len("Bearer ") :].strip()
                with test_case.state_lock:
                    ok = tok in test_case.valid_tokens
                return tok if ok else None

            def _send_401(self):
                body = json.dumps(
                    {
                        "error": {
                            "code": 401,
                            "status": "UNAUTHENTICATED",
                            "message": "mock expired",
                        }
                    }
                ).encode()
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            def do_POST(self):
                cl = int(self.headers.get("Content-Length") or "0")
                req_body = self.rfile.read(cl) if cl else b""

                if self.path == "/v1internal:eofOnce":
                    with test_case.state_lock:
                        should_drop = not test_case.eof_once_done["done"]
                        test_case.eof_once_done["done"] = True
                    if should_drop:
                        self.connection.close()
                        return

                tok = self._check_auth()
                if tok is None:
                    self._send_401()
                    return

                if self.path.startswith("/v1internal:streamGenerateContent"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for idx in range(1, 4):
                        ev = f'data: {{"event":{idx},"tok":"{tok}"}}\n\n'.encode()
                        self.wfile.write(f"{len(ev):x}\r\n".encode() + ev + b"\r\n")
                        self.wfile.flush()
                        if idx < 3:
                            time.sleep(0.12)
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                    return

                resp_obj = {
                    "ok": True,
                    "path": self.path,
                    "tok": tok,
                    "echo": req_body.decode("utf-8", errors="replace"),
                }
                raw = json.dumps(resp_obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                self.wfile.flush()

        self.mock_srv = FastThreadingHTTPServer(("127.0.0.1", self.mock_port), MockUpstream)
        threading.Thread(target=self.mock_srv.serve_forever, daemon=True).start()

        # Fake `agy` binary used by Tier-2 probes
        self.fake_bin_dir = self.tmp / "fakebin"
        self.fake_bin_dir.mkdir()
        self.fake_agy = self.fake_bin_dir / "agy"
        self.probe_token_file = self.tmp / "probe_token.txt"
        self.probe_acp_log = self.tmp / "probe_acp_methods.txt"
        self.probe_err_log = self.tmp / "probe_err.txt"
        self.probe_token_file.write_text("tok_probed_fresh\n", encoding="utf-8")
        self.fake_agy.write_text(
            f"""#!{sys.executable}
import json, os, sys, urllib.request
url = os.environ["CLOUD_CODE_URL"].rstrip("/") + "/v1internal:loadCodeAssist"
tok = open({str(self.probe_token_file)!r}).read().strip()
def hit():
    req = urllib.request.Request(
        url,
        data=b"{{}}",
        headers={{"Authorization": f"Bearer {{tok}}", "Content-Type": "application/json"}},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
    except Exception as exc:
        with open({str(self.probe_err_log)!r}, "a") as ef:
            ef.write(repr(exc) + "\\n")
if len(sys.argv) > 1 and sys.argv[1] == "acp":
    for line in sys.stdin:
        msg = json.loads(line)
        m = msg.get("method", "")
        with open({str(self.probe_acp_log)!r}, "a") as lf:
            lf.write(m + "\\n")
        if m == "initialize":
            sys.stdout.write(json.dumps({{"jsonrpc":"2.0","id":msg["id"],"result":{{"protocolVersion":1}}}}) + "\\n")
            sys.stdout.flush()
        elif m == "authenticate":
            hit()
            sys.stdout.write(json.dumps({{"jsonrpc":"2.0","id":msg["id"],"result":{{}}}}) + "\\n")
            sys.stdout.flush()
            break
else:
    hit()
""",
            encoding="utf-8",
        )
        self.fake_agy.chmod(0o755)

        self.daemon = self._start_daemon()
        self.ctrl_tok = (self.shield_home / "run" / "control.token").read_text().strip()
        self.ctrl_hdrs = {"X-Shield-Token": self.ctrl_tok}

    def _start_daemon(self) -> subprocess.Popen:
        env = dict(os.environ)
        env["AGY_AUTH_SHIELD_HOME"] = str(self.shield_home)
        env["AGY_AUTH_SHIELD_PORT"] = str(self.shield_port)
        env["AGY_AUTH_SHIELD_UPSTREAM"] = f"http://127.0.0.1:{self.mock_port}"
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["no_proxy"] = "127.0.0.1,localhost"
        proc = subprocess.Popen(
            [sys.executable, str(DAEMON_SCRIPT)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(50):
            try:
                st, payload = http_json(self.shield_port, "GET", "/__healthz")
                if st == 200 and payload and payload.get("ok"):
                    return proc
            except Exception:
                time.sleep(0.08)
        proc.kill()
        raise AssertionError("Daemon failed to start in time")

    def _stop_daemon(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
                proc.wait(timeout=2)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    def tearDown(self) -> None:
        self._stop_daemon(self.daemon)
        self.mock_srv.shutdown()
        self.mock_srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_control_auth_and_permissions(self) -> None:
        st, _ = http_json(self.shield_port, "GET", "/__status")
        self.assertEqual(st, 403)
        st, _ = http_json(
            self.shield_port, "GET", "/__status", headers={"X-Shield-Token": "wrong"}
        )
        self.assertEqual(st, 403)
        st, body = http_json(self.shield_port, "GET", "/__status", headers=self.ctrl_hdrs)
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(stat.S_IMODE(self.shield_home.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.shield_home / "run").stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.shield_home / "run" / "control.token").stat().st_mode), 0o600
        )

    def test_sse_zero_buffering_streaming(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.shield_port, timeout=10)
        conn.request(
            "POST",
            "/scope/cli-sse/v1internal:streamGenerateContent?alt=sse",
            body=b'{"prompt":"hi"}',
            headers={"Authorization": "Bearer tok_v1", "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        times = []
        buf = b""
        while True:
            chunk = resp.read1(4096)
            if not chunk:
                break
            buf += chunk
            times.append(time.monotonic())
        conn.close()
        self.assertIn(b'"event":1', buf)
        self.assertIn(b'"event":3', buf)
        self.assertGreaterEqual(len(times), 3)
        self.assertGreaterEqual(times[-1] - times[0], 0.18)

    def test_chunked_request_body_and_100_continue(self) -> None:
        raw_sock = socket.create_connection(("127.0.0.1", self.shield_port), timeout=10)
        req_head = (
            "POST /scope/cli-chunk/v1internal:echo HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.shield_port}\r\n"
            "Authorization: Bearer tok_v1\r\n"
            "Transfer-Encoding: chunked\r\n"
            "Expect: 100-continue\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        raw_sock.sendall(req_head)
        cont = raw_sock.recv(1024)
        self.assertIn(b"100 Continue", cont)
        raw_sock.sendall(b"5\r\nhello\r\n6\r\n_world\r\n0\r\n\r\n")
        resp_raw = b""
        while True:
            part = raw_sock.recv(4096)
            if not part:
                break
            resp_raw += part
        raw_sock.close()
        self.assertIn(b"200 OK", resp_raw)
        self.assertIn(b'"echo": "hello_world"', resp_raw)

    def test_proactive_swap_and_tier1_cache_heal(self) -> None:
        st, d = http_json(
            self.shield_port,
            "POST",
            "/scope/cli-swap/v1internal:call",
            headers={"Authorization": "Bearer tok_v1"},
            body={"k": 0},
        )
        self.assertEqual(st, 200)
        st, d = http_json(
            self.shield_port,
            "POST",
            "/scope/cli-swap/v1internal:call",
            headers={"Authorization": "Bearer tok_v2"},
            body={"k": 1},
        )
        self.assertEqual((st, d["tok"]), (200, "tok_v2"))

        # Stale tok_v1 is proactively swapped before hitting upstream
        st, d = http_json(
            self.shield_port,
            "POST",
            "/scope/cli-swap/v1internal:call",
            headers={"Authorization": "Bearer tok_v1"},
            body={"k": 2},
        )
        self.assertEqual((st, d["tok"]), (200, "tok_v2"))

        # Never-seen expired token hits 401 and heals from RAM cache
        st, d = http_json(
            self.shield_port,
            "POST",
            "/scope/cli-swap/v1internal:call",
            headers={"Authorization": "Bearer tok_expired_unknown"},
            body={"k": 3},
        )
        self.assertEqual((st, d["tok"]), (200, "tok_v2"))

        _, status_after = http_json(
            self.shield_port, "GET", "/__status", headers=self.ctrl_hdrs
        )
        self.assertGreaterEqual(status_after["stats"]["proactive_swaps"], 1)
        self.assertGreaterEqual(status_after["stats"]["healed_401_cache"], 1)

    def test_singleflight_cli_probe_under_concurrent_burst(self) -> None:
        st, _ = http_json(
            self.shield_port,
            "POST",
            "/__register",
            headers=self.ctrl_hdrs,
            body={"scope_id": "cli-burst", "mode": "cli", "real_agy": str(self.fake_agy)},
        )
        self.assertEqual(st, 200)
        with self.state_lock:
            self.valid_tokens.add("tok_probed_fresh")

        def call(idx: int):
            return http_json(
                self.shield_port,
                "POST",
                f"/scope/cli-burst/v1internal:burst{idx}",
                headers={"Authorization": "Bearer tok_expired_burst"},
                body={"idx": idx},
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            results = list(ex.map(call, [1, 2, 3]))
        for st, d in results:
            self.assertEqual((st, d["tok"]), (200, "tok_probed_fresh"))

        _, status_after = http_json(
            self.shield_port, "GET", "/__status", headers=self.ctrl_hdrs
        )
        self.assertEqual(status_after["stats"]["probe_attempts"], 1)
        self.assertEqual(status_after["stats"]["probe_successes"], 1)

    def test_acp_probe_and_spec_persistence_across_restart(self) -> None:
        self.probe_token_file.write_text("tok_acp_probed\n", encoding="utf-8")
        with self.state_lock:
            self.valid_tokens.add("tok_acp_probed")

        st, _ = http_json(
            self.shield_port,
            "POST",
            "/__register",
            headers=self.ctrl_hdrs,
            body={
                "scope_id": "acp-persist",
                "mode": "acp",
                "real_agy": str(self.fake_agy),
                "gemini_dir": str(self.tmp / "fake_gemini"),
                "app_data_dir": "antigravity-acp",
                "force_file_storage": True,
            },
        )
        self.assertEqual(st, 200)

        # Restart daemon WITHOUT re-registering scope; verify persisted non-secret ProbeSpec heals 401!
        self._stop_daemon(self.daemon)
        self.daemon = self._start_daemon()

        st, d = http_json(
            self.shield_port,
            "POST",
            "/scope/acp-persist/v1internal:streamCheck",
            headers={"Authorization": "Bearer tok_acp_expired"},
            body={"turn": 1},
        )
        diag = (
            f"status={st} body={d} "
            f"probe_err={self.probe_err_log.read_text(encoding='utf-8') if self.probe_err_log.exists() else ''} "
            f"log={(self.shield_home / 'shield.log').read_text(encoding='utf-8') if (self.shield_home / 'shield.log').exists() else ''}"
        )
        self.assertEqual(st, 200, diag)
        self.assertEqual(d.get("tok"), "tok_acp_probed", diag)
        acp_methods = self.probe_acp_log.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(acp_methods, ["initialize", "authenticate"])

        # Verify zero Bearer tokens on disk in scopes.json or shield.log
        scopes_txt = (self.shield_home / "run" / "scopes.json").read_text(encoding="utf-8")
        log_txt = (self.shield_home / "shield.log").read_text(encoding="utf-8")
        for secret in ("tok_v1", "tok_v2", "tok_acp_probed", "tok_acp_expired"):
            self.assertNotIn(secret, scopes_txt)
            self.assertNotIn(secret, log_txt)

    def test_genuine_logout_passthrough_and_cooldown(self) -> None:
        self.probe_token_file.write_text("tok_still_revoked\n", encoding="utf-8")
        st, _ = http_json(
            self.shield_port,
            "POST",
            "/__register",
            headers=self.ctrl_hdrs,
            body={"scope_id": "cli-revoked", "mode": "cli", "real_agy": str(self.fake_agy)},
        )
        self.assertEqual(st, 200)

        st1, d1 = http_json(
            self.shield_port,
            "POST",
            "/scope/cli-revoked/v1internal:call",
            headers={"Authorization": "Bearer tok_dead_1"},
            body={},
        )
        self.assertEqual(st1, 401)
        self.assertEqual(d1["error"]["status"], "UNAUTHENTICATED")

        st2, _ = http_json(
            self.shield_port,
            "POST",
            "/scope/cli-revoked/v1internal:call",
            headers={"Authorization": "Bearer tok_dead_2"},
            body={},
        )
        self.assertEqual(st2, 401)
        _, status_after = http_json(
            self.shield_port, "GET", "/__status", headers=self.ctrl_hdrs
        )
        self.assertEqual(status_after["stats"]["probe_attempts"], 1)

    def test_transient_eof_retry(self) -> None:
        st, d = http_json(
            self.shield_port,
            "POST",
            "/scope/cli-eof/v1internal:eofOnce",
            headers={"Authorization": "Bearer tok_v2"},
            body={"retry": True},
        )
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        _, status_after = http_json(
            self.shield_port, "GET", "/__status", headers=self.ctrl_hdrs
        )
        self.assertGreaterEqual(status_after["stats"]["network_retries"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
