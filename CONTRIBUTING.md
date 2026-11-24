# Contributing to agy-auth-shield

Thank you for your interest in improving `agy-auth-shield`!

## Design Principles

- **Zero external dependencies:** Core daemon uses Python 3.9+ standard library only; wrapper and scripts must stay compatible with macOS Bash 3.2 and Linux Bash.
- **Zero credential persistence:** Never write Bearer tokens or Authorization headers to disk or logs.
- **Zero-residue uninstall:** Any file, service unit, or shell config line added by `install.sh` must be cleanly removed by `agy-auth-shield uninstall` and `uninstall.sh`, with automated assertions in `tests/test_lifecycle.py`.
- **Fail-open:** Wrapper changes must never block `agy` if the daemon is down or unreachable.

## Local Development & Testing

```bash
# 1. Check Bash syntax (uses /bin/bash to catch Bash 3.2 issues on macOS)
bash -n install.sh uninstall.sh src/bin/agy src/bin/agy-auth-shield src/bin/agy-auth-shield-daemon

# 2. Optional ShellCheck
shellcheck -S warning install.sh uninstall.sh src/bin/agy src/bin/agy-auth-shield src/bin/agy-auth-shield-daemon

# 3. Run full offline test suite (daemon + isolated fake-$HOME install/uninstall tests)
python3 -m unittest discover -s tests -p "test_*.py" -v
```

Pull requests should pass CI on both `ubuntu-latest` and `macos-latest`.
