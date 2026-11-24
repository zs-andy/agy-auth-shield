#!/usr/bin/env python3
"""Opt-in live end-to-end verification against an active agy-auth-shield install and Google API.

Not executed in CI. Run manually on a machine signed into Antigravity CLI:
    python3 tests/live_e2e.py
"""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import stat
import subprocess


SHIELD_HOME = Path(
    os.environ.get("AGY_AUTH_SHIELD_HOME", Path.home() / ".local" / "share" / "agy-auth-shield")
)
SHIELD_PORT = int(os.environ.get("AGY_AUTH_SHIELD_PORT", "18765"))
WRAPPER = SHIELD_HOME / "bin" / "agy"


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


def main() -> None:
    print(f"=== Running LIVE end-to-end verification against 127.0.0.1:{SHIELD_PORT} ===")
    ctrl_tok = (SHIELD_HOME / "run" / "control.token").read_text().strip()
    ctrl_hdrs = {"X-Shield-Token": ctrl_tok}

    cli_env = dict(os.environ)
    for k in ("GEMINI_HOME", "SSH_CONNECTION", "CLOUD_CODE_URL", "AGY_ACP_FORCE_FILE_STORAGE"):
        cli_env.pop(k, None)

    res = subprocess.run(
        [str(WRAPPER), "-p", "Reply with ONLY the exact word SHIELD_OK", "--effort", "low"],
        env=cli_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0, f"CLI failed rc={res.returncode} stderr={res.stderr[:400]}"
    assert "SHIELD_OK" in res.stdout, f"Unexpected CLI output: {res.stdout[:200]}"
    print(f"  [PASS] Live CLI through wrapper succeeded: {res.stdout.strip()!r}")

    st, status = http_json(SHIELD_PORT, "GET", "/__status", headers=ctrl_hdrs)
    assert st == 200 and status["ok"]
    cli_scopes = [
        sid
        for sid, info in status["scopes"].items()
        if sid.startswith("cli-") and info["has_valid_token"]
    ]
    assert cli_scopes, f"No valid cli-* scope registered: {status['scopes']}"
    cli_scope = cli_scopes[0]
    print(f"  [PASS] Live CLI scope registered & active: {cli_scope}")

    # Tier-1 cache heal
    st, body = http_json(
        SHIELD_PORT,
        "POST",
        f"/scope/{cli_scope}/v1internal:loadCodeAssist",
        headers={"Authorization": "Bearer ya29.INTENTIONALLY_EXPIRED_CACHE_TEST"},
        body={
            "metadata": {
                "ideType": "IDE_UNSPECIFIED",
                "platform": "PLATFORM_UNSPECIFIED",
                "pluginType": "GEMINI",
            }
        },
    )
    assert st == 200, f"Live Tier-1 cache heal failed: status={st} body={body}"
    print("  [PASS] Live Google 401 healed via Tier-1 RAM cache (200 OK)")

    # Tier-2 on-demand `agy models` probe heal
    st, _ = http_json(
        SHIELD_PORT, "POST", "/__reset_scope", headers=ctrl_hdrs, body={"scope_id": cli_scope}
    )
    assert st == 200
    st, body = http_json(
        SHIELD_PORT,
        "POST",
        f"/scope/{cli_scope}/v1internal:loadCodeAssist",
        headers={"Authorization": "Bearer ya29.INTENTIONALLY_EXPIRED_PROBE_TEST"},
        body={
            "metadata": {
                "ideType": "IDE_UNSPECIFIED",
                "platform": "PLATFORM_UNSPECIFIED",
                "pluginType": "GEMINI",
            }
        },
    )
    assert st == 200, f"Live Tier-2 probe heal failed: status={st} body={body}"
    print("  [PASS] Live Google 401 healed via Tier-2 on-demand `agy models` probe (200 OK)")

    # Hygiene audit
    for fname in ("shield.log", "launchd.log"):
        p = SHIELD_HOME / fname
        if p.exists():
            content = p.read_text(encoding="utf-8", errors="replace")
            assert "ya29." not in content and "Bearer " not in content, f"Secret leaked in {p}!"
    assert stat.S_IMODE((SHIELD_HOME / "run" / "control.token").stat().st_mode) == 0o600
    assert stat.S_IMODE(SHIELD_HOME.stat().st_mode) == 0o700
    print("  [PASS] Security & zero-token log hygiene verified")
    print("\nALL LIVE E2E TESTS PASSED!")


if __name__ == "__main__":
    main()
