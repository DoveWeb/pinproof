"""Tests for the CI gate (`check`) and the built-in selftest.  Run: python -m unittest discover tests"""
import json, os, subprocess, sys, tempfile, unittest

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pinproof.py")


def run(*args, pin_dir):
    env = dict(os.environ, PIN_DIR=pin_dir)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    return subprocess.run([sys.executable, TOOL, *args], capture_output=True, text=True, env=env)


def make_pin(pin_dir, slug, decide=True):
    run("open", slug, "--claim", "the job finishes with zero errors", "--kill", "any error kills it",
        "--rule", "after==0", "--unit", "errors in one run", "--single", "one run, one count",
        "--degenerate", "a run that processes nothing also shows 0 errors", pin_dir=pin_dir)
    if decide:
        run("before", slug, "--value", "3", "--evidence", "run 1: 3 errors", pin_dir=pin_dir)
        run("after", slug, "--value", "0", "--evidence", "run 2: 0 errors, 500 rows", pin_dir=pin_dir)
        run("verdict", slug, pin_dir=pin_dir)


class CheckGate(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_selftest_passes(self):
        self.assertEqual(run("selftest", pin_dir=self.d).returncode, 0)

    def test_intact_decided_records_pass(self):
        make_pin(self.d, "a"); make_pin(self.d, "b")
        r = run("check", "--require-verdict", pin_dir=self.d)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_tampered_record_fails(self):
        make_pin(self.d, "a")
        p = os.path.join(self.d, "a.json")
        pin = json.load(open(p, encoding="utf-8"))
        pin["events"][0]["data"]["kill"] = "softened after the fact"   # move the pass bar
        json.dump(pin, open(p, "w", encoding="utf-8"))
        r = run("check", pin_dir=self.d)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("broken    : 1", r.stdout)

    def test_undecided_fails_only_when_required(self):
        make_pin(self.d, "open-claim", decide=False)
        self.assertEqual(run("check", pin_dir=self.d).returncode, 0)
        self.assertEqual(run("check", "--require-verdict", pin_dir=self.d).returncode, 1)

    def test_empty_folder_is_not_a_green_build(self):
        self.assertEqual(run("check", pin_dir=self.d).returncode, 1)
        self.assertEqual(run("check", "--allow-empty", pin_dir=self.d).returncode, 0)


if __name__ == "__main__":
    unittest.main()
