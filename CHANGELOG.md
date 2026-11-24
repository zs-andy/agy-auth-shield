# Changelog

All notable changes to `agy-auth-shield` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
