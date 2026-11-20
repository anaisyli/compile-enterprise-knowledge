#!/usr/bin/env python3
"""One script at a time per run folder: every command taking --out shares the same lock."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

SCRIPTS = ROOT / "scripts"
LOCK = ".run_pipeline.lock"


def script(name: str, *arguments: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / name), *arguments],
                          capture_output=True, text=True, encoding="utf-8", env=env)


class RunLockTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.run = self.root / "run"
        self.vault = self.root / "vault"
        self.run.mkdir(parents=True)
        self.vault.mkdir(parents=True)
        (self.vault / "marker.txt").write_text("keep", encoding="utf-8")
        self.holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self.stop_holder)

    def stop_holder(self) -> None:
        self.holder.kill()
        self.holder.wait()

    def hold(self, pid: int, what: str = "run_pipeline.py") -> None:
        (self.run / LOCK).write_text(f"{pid} 2026-01-01T00:00:00 {what}\n", encoding="utf-8")

    def test_site_view_update_exits_3_and_leaves_the_vault_alone(self) -> None:
        self.hold(self.holder.pid)
        result = script("site_view.py", "update", "--out", str(self.run), "--vault", str(self.vault))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("run_pipeline.py", result.stdout)  # The message names what holds the lock.
        self.assertIn(str(self.holder.pid), result.stdout)
        self.assertEqual(sorted(path.name for path in self.vault.iterdir()), ["marker.txt"])

    def test_publish_vault_dry_run_exits_3_while_the_run_is_locked(self) -> None:
        self.hold(self.holder.pid)
        result = script("publish_vault.py", "--out", str(self.run), "--vault", str(self.vault), "--dry-run")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)

    def test_d_review_collect_exits_3_and_leaves_the_config_alone(self) -> None:
        self.hold(self.holder.pid)
        config = self.root / "kb-config.json"
        config.write_text('{"company": "视领"}', encoding="utf-8")
        before = config.read_bytes()
        result = script("d_review.py", "collect", "--out", str(self.run), "--config", str(config))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(config.read_bytes(), before)

    def test_pictures_collect_exits_3_while_the_run_is_locked(self) -> None:
        self.hold(self.holder.pid)
        result = script("pictures.py", "collect", "--out", str(self.run))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)

    def test_a_step_inheriting_the_lock_holder_passes(self) -> None:
        self.hold(self.holder.pid)
        env = {**os.environ, "CEK_RUN_LOCK": str(self.holder.pid)}
        result = script("pictures.py", "collect", "--out", str(self.run), env=env)
        # Past the lock it fails or finishes for its own reasons — just not with code 3.
        self.assertNotEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertNotIn("这个运行目录正在运行", result.stdout + result.stderr)
        # The child did not touch the parent's lock file.
        self.assertTrue((self.run / LOCK).read_text(encoding="utf-8").startswith(str(self.holder.pid)))

    def test_a_dead_holder_is_taken_over(self) -> None:
        probe = subprocess.Popen([sys.executable, "-c", "pass"])
        probe.wait()
        self.hold(probe.pid)
        result = script("pictures.py", "collect", "--out", str(self.run))
        self.assertNotEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertNotIn("这个运行目录正在运行", result.stdout + result.stderr)

    def test_every_run_folder_script_takes_the_lock(self) -> None:
        # mineru_client / reducto_client write --out files, legacy_migrations is read-only status; the rest must lock.
        excluded = {"mineru_client.py", "reducto_client.py", "legacy_migrations.py"}
        checked = 0
        for path in sorted(SCRIPTS.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            if 'add_argument("--out"' not in source and "add_argument('--out'" not in source:
                continue
            if path.name in excluded:
                continue
            checked += 1
            self.assertIn("run_lock(", source, path.name)
        self.assertGreater(checked, 15)

    def fake_package(self) -> Path:
        package = self.root / "package"
        (package / "审计记录").mkdir(parents=True)
        (package / "审计记录" / "package-manifest.json").write_text('{"assets": []}', encoding="utf-8")
        return package

    def test_a_config_edited_mid_build_stops_the_pipeline(self) -> None:
        import run_pipeline
        package = self.fake_package()
        config = self.root / "kb-config.json"
        config.write_text('{"company": "视领"}', encoding="utf-8")
        calls = []

        def fake_step(script: str, *arguments: str) -> int:
            calls.append(script)
            if script == "extract_blocks.py":
                config.write_text('{"company": "改过"}', encoding="utf-8")
            return 0

        argv = ["run_pipeline.py", "--package", str(package), "--out", str(self.run), "--config", str(config)]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), mock.patch.object(run_pipeline, "step", fake_step), \
                contextlib.redirect_stdout(out):
            code = run_pipeline.main()
        self.assertEqual(code, 5)
        self.assertIn("kb-config.json 在建库过程中被改过", out.getvalue())
        self.assertIn("extract_blocks.py", out.getvalue())
        self.assertEqual(calls, ["sample_gate.py", "extract_package_text.py", "extract_blocks.py"])

    def test_an_untouched_config_lets_the_pipeline_finish(self) -> None:
        import run_pipeline
        package = self.fake_package()
        config = self.root / "kb-config.json"
        config.write_text('{"company": "视领"}', encoding="utf-8")
        calls = []

        def fake_step(script: str, *arguments: str) -> int:
            calls.append(script)
            return 0

        argv = ["run_pipeline.py", "--package", str(package), "--out", str(self.run), "--config", str(config)]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(run_pipeline, "step", fake_step), \
                contextlib.redirect_stdout(io.StringIO()):
            code = run_pipeline.main()
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["sample_gate.py", "extract_package_text.py", "extract_blocks.py",
                                 "page_check.py", "guard_blocks.py", "render_kb.py",
                                 "curate.py", "curate.py", "verify_kb.py"])


if __name__ == "__main__":
    unittest.main()
