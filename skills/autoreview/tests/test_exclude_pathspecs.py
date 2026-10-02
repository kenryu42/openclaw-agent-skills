"""`--exclude` leaves committed generated trees out of every review target."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from .test_autoreview_hardening import SCRIPT, git, init_repo, load_helper


class ExcludePathspecTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="autoreview-exclude.")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        home = self.root / "operator"
        home.mkdir()
        env = {key: os.environ[key] for key in (
            "PATH", "PATHEXT", "SYSTEMROOT", "SystemRoot", "COMSPEC", "WINDIR",
            "TEMP", "TMP", "TMPDIR", "DEVELOPER_DIR",
        ) if key in os.environ}
        env.update({
            "HOME": str(home), "USERPROFILE": str(home),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        })
        environment = mock.patch.dict(os.environ, env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.helper = load_helper()
        self.repo = init_repo(self.root)
        (self.repo / "src").mkdir()
        (self.repo / "dist").mkdir()
        (self.repo / "src/a.py").write_text("base()\n")
        (self.repo / "dist/out.js").write_text("base\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").strip()
        (self.repo / "src/a.py").write_text("changed()\n")
        (self.repo / "dist/out.js").write_text("generated\n")
        (self.repo / "dist/font.woff2").write_bytes(b"\0\1binary-generated\0")
        (self.repo / "dist/logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0not-really")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "change")

    def assert_only_source(self, captured, paths=frozenset({"src/a.py"})):
        self.assertIn("excluded pathspecs: dist", captured.text)
        self.assertNotIn("dist/", captured.text)
        self.assertNotIn("generated", captured.text)
        self.assertEqual(captured.paths, set(paths))
        self.assertEqual(captured.images, ())

    def test_committed_targets_omit_excluded_text_and_binary_changes(self):
        build = self.helper["build_bundle"]
        for target in ("branch", "commit"):
            with self.subTest(target=target):
                with self.assertRaisesRegex(SystemExit, "refusing"):
                    build(self.repo, target, self.base, "HEAD")
                captured = build(self.repo, target, self.base, "HEAD", ("dist",))
                self.assertIn("+changed()", captured.text)
                self.assert_only_source(captured)

    def test_local_target_omits_excluded_staged_unstaged_and_untracked_changes(self):
        (self.repo / "src/a.py").write_text("staged()\n")
        (self.repo / "dist/out.js").write_text("staged generated\n")
        (self.repo / "dist/staged.bin").write_bytes(b"\0staged-generated\0")
        git(self.repo, "add", ".")
        (self.repo / "dist/out.js").write_text("dirty generated\n")
        (self.repo / "dist/new.js").write_text("untracked generated\n")
        (self.repo / "src/new.py").write_text("untracked()\n")
        local = self.helper["local_bundle"]
        with self.assertRaisesRegex(SystemExit, "refusing"):
            local(self.repo)
        for base in (None, self.base):
            with self.subTest(base=base):
                captured = local(self.repo, base, ("dist",))
                self.assertIn("+staged()", captured.text)
                self.assertIn("untracked()", captured.text)
                self.assert_only_source(captured, {"src/a.py", "src/new.py"})

    def test_local_target_with_only_excluded_changes_has_nothing_to_review(self):
        (self.repo / "dist/out.js").write_text("dirty generated\n")
        (self.repo / "dist/new.js").write_text("untracked generated\n")
        with self.assertRaisesRegex(SystemExit, "no local changes to review"):
            self.helper["local_bundle"](self.repo, None, ("dist",))

    def test_exclude_values_must_be_plain_relative_paths(self):
        validate = self.helper["exclude_path"]
        self.assertEqual(validate("dist"), "dist")
        self.assertEqual(validate("build/"), "build/")
        self.assertEqual(validate("dist/*.map"), "dist/*.map")
        for bad in ("", ":(glob)dist", ":dist", "-dist", "/tmp/dist", "\\dist",
                    "C:dist", "../dist", "dist/../src", "dist\\..\\src"):
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                validate(bad)

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.repo, text=True, capture_output=True, timeout=120,
        )

    def test_cli_refuses_unsafe_exclude_during_argument_parsing(self):
        result = self.run_cli("--mode", "commit", "--exclude", "../dist", "--dry-run")
        self.assertEqual(result.returncode, 2)
        self.assertIn("argument --exclude: must be a plain repository-relative path", result.stderr)
        self.assertNotIn("autoreview target", result.stdout)

    def test_cli_dry_run_reviews_the_target_without_excluded_paths(self):
        refused = self.run_cli("--mode", "commit", "--dry-run")
        self.assertIn("bundle: FAILED (refusing binary changes", refused.stdout)
        result = self.run_cli("--mode", "commit", "--exclude", "dist", "--dry-run")
        self.assertIn("excludes: dist\n", result.stdout)
        self.assertIn("bundle: constructible\n", result.stdout)
        self.assertIn("prompt: OK\n", result.stdout)


if __name__ == "__main__":
    unittest.main()
