#!/usr/bin/env bash
# Production installer for agy-auth-shield (macOS & Linux).
#
# One-line remote install:
#   curl -fsSL https://raw.githubusercontent.com/zs-andy/agy-auth-shield/main/install.sh | bash
#
# Local checkout install:
#   ./install.sh [options]

set -euo pipefail

REPO_SLUG="${AGY_AUTH_SHIELD_REPO:-zs-andy/agy-auth-shield}"
REF="${AGY_AUTH_SHIELD_REF:-main}"
SHIELD_HOME="${AGY_AUTH_SHIELD_HOME:-$HOME/.local/share/agy-auth-shield}"
SHIELD_PORT="${AGY_AUTH_SHIELD_PORT:-18765}"
SERVICE_LABEL="${AGY_AUTH_SHIELD_LABEL:-io.github.zs-andy.agy-auth-shield}"
T3_SETTINGS="${AGY_AUTH_SHIELD_T3_SETTINGS:-$HOME/.t3/userdata/settings.json}"

OPT_ENABLE_T3=0
OPT_NO_PATH=0
OPT_NO_SERVICE=0
OPT_DRY_RUN=0
OPT_YES=0

OS_NAME="$(uname -s 2>/dev/null || printf 'unknown')"
UID_NUM="$(id -u)"
CURL_BIN="$(command -v curl 2>/dev/null || printf '%s' '/usr/bin/curl')"
PYTHON_BIN="$(command -v python3 2>/dev/null || printf '%s' '/usr/bin/python3')"

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  C_RESET=$'\033[0m'
  C_BOLD=$'\033[1m'
  C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'
  C_RED=$'\033[31m'
  C_CYAN=$'\033[36m'
else
  C_RESET=""; C_BOLD=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_CYAN=""
fi

ok()   { printf '%s✓%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%s!%s %s\n' "$C_YELLOW" "$C_RESET" "$*"; }
err()  { printf '%s✗%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }
info() { printf '%s·%s %s\n' "$C_CYAN" "$C_RESET" "$*"; }

usage() {
  cat <<EOF
Install agy-auth-shield — Transparent OAuth 401 auto-healing gateway for Antigravity CLI & T3 Code

Usage:
  install.sh [options]

Options:
  --dir <path>      Install directory (default: ~/.local/share/agy-auth-shield)
  --port <port>     Loopback daemon port, 1024-65535 (default: 18765)
  --enable-t3       Also bind T3 Code settings.json binaryPath (run when T3 threads are idle)
  --no-path         Do not modify shell RC files
  --no-service      Install files only; do not register LaunchAgent / systemd service
  --ref <git-ref>   Git branch, tag, or commit when installing remotely (default: main)
  --dry-run         Show what would be done without modifying anything
  -y, --yes         Non-interactive mode
  -h, --help        Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)
      shift; SHIELD_HOME="${1:-}"; [[ -n "$SHIELD_HOME" ]] || { err "--dir requires a path"; exit 2; }
      ;;
    --port)
      shift; SHIELD_PORT="${1:-}"
      ;;
    --enable-t3) OPT_ENABLE_T3=1 ;;
    --no-path) OPT_NO_PATH=1 ;;
    --no-service) OPT_NO_SERVICE=1 ;;
    --ref)
      shift; REF="${1:-}"; [[ -n "$REF" ]] || { err "--ref requires a value"; exit 2; }
      ;;
    --dry-run) OPT_DRY_RUN=1 ;;
    -y|--yes) OPT_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) err "Unknown option: $1"; usage >&2; exit 2 ;;
  esac
  shift
done

if ! [[ "$SHIELD_PORT" =~ ^[0-9]+$ ]] || (( SHIELD_PORT < 1024 || SHIELD_PORT > 65535 )); then
  err "Invalid --port '${SHIELD_PORT}': expected integer between 1024 and 65535"
  exit 2
fi

case "$OS_NAME" in
  Darwin|Linux) ;;
  *)
    err "Unsupported OS: ${OS_NAME} (agy-auth-shield supports macOS and Linux)"
    exit 1
    ;;
esac

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1 && [[ ! -x "$PYTHON_BIN" ]]; then
  err "python3 is required (3.9+ with ssl module)"
  exit 1
fi

if ! "$PYTHON_BIN" -c 'import ssl, sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
  # Check common newer Python paths on macOS
  _found_py=""
  for _cand in \
    "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3" \
    "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3" \
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3" \
    "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3" \
    "/opt/homebrew/bin/python3" \
    "/usr/local/bin/python3"; do
    if [[ -x "$_cand" ]] && "$_cand" -c 'import ssl, sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
      _found_py="$_cand"
      break
    fi
  done
  if [[ -n "$_found_py" ]]; then
    PYTHON_BIN="$_found_py"
  else
    err "Python 3.9+ with ssl support is required (found older python3)"
    exit 1
  fi
fi

# Determine source directory: local checkout or remote archive download
SCRIPT_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
fi

SRC_ROOT=""
TMP_DOWNLOAD_DIR=""
cleanup_tmp() {
  if [[ -n "${TMP_DOWNLOAD_DIR:-}" && -d "${TMP_DOWNLOAD_DIR:-}" ]]; then
    rm -rf "$TMP_DOWNLOAD_DIR"
  fi
}
trap cleanup_tmp EXIT

if [[ -n "$SCRIPT_DIR" && -f "${SCRIPT_DIR}/src/agy_auth_shield.py" && -f "${SCRIPT_DIR}/src/bin/agy" ]]; then
  SRC_ROOT="$SCRIPT_DIR"
else
  if [[ "$OPT_DRY_RUN" == "1" ]]; then
    info "[dry-run] Would download https://github.com/${REPO_SLUG}/archive/${REF}.tar.gz"
  else
    TMP_DOWNLOAD_DIR="$(mktemp -d -t agy-auth-shield-install.XXXXXX)"
    ARCHIVE_URL="https://github.com/${REPO_SLUG}/archive/${REF}.tar.gz"
    info "Downloading ${REPO_SLUG}@${REF} ..."
    "$CURL_BIN" -fsSL "$ARCHIVE_URL" | tar -xz -C "$TMP_DOWNLOAD_DIR"
    SRC_ROOT="$(find "$TMP_DOWNLOAD_DIR" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
    if [[ -z "$SRC_ROOT" || ! -f "${SRC_ROOT}/src/agy_auth_shield.py" ]]; then
      err "Downloaded archive did not contain expected src/agy_auth_shield.py"
      exit 1
    fi
  fi
fi

INSTALL_VERSION="1.1.0"
if [[ -n "$SRC_ROOT" && -r "${SRC_ROOT}/VERSION" ]]; then
  INSTALL_VERSION="$(tr -d '\r\n' < "${SRC_ROOT}/VERSION")"
fi

printf '%sInstalling agy-auth-shield%s v%s\n' "$C_BOLD" "$C_RESET" "$INSTALL_VERSION"
printf '  Target directory: %s\n' "$SHIELD_HOME"
printf '  Loopback port:    127.0.0.1:%s\n' "$SHIELD_PORT"
printf '  Service label:    %s\n\n' "$SERVICE_LABEL"

if [[ "$OPT_DRY_RUN" == "1" ]]; then
  info "[dry-run] Would install daemon and CLI scripts into ${SHIELD_HOME} (0700)"
  if [[ "$OPT_NO_PATH" != "1" ]]; then
    info "[dry-run] Would add managed PATH block to existing shell RC files"
  fi
  if [[ "$OPT_NO_SERVICE" != "1" ]]; then
    if [[ "$OS_NAME" == "Darwin" ]]; then
      info "[dry-run] Would install LaunchAgent ~/Library/LaunchAgents/${SERVICE_LABEL}.plist and migrate any legacy plist"
    else
      info "[dry-run] Would install systemd user service ~/.config/systemd/user/agy-auth-shield.service"
    fi
  fi
  if [[ "$OPT_ENABLE_T3" == "1" ]]; then
    info "[dry-run] Would run agy-auth-shield enable-t3 --yes"
  fi
  ok "[dry-run] Complete — no files were modified."
  exit 0
fi

# 1. Create directories with strict permissions (preserving existing run/control.token on upgrade)
mkdir -p "${SHIELD_HOME}/bin" "${SHIELD_HOME}/run"
chmod 700 "${SHIELD_HOME}" "${SHIELD_HOME}/run"

# 2. Install payload files atomically
install_file() {
  local src="$1" dst="$2" mode="$3"
  local tmp="${dst}.tmp.$$"
  cp "$src" "$tmp"
  chmod "$mode" "$tmp"
  mv -f "$tmp" "$dst"
}

install_file "${SRC_ROOT}/src/agy_auth_shield.py" "${SHIELD_HOME}/agy_auth_shield.py" 0755
install_file "${SRC_ROOT}/src/bin/agy" "${SHIELD_HOME}/bin/agy" 0755
install_file "${SRC_ROOT}/src/bin/agy-auth-shield" "${SHIELD_HOME}/bin/agy-auth-shield" 0755
install_file "${SRC_ROOT}/src/bin/agy-auth-shield-daemon" "${SHIELD_HOME}/bin/agy-auth-shield-daemon" 0755
if [[ -f "${SRC_ROOT}/VERSION" ]]; then
  install_file "${SRC_ROOT}/VERSION" "${SHIELD_HOME}/VERSION" 0644
else
  printf '%s\n' "$INSTALL_VERSION" > "${SHIELD_HOME}/VERSION"
fi

cat > "${SHIELD_HOME}/config.env" <<EOF
# Managed by agy-auth-shield installer
AGY_AUTH_SHIELD_PORT=${SHIELD_PORT}
AGY_AUTH_SHIELD_LABEL=${SERVICE_LABEL}
EOF
chmod 600 "${SHIELD_HOME}/config.env"

cat > "${SHIELD_HOME}/.agy-auth-shield-managed" <<EOF
version=${INSTALL_VERSION}
installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
port=${SHIELD_PORT}
label=${SERVICE_LABEL}
EOF
chmod 600 "${SHIELD_HOME}/.agy-auth-shield-managed"

ok "Installed daemon and CLI scripts to ${SHIELD_HOME}"

# 3. Configure shell PATH with managed sentinel markers (migrating legacy v1.0.0 blocks cleanly)
configure_shell_rc() {
  "$PYTHON_BIN" - "$HOME" "$SHIELD_HOME" <<'PY'
import os, stat, sys, tempfile
from pathlib import Path

home = Path(sys.argv[1])
shield_home = sys.argv[2]
bin_dir = f"{shield_home}/bin"
shell_name = Path(os.environ.get("SHELL", "")).name

START_MARK = "# >>> agy-auth-shield initialize >>>"
END_MARK = "# <<< agy-auth-shield initialize <<<"

def strip_existing(lines):
    out = []
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if stripped == START_MARK:
            i += 1
            while i < n and lines[i].strip() != END_MARK:
                i += 1
            if i < n and lines[i].strip() == END_MARK:
                i += 1
            if out and out[-1].strip() == "":
                out.pop()
            continue
        if stripped.startswith("# Added by agy-auth-shield"):
            j = i + 1
            while j < n and lines[j].strip().startswith("#"):
                j += 1
            if j < n and ("agy-auth-shield" in lines[j] or shield_home in lines[j]):
                i = j + 1
                if out and out[-1].strip() == "":
                    out.pop()
                continue
        if ("agy-auth-shield/bin" in lines[i] or bin_dir in lines[i]) and (
            "PATH" in lines[i] or "fish_add_path" in lines[i]
        ):
            i += 1
            continue
        out.append(lines[i])
        i += 1
    while len(out) >= 2 and out[-1].strip() == "" and out[-2].strip() == "":
        out.pop()
    return out

def block_for(rc_path: Path) -> str:
    if rc_path.name == "config.fish":
        return (
            f"{START_MARK}\n"
            f'fish_add_path -p "{bin_dir}"\n'
            f"{END_MARK}\n"
        )
    return (
        f"{START_MARK}\n"
        f'export PATH="{bin_dir}:$PATH"\n'
        f"{END_MARK}\n"
    )

targets = []
# Always update existing rc files for zsh/bash/fish, plus create default rc for current shell if absent
candidates = [
    home / ".zshrc",
    home / ".zprofile",
    home / ".bashrc",
    home / ".bash_profile",
    home / ".config" / "fish" / "config.fish",
]
for c in candidates:
    if c.is_file():
        targets.append(c)

if shell_name == "zsh":
    for req in (home / ".zshrc", home / ".zprofile"):
        if req not in targets:
            targets.append(req)
elif shell_name == "bash":
    req = home / (".bash_profile" if sys.platform == "darwin" else ".bashrc")
    if req not in targets:
        targets.append(req)
elif shell_name == "fish":
    req = home / ".config" / "fish" / "config.fish"
    if req not in targets:
        req.parent.mkdir(parents=True, exist_ok=True)
        targets.append(req)
elif not targets:
    targets.append(home / ".profile")

updated = []
for rc in targets:
    exists = rc.is_file()
    orig_text = rc.read_text(encoding="utf-8") if exists else ""
    cleaned_lines = strip_existing(orig_text.splitlines(keepends=True))
    cleaned_text = "".join(cleaned_lines)
    if cleaned_text and not cleaned_text.endswith("\n"):
        cleaned_text += "\n"
    sep = "\n" if cleaned_text and not cleaned_text.endswith("\n\n") else ""
    new_text = cleaned_text + sep + block_for(rc)
    if new_text != orig_text or not exists:
        real_rc = rc.resolve() if exists else rc
        real_rc.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(real_rc.stat().st_mode) if exists else 0o644
        fd, tmp_path = tempfile.mkstemp(prefix=".rc.", suffix=".tmp", dir=str(real_rc.parent))
        try:
            os.chmod(tmp_path, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_text)
            os.replace(tmp_path, str(real_rc))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        updated.append(str(rc))

for u in updated:
    print(u)
PY
}

if [[ "$OPT_NO_PATH" != "1" ]]; then
  updated_rcs="$(configure_shell_rc || true)"
  if [[ -n "$updated_rcs" ]]; then
    while IFS= read -r _rc; do
      [[ -n "$_rc" ]] && ok "Updated shell PATH: ${_rc}"
    done <<< "$updated_rcs"
  else
    ok "Shell PATH already up to date"
  fi
else
  info "Skipped shell RC updates (--no-path)"
fi

# 4. Install & start background daemon service
install_service_macos() {
  local plist_dir="$HOME/Library/LaunchAgents"
  local plist_path="${plist_dir}/${SERVICE_LABEL}.plist"
  mkdir -p "$plist_dir"

  # Migrate / unload any legacy LaunchAgent (e.g. com.<user>.agy-auth-shield.plist)
  local legacy_plist legacy_label
  for legacy_plist in "$plist_dir"/com.*.agy-auth-shield.plist; do
    if [[ -f "$legacy_plist" ]]; then
      legacy_label="$(basename "$legacy_plist" .plist)"
      /bin/launchctl bootout "gui/${UID_NUM}/${legacy_label}" >/dev/null 2>&1 || true
      rm -f "$legacy_plist"
      ok "Migrated legacy LaunchAgent (${legacy_label} -> ${SERVICE_LABEL})"
    fi
  done

  # Stop current instance cleanly so the new binary & plist take over
  /bin/launchctl bootout "gui/${UID_NUM}/${SERVICE_LABEL}" >/dev/null 2>&1 || true

  cat > "$plist_path" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${SERVICE_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${SHIELD_HOME}/bin/agy-auth-shield-daemon</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>AGY_AUTH_SHIELD_HOME</key>
    <string>${SHIELD_HOME}</string>
    <key>AGY_AUTH_SHIELD_PORT</key>
    <string>${SHIELD_PORT}</string>
    <key>NO_PROXY</key>
    <string>127.0.0.1,localhost</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>5</integer>
  <key>ProcessType</key>
  <string>Background</string>
  <key>StandardOutPath</key>
  <string>${SHIELD_HOME}/launchd.log</string>
  <key>StandardErrorPath</key>
  <string>${SHIELD_HOME}/launchd.log</string>
</dict>
</plist>
EOF
  chmod 644 "$plist_path"

  sleep 0.2
  /bin/launchctl bootstrap "gui/${UID_NUM}" "$plist_path"
  ok "Installed and loaded LaunchAgent: ${plist_path}"
}

install_service_linux() {
  if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    local unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    local unit_path="${unit_dir}/agy-auth-shield.service"
    mkdir -p "$unit_dir"
    cat > "$unit_path" <<EOF
[Unit]
Description=agy-auth-shield local OAuth 401 auto-healing gateway for Antigravity CLI & T3 Code
After=network.target

[Service]
Type=simple
ExecStart=${SHIELD_HOME}/bin/agy-auth-shield-daemon
Environment=AGY_AUTH_SHIELD_HOME=${SHIELD_HOME}
Environment=AGY_AUTH_SHIELD_PORT=${SHIELD_PORT}
Environment=NO_PROXY=127.0.0.1,localhost
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
    chmod 644 "$unit_path"
    systemctl --user daemon-reload
    systemctl --user enable agy-auth-shield.service >/dev/null 2>&1 || true
    systemctl --user restart agy-auth-shield.service
    ok "Installed and started systemd user service: ${unit_path}"
  else
    warn "systemd --user not available; starting daemon directly in background"
    AGY_AUTH_SHIELD_HOME="$SHIELD_HOME" AGY_AUTH_SHIELD_PORT="$SHIELD_PORT" \
      "${SHIELD_HOME}/bin/agy-auth-shield" stop >/dev/null 2>&1 || true
    nohup "${SHIELD_HOME}/bin/agy-auth-shield-daemon" >>"${SHIELD_HOME}/launchd.log" 2>&1 </dev/null &
  fi
}

if [[ "$OPT_NO_SERVICE" != "1" ]]; then
  if [[ "$OS_NAME" == "Darwin" ]]; then
    install_service_macos
  else
    install_service_linux
  fi

  # Wait for daemon health check
  _healthy=0
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    if "$CURL_BIN" -fsS --noproxy '*' --max-time 0.5 "http://127.0.0.1:${SHIELD_PORT}/__healthz" >/dev/null 2>&1; then
      _healthy=1
      break
    fi
    sleep 0.2
  done
  if [[ "$_healthy" == "1" ]]; then
    ok "Daemon is healthy on http://127.0.0.1:${SHIELD_PORT}"
  else
    err "Daemon did not become healthy on http://127.0.0.1:${SHIELD_PORT} within 4s"
    info "Check logs: ${SHIELD_HOME}/launchd.log and ${SHIELD_HOME}/shield.log"
    exit 1
  fi
else
  info "Skipped background service registration (--no-service)"
fi

# 5. Optional T3 Code settings binding
if [[ "$OPT_ENABLE_T3" == "1" ]]; then
  if [[ "$OPT_YES" == "1" || ! -t 0 ]]; then
    AGY_AUTH_SHIELD_HOME="$SHIELD_HOME" AGY_AUTH_SHIELD_PORT="$SHIELD_PORT" \
      "${SHIELD_HOME}/bin/agy-auth-shield" enable-t3 --yes
  else
    AGY_AUTH_SHIELD_HOME="$SHIELD_HOME" AGY_AUTH_SHIELD_PORT="$SHIELD_PORT" \
      "${SHIELD_HOME}/bin/agy-auth-shield" enable-t3
  fi
fi

# 6. Gentle check if real `agy` is installed
if ! command -v agy >/dev/null 2>&1 && [[ ! -x "$HOME/.local/bin/agy" ]]; then
  warn "Antigravity CLI ('agy') was not detected yet — wrapper will pick it up automatically once installed."
fi

printf '\n%sInstallation complete!%s\n\n' "$C_GREEN$C_BOLD" "$C_RESET"
printf 'Next steps:\n'
if [[ "$OPT_NO_PATH" != "1" ]]; then
  printf '  1. Open a new terminal tab (or reload your shell profile) so `agy` uses the shield wrapper.\n'
fi
if [[ "$OPT_ENABLE_T3" != "1" && -f "$T3_SETTINGS" ]]; then
  printf '  2. For T3 Code: restart T3 Code to inherit PATH, or run `agy-auth-shield enable-t3` when idle.\n'
fi
printf '  • Check health:       agy-auth-shield doctor\n'
printf '  • Live stats:         agy-auth-shield status\n'
printf '  • Complete uninstall: agy-auth-shield uninstall\n'
