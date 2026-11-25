# Changelog

All notable changes to `agy-auth-shield` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-11-26

### Fixed
- **Stale token pool no longer bypasses Tier-2 `agy` probe:** Added a 55-minute TTL (`VALID_TOKEN_TTL_SEC`, configurable via `AGY_AUTH_SHIELD_VALID_TOKEN_TTL_SEC`), monotonic `ok_seq` gating on Tier-2 probes, and older-token eviction when a confirmed token expires. Older expired tokens accumulated across hours can no longer short-circuit on-demand OAuth probing and trigger `unhealed_401_after_probe`.
- **Transient transport errors during 401 replay return `502`/`504` instead of stale `401`:** When a Tier-1 cache or Tier-2 probe replay fails strictly due to upstream transport drops (`SSLEOFError`, `ConnectionResetError`, `TimeoutError`), the gateway now returns `502 Bad Gateway` / `504 Gateway Timeout` so `agy` retries gracefully instead of aborting the turn with a stale `401 UNAUTHENTICATED`.
- **Safe `launchd` / `systemd` service ownership isolation:** `stop`, `uninstall`, `uninstall.sh`, and `agy` autostart now honor `AGY_AUTH_SHIELD_NO_SERVICE=1` (persisted by `install.sh --no-service`) and verify that a LaunchAgent plist / running `launchd` job / `systemd --user` unit belongs to `$SHIELD_HOME` before stopping or removing it. Isolated test runs can no longer accidentally boot out the host's production daemon.
- **Large-context upload stall & double-timeout protection:** Added transparent `gzip` compression for large uncompressed JSON bodies (`>= 16 KiB`, with automatic `415`/error fallback), 64 KiB chunked upload with per-chunk `30s` write-stall timeout and TCP keepalive, non-blocking client disconnect detection, and `504` fail-fast on full response-header read timeouts (preventing stacked 600s retries).
- Suppressed noisy `BrokenPipeError` tracebacks when a local client disconnects while writing JSON/buffered responses.

### Added
- Environment variables: `AGY_AUTH_SHIELD_GZIP_REQUESTS`, `AGY_AUTH_SHIELD_WRITE_TIMEOUT_SEC`, `AGY_AUTH_SHIELD_READ_TIMEOUT_SEC`, `AGY_AUTH_SHIELD_VALID_TOKEN_TTL_SEC`, and `AGY_AUTH_SHIELD_NO_SERVICE`.
- Gzip compression savings summary in `agy-auth-shield status` and slow/large request timing logs in `shield.log`.
- Regression tests covering stale-token pool probing, valid-token TTL eviction, replay transport-error `502` handling, large JSON gzip + `415` fallback, single-attempt header read timeout, and foreign-service isolation.

## [1.1.0] - 2026-11-24

### Added
- Production-grade `install.sh` supporting one-line remote install (`curl | bash`), local checkout install, `--dry-run`, `--port`, `--dir`, `--enable-t3`, `--no-path`, `--no-service`, and `--ref`.
- Complete **zero-residue uninstall** via `agy-auth-shield uninstall` and standalone `uninstall.sh` (`--yes`, `--keep-logs`, `--keep-t3`), cleanly removing daemon, service definitions, managed shell RC blocks across zsh/bash/fish, T3 Code override, and installation directory.
- `agy-auth-shield doctor` diagnostic suite checking Python/OpenSSL, original `agy` binary, `PATH` precedence, loopback daemon health, control token auth, `0700`/`0600` permissions, log hygiene, and T3 Code binding.
- `agy-auth-shield status` human-readable dashboard (with `--json` for scripting), `logs -f`, and `upgrade`.
- Cross-platform Linux support (`systemd --user` service, portable SHA-256 hashing, Linux CA bundle paths).
- Non-secret `run/scopes.json` (`0600`) persistence so active `agy` / `agy acp` sessions retain probe metadata seamlessly across daemon restarts and upgrades.
- Automated offline test suite (`tests/test_daemon.py` and `tests/test_lifecycle.py`) and GitHub Actions CI matrix across macOS and Ubuntu.

### Changed
- Standardized LaunchAgent label to `io.github.zs-andy.agy-auth-shield` with automatic migration and cleanup of legacy `com.*.agy-auth-shield.plist` files.
- Made `enable-t3` idempotent (no-op if already bound) and stored settings backups inside `$SHIELD_HOME/backups/` instead of `~/.t3/userdata/`.
- Improved chunked request body trailer/CRLF parsing for bare-LF clients.

## [1.0.0] - 2026-11-24

### Added
- Initial macOS loopback HTTP/1.1 gateway (`agy_auth_shield.py`) with proactive stale-token swap, Tier-1 RAM cache replay, and Tier-2 singleflight on-demand `agy` probe healing.
- Non-invasive `agy` wrapper enforcing `GODEBUG=http2client=0` and fail-open behavior.
- Basic LaunchAgent and management CLI.
