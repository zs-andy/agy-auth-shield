# Security Policy

## Architecture & Security Boundaries

`agy-auth-shield` is designed to sit transparently between `agy` and Google's Cloud Code API with minimal privilege and zero credential persistence:

1. **Loopback-only binding:** The daemon binds strictly to `127.0.0.1` and refuses external network exposure.
2. **RAM-only OAuth tokens:** Observed `Authorization: Bearer ...` access tokens are held exclusively in process memory (`ScopeState`) and are never written to disk, logs, or `run/scopes.json`.
3. **No credential store access:** `agy-auth-shield` never opens macOS Keychain, Linux Secret Service, or T3 Code credential files. When a token expires and no newer token is cached, refresh is delegated to the official `agy` executable via its documented interfaces (`agy models` / `agy acp` `initialize` + `authenticate`).
4. **Authenticated local control plane:** Control endpoints (`/__status`, `/__register`, `/__reset_scope`, `/__shutdown`) require an `X-Shield-Token` header verified with constant-time comparison (`secrets.compare_digest`) against a per-install 256-bit random hex token stored with `0600` permissions inside a `0700` directory.
5. **Executable validation:** `/__register` rejects any `real_agy` path that is not an absolute path to an executable named `agy` or `antigravity`.
6. **Strict TLS verification upstream:** Upstream HTTPS connections always enforce hostname verification and certificate validation (`ssl.CERT_REQUIRED`).
7. **Zero-token log hygiene:** Request headers, query strings, request/response bodies, and Bearer tokens are never logged.

## Reporting a Vulnerability

If you discover a security issue (for example, local privilege escalation, token leakage to disk/logs, or control-plane bypass), please report it privately via GitHub Security Advisories on `zs-andy/agy-auth-shield` rather than opening a public issue.
