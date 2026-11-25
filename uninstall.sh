#!/usr/bin/env bash
# Standalone zero-residue uninstaller for agy-auth-shield (macOS & Linux).
#
# Can be run locally or directly via curl:
#   curl -fsSL https://raw.githubusercontent.com/zs-andy/agy-auth-shield/main/uninstall.sh | bash
#   ./uninstall.sh --yes

set -euo pipefail

SHIELD_HOME="${AGY_AUTH_SHIELD_HOME:-$HOME/.local/share/agy-auth-shield}"
SERVICE_LABEL="${AGY_AUTH_SHIELD_LABEL:-io.github.zs-andy.agy-auth-shield}"
T3_SETTINGS="${AGY_AUTH_SHIELD_T3_SETTINGS:-$HOME/.t3/userdata/settings.json}"
OPT_YES=0
OPT_KEEP_LOGS=0
OPT_KEEP_T3=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)
      shift; SHIELD_HOME="${1:-}"
      ;;
    -y|--yes) OPT_YES=1 ;;
    --keep-logs) OPT_KEEP_LOGS=1 ;;
    --keep-t3) OPT_KEEP_T3=1 ;;
    -h|--help)
      cat <<'EOF'
Completely uninstall agy-auth-shield with zero residue.

Usage:
  uninstall.sh [--dir <path>] [-y|--yes] [--keep-logs] [--keep-t3]
EOF
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# If the installed CLI exists, delegate to it (passing through flags) so behavior stays 100% unified.
CLI_BIN="${SHIELD_HOME}/bin/agy-auth-shield"
if [[ -x "$CLI_BIN" ]]; then
  ARGS=()
  [[ "$OPT_YES" == "1" ]] && ARGS+=(--yes)
  [[ "$OPT_KEEP_LOGS" == "1" ]] && ARGS+=(--keep-logs)
  [[ "$OPT_KEEP_T3" == "1" ]] && ARGS+=(--keep-t3)
  # If invoked via non-interactive pipe (curl | bash), default to --yes
  if [[ ! -t 0 && "$OPT_YES" != "1" ]]; then
    ARGS+=(--yes)
  fi
  export AGY_AUTH_SHIELD_HOME="$SHIELD_HOME"
  export AGY_AUTH_SHIELD_T3_SETTINGS="$T3_SETTINGS"
  exec "$CLI_BIN" uninstall ${ARGS[@]+"${ARGS[@]}"}
fi

# Fallback standalone cleanup if SHIELD_HOME/bin/agy-auth-shield was already deleted manually
OS_NAME="$(uname -s 2>/dev/null || printf 'unknown')"
UID_NUM="$(id -u)"
PYTHON_BIN="$(command -v python3 2>/dev/null || printf '%s' '/usr/bin/python3')"

if [[ "${AGY_AUTH_SHIELD_NO_SERVICE:-0}" != "1" ]]; then
  if [[ "$OS_NAME" == "Darwin" ]]; then
    PLIST_DIR="$HOME/Library/LaunchAgents"
    PLIST_PATH="${PLIST_DIR}/${SERVICE_LABEL}.plist"
    _plist_owned=0
    if [[ -f "$PLIST_PATH" ]] && (grep -Fq "<string>${SHIELD_HOME}</string>" "$PLIST_PATH" || grep -Fq "<string>${SHIELD_HOME}/" "$PLIST_PATH"); then
      _plist_owned=1
    fi
    _job_out="$(/bin/launchctl print "gui/${UID_NUM}/${SERVICE_LABEL}" 2>/dev/null || true)"
    if [[ "$_plist_owned" == "1" || "$_job_out" == *"${SHIELD_HOME}/"* || "$_job_out" == *"AGY_AUTH_SHIELD_HOME => ${SHIELD_HOME}"* ]]; then
      /bin/launchctl bootout "gui/${UID_NUM}/${SERVICE_LABEL}" >/dev/null 2>&1 || true
    fi
    if [[ "$_plist_owned" == "1" ]]; then
      rm -f "$PLIST_PATH"
    fi
    for legacy in "$PLIST_DIR"/com.*.agy-auth-shield.plist; do
      if [[ -f "$legacy" ]] && (grep -Fq "<string>${SHIELD_HOME}</string>" "$legacy" || grep -Fq "<string>${SHIELD_HOME}/" "$legacy"); then
        /bin/launchctl bootout "gui/${UID_NUM}/$(basename "$legacy" .plist)" >/dev/null 2>&1 || true
        rm -f "$legacy"
      fi
    done
  elif [[ "$OS_NAME" == "Linux" ]] && command -v systemctl >/dev/null 2>&1; then
    UNIT_PATH="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/agy-auth-shield.service"
    _unit_owned=0
    if [[ -f "$UNIT_PATH" ]]; then
      if grep -Fxq "Environment=AGY_AUTH_SHIELD_HOME=${SHIELD_HOME}" "$UNIT_PATH" || grep -Fq "ExecStart=${SHIELD_HOME}/" "$UNIT_PATH"; then
        _unit_owned=1
      fi
    else
      _show_out="$(systemctl --user show -p ExecStart -p Environment agy-auth-shield.service 2>/dev/null || true)"
      if [[ "$_show_out" == *"${SHIELD_HOME}/"* || "$_show_out" == *"AGY_AUTH_SHIELD_HOME=${SHIELD_HOME}"* ]]; then
        _unit_owned=1
      fi
    fi
    if [[ "$_unit_owned" == "1" ]]; then
      systemctl --user disable --now agy-auth-shield.service >/dev/null 2>&1 || true
      rm -f "$UNIT_PATH"
      systemctl --user daemon-reload >/dev/null 2>&1 || true
    fi
  fi
fi

if command -v "$PYTHON_BIN" >/dev/null 2>&1 || [[ -x "$PYTHON_BIN" ]]; then
  "$PYTHON_BIN" - "$HOME" "$SHIELD_HOME" "$T3_SETTINGS" "$OPT_KEEP_T3" <<'PY'
import json, os, stat, sys, tempfile
from pathlib import Path

home = Path(sys.argv[1])
shield_home = sys.argv[2]
t3_settings = Path(sys.argv[3])
keep_t3 = sys.argv[4] == "1"

START_MARK = "# >>> agy-auth-shield initialize >>>"
END_MARK = "# <<< agy-auth-shield initialize <<<"

def clean_rc(rc: Path):
    if not rc.is_file():
        return
    orig = rc.read_text(encoding="utf-8")
    if "agy-auth-shield" not in orig and shield_home not in orig:
        return
    lines = orig.splitlines(keepends=True)
    out, i, n = [], 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if s == START_MARK:
            i += 1
            while i < n and lines[i].strip() != END_MARK:
                i += 1
            if i < n and lines[i].strip() == END_MARK:
                i += 1
            if out and out[-1].strip() == "":
                out.pop()
            continue
        if s.startswith("# Added by agy-auth-shield"):
            j = i + 1
            while j < n and lines[j].strip().startswith("#"):
                j += 1
            if j < n and ("agy-auth-shield" in lines[j] or shield_home in lines[j]):
                i = j + 1
                if out and out[-1].strip() == "":
                    out.pop()
                continue
        if ("agy-auth-shield/bin" in lines[i] or f"{shield_home}/bin" in lines[i]) and (
            "PATH" in lines[i] or "fish_add_path" in lines[i]
        ):
            i += 1
            continue
        out.append(lines[i])
        i += 1
    while len(out) >= 2 and out[-1].strip() == "" and out[-2].strip() == "":
        out.pop()
    new_text = "".join(out)
    if new_text != orig:
        real_rc = rc.resolve()
        mode = stat.S_IMODE(real_rc.stat().st_mode)
        fd, tmp = tempfile.mkstemp(prefix=".rc.", suffix=".tmp", dir=str(real_rc.parent))
        try:
            os.chmod(tmp, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_text)
            os.replace(tmp, str(real_rc))
        except Exception:
            os.unlink(tmp)
            raise

for p in (
    home / ".zshrc", home / ".zprofile", home / ".zshenv",
    home / ".bashrc", home / ".bash_profile", home / ".profile",
    home / ".config" / "fish" / "config.fish",
):
    clean_rc(p)

if not keep_t3 and t3_settings.is_file():
    try:
        data = json.loads(t3_settings.read_text(encoding="utf-8"))
        ag = (data.get("providerInstances") or {}).get("antigravity")
        if isinstance(ag, dict) and isinstance(ag.get("config"), dict):
            bp = str(ag["config"].get("binaryPath", ""))
            if "agy-auth-shield" in bp:
                ag["config"].pop("binaryPath", None)
                if not ag["config"]:
                    ag.pop("config", None)
                mode = stat.S_IMODE(t3_settings.stat().st_mode)
                fd, tmp = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=str(t3_settings.parent))
                try:
                    os.chmod(tmp, mode)
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2)
                        f.write("\n")
                    os.replace(tmp, str(t3_settings))
                except Exception:
                    os.unlink(tmp)
                    raise
    except Exception:
        pass
PY
fi

if [[ -n "$SHIELD_HOME" && "$SHIELD_HOME" != "/" && "$SHIELD_HOME" != "$HOME" && -d "$SHIELD_HOME" ]]; then
  if [[ -f "${SHIELD_HOME}/.agy-auth-shield-managed" || -f "${SHIELD_HOME}/agy_auth_shield.py" ]]; then
    if [[ "$OPT_KEEP_LOGS" == "1" ]]; then
      rm -rf "${SHIELD_HOME:?}/bin" "${SHIELD_HOME:?}/run" "${SHIELD_HOME:?}/__pycache__" \
             "${SHIELD_HOME:?}/backups" "${SHIELD_HOME:?}/agy_auth_shield.py" \
             "${SHIELD_HOME:?}/VERSION" "${SHIELD_HOME:?}/config.env" \
             "${SHIELD_HOME:?}/.agy-auth-shield-managed"
    else
      rm -rf "$SHIELD_HOME"
    fi
  fi
fi

echo "✓ agy-auth-shield cleanup complete."
