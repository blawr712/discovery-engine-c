"""A rehearsal of the monthly cycle must never touch real records."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import main
from src.cli import parse_args

REPOSITORY = Path(__file__).resolve().parent.parent


class IsolationTests(unittest.TestCase):
    def _paths(self, env):
        probe = (
            "import json, src.config as c; print(json.dumps({k: str(getattr(c, k)) for k in "
            "('OUTPUT_DIR', 'RUN_DIR', 'HISTORY_DATABASE', 'CACHE_DIR')}))"
        )
        output = subprocess.run(
            [sys.executable, "-c", probe], cwd=REPOSITORY, env=env,
            capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()[-1]
        return json.loads(output)

    def test_every_output_path_moves_into_the_isolated_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "DISCOVERY_ISOLATED_ROOT": directory}
            isolated = self._paths(env)
            root = str(Path(directory).resolve())
            for name in ("OUTPUT_DIR", "RUN_DIR", "HISTORY_DATABASE"):
                self.assertTrue(isolated[name].startswith(root), (name, isolated[name]))

    def test_caches_stay_shared_and_normal_runs_are_unchanged(self):
        env = {k: v for k, v in os.environ.items() if k != "DISCOVERY_ISOLATED_ROOT"}
        normal = self._paths(env)
        with tempfile.TemporaryDirectory() as directory:
            isolated = self._paths({**env, "DISCOVERY_ISOLATED_ROOT": directory})
        self.assertEqual(isolated["CACHE_DIR"], normal["CACHE_DIR"])
        self.assertTrue(normal["OUTPUT_DIR"].endswith(os.path.join("data", "exports")))
        self.assertTrue(normal["RUN_DIR"].endswith(os.path.join("data", "runs")))


class CommandTests(unittest.TestCase):
    def test_rehearsal_options(self):
        args = parse_args(["--monthly", "--rehearsal"])
        self.assertTrue(args.rehearsal)
        self.assertEqual(args.rehearsal_size, 25)
        self.assertEqual(parse_args(["--monthly", "--rehearsal", "--rehearsal-size", "5"]).rehearsal_size, 5)
        with self.assertRaises(SystemExit):
            parse_args(["--rehearsal"])

    def test_a_rehearsal_refuses_to_run_without_isolation(self):
        with mock.patch.object(main, "ISOLATED_ROOT", None):
            with self.assertRaises(SystemExit) as raised:
                main.run_monthly(rehearsal_size=5)
        self.assertIn("isolated folder", str(raised.exception))

    def test_the_parent_relaunches_itself_into_a_fresh_folder(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(main, "BASE_DIR", Path(directory)), \
                mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            main.rehearse_monthly(["--monthly", "--rehearsal"])

            command = run.call_args.args[0]
            env = run.call_args.kwargs["env"]
            root = Path(env["DISCOVERY_ISOLATED_ROOT"])
            self.assertEqual(command[-2:], ["--monthly", "--rehearsal"])
            self.assertEqual(root.parent, Path(directory) / "data" / "rehearsal")
            self.assertTrue(root.is_dir())

    def test_a_failed_rehearsal_exits_with_its_code(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(main, "BASE_DIR", Path(directory)), \
                mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 3)):
            with self.assertRaises(SystemExit) as raised:
                main.rehearse_monthly(["--monthly", "--rehearsal"])
        self.assertEqual(raised.exception.code, 3)


if __name__ == "__main__":
    unittest.main()
