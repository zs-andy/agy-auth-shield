#!/usr/bin/env python3
"""End-to-end installer, CLI, wrapper, and ZERO-RESIDUE uninstaller lifecycle tests.

Runs entirely inside an isolated temporary $HOME directory; never modifies the real user's files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"
UNINSTALL_SH = REPO_ROOT / "uninstall.sh"
BASH_BIN = "/bin/bash" if Path("/bin/bash").is_file() else "bash"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agy-shield-lifecycle-"))
        self.fake_home = self.tmp / "home"
        self.fake_home.mkdir()
        self.port = free_port()
        self.shield_home = self.fake_home / ".local" / "share" / "agy-auth-shield"
        self.t3_settings = self.fake_home / ".t3" / "userdata" / "settings.json"

        # Seed fake real `agy` binary in ~/.local/bin/agy
        fake_local_bin = self.fake_home / ".local" / "bin"
        fake_local_bin.mkdir(parents=True)
        self.fake_agy = fake_local_bin / "agy"
        self.fake_agy.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "${1:-}" == "--version" ]]; then echo "agy 9.9.9-test"; exit 0; fi\n'
            'printf "REAL_AGY_CALLED GODEBUG=%s CLOUD_CODE_URL=%s NO_PROXY=%s ARGS=%s\\n" '
            '"${GODEBUG:-}" "${CLOUD_CODE_URL:-}" "${NO_PROXY:-}" "$*"\n',
            encoding="utf-8",
        )
        self.fake_agy.chmod(0o755)

        # Seed realistic shell RC files including a legacy v1.0.0 block in .zshrc
        self.orig_zshrc = (
            "# User zshrc header\n"
            'export EDITOR="vim"\n\n'
            "# Added by Antigravity CLI installer\n"
            'export PATH="$HOME/.local/bin:$PATH"\n'
        )
        legacy_zshrc = (
            self.orig_zshrc
            + "\n# Added by agy-auth-shield:\n"
            + "# Legacy v1.0.0 comment\n"
            + f'export PATH="{self.shield_home}/bin:$PATH"\n'
        )
        (self.fake_home / ".zshrc").write_text(legacy_zshrc, encoding="utf-8")

        self.orig_zprofile = 'export LANG="en_US.UTF-8"\n'
        (self.fake_home / ".zprofile").write_text(self.orig_zprofile, encoding="utf-8")

        self.orig_bashrc = "# User bashrc\nexport FOO=bar\n"
        (self.fake_home / ".bashrc").write_text(self.orig_bashrc, encoding="utf-8")

        fish_dir = self.fake_home / ".config" / "fish"
        fish_dir.mkdir(parents=True)
        self.orig_fish = "# User fish config\nset -gx EDITOR vim\n"
        (fish_dir / "config.fish").write_text(self.orig_fish, encoding="utf-8")

        # Seed fake T3 Code settings.json
        self.t3_settings.parent.mkdir(parents=True)
        self.orig_t3 = {
            "theme": "dark",
            "providerInstances": {
                "antigravity": {"driver": "antigravity", "enabled": True},
                "claude": {"driver": "claude", "enabled": True},
            },
        }
        self.t3_settings.write_text(json.dumps(self.orig_t3, indent=2) + "\n", encoding="utf-8")
        self.t3_settings.chmod(0o600)

    def tearDown(self) -> None:
        # Ensure no background daemon survives if a test fails mid-way
        cli = self.shield_home / "bin" / "agy-auth-shield"
        if cli.is_file():
            subprocess.run([BASH_BIN, str(cli), "stop"], env=self._env(), check=False)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self, include_shield_in_path: bool = False) -> dict:
        env = dict(os.environ)
        for key in (
            "CLOUD_CODE_URL",
            "GEMINI_HOME",
            "SSH_CONNECTION",
            "AGY_ACP_FORCE_FILE_STORAGE",
            "AGY_AUTH_SHIELD_DISABLE",
            "AGY_AUTH_SHIELD_REAL_AGY",
            "AGY_AUTH_SHIELD_NO_AUTOSTART",
            "AGY_AUTH_SHIELD_LABEL",
        ):
            env.pop(key, None)
        env["HOME"] = str(self.fake_home)
        env["SHELL"] = "/bin/zsh"
        env["NO_COLOR"] = "1"
        env["AGY_AUTH_SHIELD_HOME"] = str(self.shield_home)
        env["AGY_AUTH_SHIELD_PORT"] = str(self.port)
        env["AGY_AUTH_SHIELD_T3_SETTINGS"] = str(self.t3_settings)
        env["XDG_CONFIG_HOME"] = str(self.fake_home / ".config")
        path_parts = []
        if include_shield_in_path:
            path_parts.append(str(self.shield_home / "bin"))
        path_parts.append(str(self.fake_home / ".local" / "bin"))
        path_parts.append(env.get("PATH", "/usr/bin:/bin"))
        env["PATH"] = ":".join(path_parts)
        return env

    def _run(self, args, include_shield_in_path: bool = False, check: bool = True):
        return subprocess.run(
            args,
            env=self._env(include_shield_in_path=include_shield_in_path),
            capture_output=True,
            text=True,
            timeout=30,
            check=check,
        )

    def test_dry_run_makes_no_changes(self) -> None:
        res = self._run([BASH_BIN, str(INSTALL_SH), "--dry-run", "--no-service"])
        self.assertIn("[dry-run]", res.stdout)
        self.assertFalse(self.shield_home.exists())

    def test_full_install_upgrade_cli_and_zero_residue_uninstall(self) -> None:
        # 1. Install (--no-service avoids touching host launchctl/systemd)
        res = self._run([BASH_BIN, str(INSTALL_SH), "--no-service", "--yes"])
        self.assertIn("Installation complete", res.stdout)
        self.assertTrue((self.shield_home / "agy_auth_shield.py").is_file())
        self.assertTrue((self.shield_home / "bin" / "agy").is_file())
        self.assertTrue((self.shield_home / "bin" / "agy-auth-shield").is_file())
        self.assertTrue((self.shield_home / ".agy-auth-shield-managed").is_file())

        # Verify legacy v1.0.0 block was migrated cleanly (exactly 1 sentinel block, 0 legacy headers)
        zshrc_after = (self.fake_home / ".zshrc").read_text(encoding="utf-8")
        self.assertNotIn("# Added by agy-auth-shield:", zshrc_after)
        self.assertEqual(zshrc_after.count("# >>> agy-auth-shield initialize >>>"), 1)
        self.assertEqual(zshrc_after.count("# <<< agy-auth-shield initialize <<<"), 1)

        fish_after = (self.fake_home / ".config" / "fish" / "config.fish").read_text(
            encoding="utf-8"
        )
        self.assertIn("fish_add_path -p", fish_after)

        # 2. Idempotent re-install produces zero duplicates
        self._run([BASH_BIN, str(INSTALL_SH), "--no-service", "--yes"])
        zshrc_re = (self.fake_home / ".zshrc").read_text(encoding="utf-8")
        self.assertEqual(zshrc_re.count("# >>> agy-auth-shield initialize >>>"), 1)

        # 3. Start daemon, verify status / status --json / doctor / version / wrapper
        cli = str(self.shield_home / "bin" / "agy-auth-shield")
        wrapper = str(self.shield_home / "bin" / "agy")

        # Start directly via daemon launcher (since --no-service skipped plist)
        launcher = str(self.shield_home / "bin" / "agy-auth-shield-daemon")
        proc = subprocess.Popen(
            [BASH_BIN, launcher],
            env=self._env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Wait via wrapper auto-detect / status
            res_st = None
            for _ in range(50):
                res_st = self._run([BASH_BIN, cli, "status", "--json"], check=False)
                if res_st.returncode == 0:
                    break
                time.sleep(0.15)
            assert res_st is not None and res_st.returncode == 0, (
                f"rc={res_st.returncode if res_st else None} stdout={res_st.stdout if res_st else ''} "
                f"stderr={res_st.stderr if res_st else ''}"
            )
            st_obj = json.loads(res_st.stdout)
            self.assertTrue(st_obj["ok"])
            self.assertEqual(st_obj["port"], self.port)

            res_human = self._run([BASH_BIN, cli, "status"], include_shield_in_path=True)
            self.assertIn("Daemon:    running", res_human.stdout)

            res_doc = self._run([BASH_BIN, cli, "doctor"], include_shield_in_path=True)
            self.assertIn("PATH resolves 'agy' to shield wrapper", res_doc.stdout)
            self.assertIn("Loopback daemon healthy", res_doc.stdout)

            # Wrapper routes CLI call through shield & sets GODEBUG + NO_PROXY
            res_wrap = self._run([BASH_BIN, wrapper, "models"], include_shield_in_path=True)
            self.assertIn("GODEBUG=http2client=0", res_wrap.stdout)
            self.assertIn(f"CLOUD_CODE_URL=http://127.0.0.1:{self.port}/scope/cli-", res_wrap.stdout)
            self.assertIn("127.0.0.1", res_wrap.stdout)

            # Wrapper fail-open when daemon unreachable
            fo_env = self._env(include_shield_in_path=True)
            fo_env["AGY_AUTH_SHIELD_PORT"] = str(free_port())
            fo_env["AGY_AUTH_SHIELD_NO_AUTOSTART"] = "1"
            res_fo = subprocess.run(
                [BASH_BIN, wrapper, "models"],
                env=fo_env,
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            self.assertIn("REAL_AGY_CALLED", res_fo.stdout)
            self.assertIn("CLOUD_CODE_URL= ", res_fo.stdout)

            # 4. enable-t3 & idempotency check
            res_t3 = self._run([BASH_BIN, cli, "enable-t3", "--yes"])
            self.assertIn("Updated T3 Code settings", res_t3.stdout)
            backups_before = list((self.shield_home / "backups").glob("*.json"))
            self.assertEqual(len(backups_before), 1)
            res_t3_again = self._run([BASH_BIN, cli, "enable-t3", "--yes"])
            self.assertIn("already set", res_t3_again.stdout)
            backups_after = list((self.shield_home / "backups").glob("*.json"))
            self.assertEqual(len(backups_after), 1)

            # 5. COMPLETE ZERO-RESIDUE UNINSTALL
            res_un = self._run([BASH_BIN, cli, "uninstall", "--yes"])
            self.assertIn("completely uninstalled", res_un.stdout)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=3)

        # Verify ZERO RESIDUE!
        self.assertFalse(self.shield_home.exists(), "SHIELD_HOME was not completely deleted!")

        # Shell RC files must have zero agy-auth-shield lines & preserve original user content
        for rc_name, expected in (
            (".zshrc", self.orig_zshrc),
            (".zprofile", self.orig_zprofile),
            (".bashrc", self.orig_bashrc),
        ):
            actual = (self.fake_home / rc_name).read_text(encoding="utf-8")
            self.assertNotIn("agy-auth-shield", actual, f"Leftover in {rc_name}: {actual!r}")
            self.assertEqual(actual.strip(), expected.strip(), f"Corrupted {rc_name}")

        fish_final = (self.fake_home / ".config" / "fish" / "config.fish").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("agy-auth-shield", fish_final)
        self.assertEqual(fish_final.strip(), self.orig_fish.strip())

        # T3 Code settings.json must have binaryPath cleanly removed while keeping all other keys
        t3_final = json.loads(self.t3_settings.read_text(encoding="utf-8"))
        self.assertEqual(t3_final, self.orig_t3)

        # Daemon port must be closed
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            self.assertNotEqual(s.connect_ex(("127.0.0.1", self.port)), 0)

    def test_standalone_uninstall_sh_and_custom_t3_preserved(self) -> None:
        self._run([BASH_BIN, str(INSTALL_SH), "--no-service", "--yes"])

        # Put a user's CUSTOM non-shield binaryPath in T3 settings; uninstall must NOT clobber it!
        custom_t3 = json.loads(self.t3_settings.read_text(encoding="utf-8"))
        custom_t3["providerInstances"]["antigravity"]["config"] = {"binaryPath": "/custom/my-agy"}
        self.t3_settings.write_text(json.dumps(custom_t3, indent=2) + "\n", encoding="utf-8")

        res = self._run([BASH_BIN, str(UNINSTALL_SH), "--yes"])
        self.assertEqual(res.returncode, 0)
        self.assertFalse(self.shield_home.exists())
        after_t3 = json.loads(self.t3_settings.read_text(encoding="utf-8"))
        self.assertEqual(
            after_t3["providerInstances"]["antigravity"]["config"]["binaryPath"],
            "/custom/my-agy",
        )

    def test_uninstall_keep_logs_option(self) -> None:
        self._run([BASH_BIN, str(INSTALL_SH), "--no-service", "--yes"])
        (self.shield_home / "shield.log").write_text("sample log line\n", encoding="utf-8")
        cli = str(self.shield_home / "bin" / "agy-auth-shield")
        self._run([BASH_BIN, cli, "uninstall", "--yes", "--keep-logs"])
        self.assertTrue((self.shield_home / "shield.log").is_file())
        self.assertFalse((self.shield_home / "bin").exists())
        self.assertFalse((self.shield_home / "agy_auth_shield.py").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
