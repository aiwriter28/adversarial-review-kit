import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "review.py"


class ReviewJourney(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "project"
        self.repo.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        fake = self.bin / "codex"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "pathlib.Path(os.environ['FAKE_CODEX_ARG_CAPTURE']).write_text('\\n'.join(args))\n"
            "prompt = sys.stdin.read()\n"
            "capture = os.environ.get('FAKE_CODEX_PROMPT_CAPTURE')\n"
            "if capture: pathlib.Path(capture).write_text(prompt)\n"
            "if os.environ.get('FAKE_CODEX_EXIT'): sys.exit(int(os.environ['FAKE_CODEX_EXIT']))\n"
            "out = pathlib.Path(args[args.index('--output-last-message') + 1])\n"
            "out.write_text(pathlib.Path(os.environ['FAKE_CODEX_RESPONSE']).read_text())\n"
        )
        fake.chmod(0o755)
        self.env = os.environ.copy()
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env["PATH"]
        self.env["FAKE_CODEX_PROMPT_CAPTURE"] = str(self.root / "prompt.txt")
        self.env["FAKE_CODEX_ARG_CAPTURE"] = str(self.root / "codex-args.txt")
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Example")
        self.git("config", "user.email", "example@example.com")
        (self.repo / "readme.txt").write_text("baseline\n")
        self.git("add", "readme.txt")
        self.git("commit", "-m", "Baseline")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        (self.repo / "review-card.md").write_text(
            "Expected: invalid input raises ValueError.\n\n"
            "## Test commands\n\n`python3 -m unittest`\n"
        )
        (self.repo / "app.py").write_text("def parse(value):\n    return value\n")
        self.git("add", "review-card.md", "app.py")
        self.git("commit", "-m", "Add parser")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            text=True,
            capture_output=True,
            check=True,
        )

    def invoke(self, *args, env=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args, "--repo", str(self.repo)],
            text=True,
            capture_output=True,
            env=env or self.env,
        )

    def response(self, data, complete_card=True):
        if isinstance(data.get("checks"), list):
            for item in data["checks"]:
                item.setdefault("purpose", "acceptance")
        for item in data.get("check_dispositions", []):
            item.setdefault("equivalent", False)
        if complete_card and isinstance(data.get("checks"), list) and not any(
            item.get("command") == "python3 -m unittest" for item in data["checks"]
        ):
            data["checks"].append({
                "command": "python3 -m unittest", "outcome": "pass", "evidence": "Fixture card check passed.",
                "purpose": "acceptance",
            })
        path = self.root / "response.json"
        path.write_text(json.dumps(data))
        self.env["FAKE_CODEX_RESPONSE"] = str(path)

    @property
    def ledger(self):
        return self.repo / ".git" / "adversarial-review" / "ledger.json"

    def start_with_finding(self):
        self.response(
            {
                "summary": "Input validation is missing.",
                "findings": [
                    {
                        "id": "F-01",
                        "severity": "major",
                        "axis": "spec",
                        "location": "app.py:2",
                        "problem": "Invalid input is accepted.",
                        "evidence": "parse(None) returns None, contrary to the card.",
                        "suggested_check": "Assert parse(None) raises ValueError.",
                    }
                ],
                "checks": [{"command": "git diff", "outcome": "pass", "evidence": "Reviewed the change."}],
            }
        )
        return self.invoke("start", "--base", self.base, "--card", "review-card.md")

    def test_findings_require_response_and_fix_verification(self):
        first = self.start_with_finding()
        self.assertEqual(first.returncode, 3, first.stderr)
        self.assertIn("F-01", first.stdout)
        self.assertTrue(self.ledger.is_file())
        self.assertIn("read-only", (self.root / "prompt.txt").read_text())
        args = (self.root / "codex-args.txt").read_text()
        self.assertIn("project_doc_max_bytes=0", args)
        self.assertIn("model_reasoning_effort=high", args)
        self.assertIn("--ignore-user-config", args)

        missing = self.invoke("verify")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("F-01", missing.stderr)

        answer = self.invoke("respond", "--id", "F-01", "--action", "fix", "--reason", "Add validation and test")
        self.assertEqual(answer.returncode, 0, answer.stderr)
        (self.repo / "app.py").write_text(
            "def parse(value):\n    if value is None:\n        raise ValueError('invalid')\n    return value\n"
        )
        self.git("add", "app.py")
        self.git("commit", "-m", "Reject invalid input")
        stale = self.invoke("status")
        self.assertEqual(stale.returncode, 3)
        self.assertIn("F-01", stale.stdout)
        self.response(
            {
                "summary": "The added validation closes F-01.",
                "dispositions": [{"id": "F-01", "status": "resolved", "evidence": "app.py now raises ValueError; the check passes."}],
                "check_dispositions": [],
                "new_findings": [],
                "checks": [{"command": "python3 -m unittest", "outcome": "pass", "evidence": "1 test passed"}],
            }
        )
        verified = self.invoke("verify")
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertIn("APPROVED", verified.stdout)
        saved = json.loads(self.ledger.read_text())
        self.assertEqual(saved["rounds"][0]["dispositions"][0]["status"], "resolved")
        self.assertIn("F-01", (self.root / "prompt.txt").read_text())

    def test_failed_provider_never_creates_or_advances_ledger(self):
        failed_env = self.env.copy()
        failed_env["FAKE_CODEX_EXIT"] = "1"
        result = self.invoke("start", "--base", self.base, "--card", "review-card.md", env=failed_env)
        self.assertEqual(result.returncode, 4)
        self.assertFalse(self.ledger.exists())

    def test_malformed_response_cannot_approve(self):
        self.response({"summary": "looks fine", "findings": [], "checks": "not an array"})
        result = self.invoke("start", "--base", self.base, "--card", "review-card.md")
        self.assertEqual(result.returncode, 4)
        self.assertFalse(self.ledger.exists())

    def test_verification_must_account_for_every_finding(self):
        self.assertEqual(self.start_with_finding().returncode, 3)
        self.assertEqual(
            self.invoke("respond", "--id", "F-01", "--action", "reject", "--reason", "False positive").returncode,
            0,
        )
        self.response({"summary": "No issue", "dispositions": [], "check_dispositions": [], "new_findings": [], "checks": []})
        result = self.invoke("verify")
        self.assertEqual(result.returncode, 4)
        self.assertEqual(json.loads(self.ledger.read_text())["rounds"], [])

    def test_clean_review_approves_and_dirty_tree_is_rejected(self):
        (self.repo / "scratch.txt").write_text("uncommitted\n")
        dirty = self.invoke("start", "--base", self.base, "--card", "review-card.md")
        self.assertEqual(dirty.returncode, 2)
        self.assertIn("scratch.txt", dirty.stderr)
        self.assertFalse(self.ledger.exists())
        (self.repo / "scratch.txt").unlink()
        self.response({
            "summary": "No issue found.", "findings": [],
            "checks": [{"command": "git diff", "outcome": "pass", "evidence": "Inspected the full change."}],
        })
        clean = self.invoke("start", "--base", self.base, "--card", "review-card.md")
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertIn("APPROVED", clean.stdout)

    def test_critical_finding_cannot_be_deferred(self):
        self.response(
            {
                "summary": "Unsafe change.",
                "findings": [{
                    "id": "F-01", "severity": "critical", "axis": "standards",
                    "location": "app.py:2", "problem": "Sensitive data is exposed.",
                    "evidence": "The return value contains a secret.",
                    "suggested_check": "Assert secret data is excluded."
                }],
                "checks": [],
            }
        )
        self.assertEqual(self.invoke("start", "--base", self.base, "--card", "review-card.md").returncode, 3)
        deferred = self.invoke("respond", "--id", "F-01", "--action", "defer", "--reason", "Later")
        self.assertEqual(deferred.returncode, 2)
        self.assertEqual(json.loads(self.ledger.read_text())["responses"], {})

    def test_noncritical_deferral_remains_visible(self):
        self.assertEqual(self.start_with_finding().returncode, 3)
        self.assertEqual(
            self.invoke("respond", "--id", "F-01", "--action", "defer", "--reason", "Known low-volume path").returncode,
            0,
        )
        self.response({
            "summary": "Risk is documented.",
            "dispositions": [{"id": "F-01", "status": "deferred", "evidence": "The low-volume risk remains."}],
            "check_dispositions": [],
            "new_findings": [],
            "checks": [{"command": "git diff", "outcome": "pass", "evidence": "Inspected current behavior."}],
        })
        result = self.invoke("verify")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("APPROVED WITH DEFERRED FINDINGS", result.stdout)
        self.assertEqual(json.loads(self.ledger.read_text())["rounds"][0]["dispositions"][0]["status"], "deferred")

    def test_fix_induced_finding_remains_open(self):
        self.assertEqual(self.start_with_finding().returncode, 3)
        self.assertEqual(
            self.invoke("respond", "--id", "F-01", "--action", "fix", "--reason", "Add a guard").returncode,
            0,
        )
        (self.repo / "app.py").write_text("def parse(value):\n    if value is None: raise ValueError()\n    return value\n")
        self.git("add", "app.py")
        self.git("commit", "-m", "Guard None")
        self.response({
            "summary": "F-01 resolved; new fix issue.",
            "dispositions": [{"id": "F-01", "status": "resolved", "evidence": "Guard is present."}],
            "check_dispositions": [],
            "new_findings": [{
                "id": "F-02", "severity": "major", "axis": "spec", "location": "app.py:2",
                "problem": "Empty string remains accepted.", "evidence": "parse('') returns ''.",
                "suggested_check": "Assert parse('') raises ValueError."
            }],
            "checks": [],
        })
        result = self.invoke("verify")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("F-02", result.stdout)
        self.assertNotIn("F-01 [", result.stdout)

    def test_criteria_cannot_be_weakened_during_fix_verification(self):
        self.assertEqual(self.start_with_finding().returncode, 3)
        self.assertEqual(
            self.invoke("respond", "--id", "F-01", "--action", "fix", "--reason", "Add a guard").returncode,
            0,
        )
        (self.repo / "review-card.md").write_text("Expected: None is allowed.\n")
        self.git("add", "review-card.md")
        self.git("commit", "-m", "Weaken expected behavior")
        result = self.invoke("verify")
        self.assertEqual(result.returncode, 2)
        self.assertIn("review card changed", result.stderr)
        self.assertEqual(json.loads(self.ledger.read_text())["rounds"], [])

    def test_failed_check_never_gets_an_approval_label(self):
        self.response({
            "summary": "No finding, but the acceptance command failed.",
            "findings": [],
            "checks": [{"command": "python3 -m unittest", "outcome": "fail", "evidence": "Exit 1"}],
        })
        result = self.invoke("start", "--base", self.base, "--card", "review-card.md")
        self.assertEqual(result.returncode, 3)
        self.assertIn("NEEDS HUMAN CHECK", result.stdout)
        self.assertNotIn("APPROVED", result.stdout)

    def test_archive_preserves_finished_ledger_and_allows_next_review(self):
        self.response({
            "summary": "No issue found.",
            "findings": [],
            "checks": [{"command": "git diff", "outcome": "pass", "evidence": "Inspected change"}],
        })
        self.assertEqual(self.invoke("start", "--base", self.base, "--card", "review-card.md").returncode, 0)
        archived = self.invoke("archive", "--reason", "Finished review")
        self.assertEqual(archived.returncode, 0, archived.stderr)
        self.assertFalse(self.ledger.exists())
        history = list((self.ledger.parent / "history").glob("*.json"))
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0].read_text())["close_reason"], "Finished review")
        self.assertEqual(self.invoke("start", "--base", self.base, "--card", "review-card.md").returncode, 0)

    def test_unfinished_review_cannot_be_archived_early(self):
        self.assertEqual(self.start_with_finding().returncode, 3)
        result = self.invoke("archive", "--reason", "Skip it")
        self.assertEqual(result.returncode, 2)
        self.assertTrue(self.ledger.exists())

    def test_status_marks_later_changes_as_stale(self):
        self.response({
            "summary": "No issue found.",
            "findings": [],
            "checks": [{"command": "git diff", "outcome": "pass", "evidence": "Inspected change"}],
        })
        self.assertEqual(self.invoke("start", "--base", self.base, "--card", "review-card.md").returncode, 0)
        (self.repo / "app.py").write_text("def parse(value):\n    return None\n")
        status = self.invoke("status")
        self.assertEqual(status.returncode, 3)
        self.assertIn("STALE REVIEW", status.stdout)

    def test_verification_rejects_divergent_git_history(self):
        self.assertEqual(self.start_with_finding().returncode, 3)
        self.assertEqual(
            self.invoke("respond", "--id", "F-01", "--action", "fix", "--reason", "Fix later").returncode,
            0,
        )
        self.git("switch", "-c", "other", self.base)
        (self.repo / "review-card.md").write_text("Expected: invalid input raises ValueError. Check: python3 -m unittest.\n")
        (self.repo / "app.py").write_text("def parse(value):\n    raise ValueError()\n")
        self.git("add", "review-card.md", "app.py")
        self.git("commit", "-m", "Divergent fix")
        result = self.invoke("verify")
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not descend", result.stderr)

    def test_start_rejects_base_from_another_branch(self):
        self.git("switch", "-c", "other", self.base)
        (self.repo / "other.txt").write_text("unrelated\n")
        self.git("add", "other.txt")
        self.git("commit", "-m", "Unrelated branch")
        other = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("switch", "main")
        result = self.invoke("start", "--base", other, "--card", "review-card.md")
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be an ancestor", result.stderr)
        self.assertFalse(self.ledger.exists())

    def test_amended_history_can_be_archived_and_restarted(self):
        self.assertEqual(self.start_with_finding().returncode, 3)
        self.git("commit", "--amend", "-m", "Amended parser change")
        blocked = self.invoke("verify")
        self.assertEqual(blocked.returncode, 2)
        archived = self.invoke("archive", "--reason", "Commit was amended; restart against current history")
        self.assertEqual(archived.returncode, 0, archived.stderr)
        history = list((self.ledger.parent / "history").glob("*.json"))
        self.assertEqual(json.loads(history[0].read_text())["close_status"], "SUPERSEDED REVIEW")
        self.assertEqual(self.start_with_finding().returncode, 3)
        restarted = json.loads(self.ledger.read_text())
        self.assertEqual(len(restarted["discovery"]["findings"]), 2)
        self.assertIn("Prior unresolved", restarted["discovery"]["findings"][1]["problem"])

    def test_changed_card_can_be_archived_and_restarted(self):
        self.assertEqual(self.start_with_finding().returncode, 3)
        (self.repo / "review-card.md").write_text(
            "Expected: different behavior.\n\n## Test commands\n\n`python3 -m unittest`\n"
        )
        self.git("add", "review-card.md")
        self.git("commit", "-m", "Revise review criteria")
        archived = self.invoke("archive", "--reason", "Criteria changed")
        self.assertEqual(archived.returncode, 0, archived.stderr)
        history = list((self.ledger.parent / "history").glob("*.json"))
        self.assertEqual(json.loads(history[0].read_text())["close_status"], "SUPERSEDED REVIEW")
        self.assertEqual(self.start_with_finding().returncode, 3)

    def test_superseded_review_can_archive_when_old_commit_is_missing(self):
        self.assertEqual(self.start_with_finding().returncode, 3)
        ledger = json.loads(self.ledger.read_text())
        ledger["reviewed_head"] = "0" * 40
        self.ledger.write_text(json.dumps(ledger))
        archived = self.invoke("archive", "--reason", "Old commit no longer exists")
        self.assertEqual(archived.returncode, 0, archived.stderr)
        self.assertIn("SUPERSEDED REVIEW", archived.stdout)

    def test_superseded_failed_check_is_carried_into_new_review(self):
        self.response({
            "summary": "Acceptance failed.", "findings": [],
            "checks": [{"command": "python3 -m unittest", "outcome": "fail", "evidence": "Exit 1"}],
        })
        self.assertEqual(self.invoke("start", "--base", self.base, "--card", "review-card.md").returncode, 3)
        (self.repo / "review-card.md").write_text(
            "Expected: different behavior.\n\n## Test commands\n\n`python3 -m unittest`\n"
        )
        self.git("add", "review-card.md")
        self.git("commit", "-m", "Revise card")
        self.assertEqual(self.invoke("archive", "--reason", "Changed criteria").returncode, 0)
        self.response({
            "summary": "Everything looks fine.", "findings": [],
            "checks": [{"command": "python3 -m unittest", "outcome": "pass", "evidence": "Exit 0"}],
        })
        restarted = self.invoke("start", "--base", self.base, "--card", "review-card.md")
        self.assertEqual(restarted.returncode, 3, restarted.stderr)
        self.assertIn("F-01", restarted.stdout)
        self.assertIn("Prior unresolved acceptance check", restarted.stdout)
        self.assertIn("python3 -m unittest", (self.root / "prompt.txt").read_text())

    def test_moving_repo_keeps_ledger_usable(self):
        self.assertEqual(self.start_with_finding().returncode, 3)
        moved = self.root / "moved-project"
        self.repo.rename(moved)
        self.repo = moved
        status = self.invoke("status")
        self.assertEqual(status.returncode, 3, status.stderr)
        self.assertIn("F-01", status.stdout)

    def test_two_unresolved_rounds_require_recorded_human_handoff(self):
        self.assertEqual(self.start_with_finding().returncode, 3)
        for round_number in (1, 2):
            self.assertEqual(
                self.invoke("respond", "--id", "F-01", "--action", "reject", "--reason", "Needs human review").returncode,
                0,
            )
            self.response({
                "summary": f"Round {round_number}: still open.",
                "dispositions": [{"id": "F-01", "status": "unresolved", "evidence": "Invalid input still passes."}],
                "check_dispositions": [],
                "new_findings": [],
                "checks": [{"command": "git diff", "outcome": "pass", "evidence": "Inspected."}],
            })
            result = self.invoke("verify")
            self.assertEqual(result.returncode, 3)
        self.assertIn("MANUAL HANDOFF", result.stdout)
        archived = self.invoke("archive", "--reason", "Human accepted the documented residual risk")
        self.assertEqual(archived.returncode, 0, archived.stderr)
        history = list((self.ledger.parent / "history").glob("*.json"))
        record = json.loads(history[0].read_text())
        self.assertEqual(record["close_status"], "MANUAL HANDOFF")
        self.assertEqual(record["rounds"][-1]["dispositions"][0]["status"], "unresolved")

    def test_verify_repeats_approved_status_after_second_round(self):
        self.assertEqual(self.start_with_finding().returncode, 3)
        for status in ("unresolved", "resolved"):
            self.assertEqual(
                self.invoke("respond", "--id", "F-01", "--action", "reject", "--reason", "Check evidence").returncode,
                0,
            )
            self.response({
                "summary": status,
                "dispositions": [{"id": "F-01", "status": status, "evidence": "Checked the source."}],
                "check_dispositions": [],
                "new_findings": [],
                "checks": [{"command": "git diff", "outcome": "pass", "evidence": "Inspected."}],
            })
            result = self.invoke("verify")
        self.assertEqual(result.returncode, 0, result.stderr)
        again = self.invoke("verify")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("APPROVED", again.stdout)

    def test_failed_discovery_check_must_be_closed_in_verification(self):
        self.response({
            "summary": "The invalid-input test fails.",
            "findings": [{
                "id": "F-01", "severity": "major", "axis": "spec", "location": "app.py:2",
                "problem": "Invalid input is accepted.", "evidence": "The test fails.",
                "suggested_check": "Run python3 -m unittest."
            }],
            "checks": [{"command": "python3 -m unittest", "outcome": "fail", "evidence": "Exit 1"}],
        })
        self.assertEqual(self.invoke("start", "--base", self.base, "--card", "review-card.md").returncode, 3)
        self.assertEqual(
            self.invoke("respond", "--id", "F-01", "--action", "reject", "--reason", "Check the fix").returncode,
            0,
        )
        report = {
            "summary": "Claimed resolved.",
            "dispositions": [{"id": "F-01", "status": "resolved", "evidence": "Code was inspected."}],
            "check_dispositions": [],
            "new_findings": [],
            "checks": [{"command": "git diff", "outcome": "pass", "evidence": "Inspected."}],
        }
        self.response(report)
        omitted = self.invoke("verify")
        self.assertEqual(omitted.returncode, 4)
        self.assertEqual(json.loads(self.ledger.read_text())["rounds"], [])
        report["check_dispositions"] = [{
            "id": "C-01", "status": "resolved", "evidence": "The same test now passes.",
            "replacement_command": "python3 -m unittest",
        }]
        report["checks"] = [{"command": "python3 -m unittest", "outcome": "pass", "evidence": "Exit 0"}]
        self.response(report)
        resolved = self.invoke("verify")
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertIn("APPROVED", resolved.stdout)

    def test_failed_check_cannot_be_dismissed_or_replaced_without_builder_response(self):
        self.response({
            "summary": "Acceptance failed.", "findings": [],
            "checks": [{"command": "python3 -m unittest", "outcome": "fail", "evidence": "Exit 1"}],
        })
        self.assertEqual(self.invoke("start", "--base", self.base, "--card", "review-card.md").returncode, 3)
        rejected = self.invoke(
            "respond", "--id", "C-01", "--action", "replace",
            "--command", "true", "--reason", "Call it equivalent",
        )
        self.assertEqual(rejected.returncode, 2)
        report = {
            "summary": "Check was dismissed.", "dispositions": [], "new_findings": [],
            "check_dispositions": [{
                "id": "C-01", "status": "deferred", "evidence": "Reviewer calls it irrelevant.",
                "replacement_command": "",
            }],
            "checks": [{"command": "git diff", "outcome": "pass", "evidence": "Diff visible."}],
        }
        self.response(report)
        self.assertEqual(self.invoke("verify").returncode, 4)
        report["check_dispositions"][0].update(status="resolved", replacement_command="git diff")
        self.response(report)
        self.assertEqual(self.invoke("verify").returncode, 4)
        self.assertEqual(json.loads(self.ledger.read_text())["rounds"], [])

    def test_uncertain_check_uses_prior_acceptance_command_as_replacement(self):
        original = "python3 - <<'PY'\nassert False\nPY"
        alternate = "python3 -m unittest discover -v"
        self.response({
            "summary": "Shell blocked one command and acceptance failed.", "findings": [],
            "checks": [
                {"command": original, "outcome": "uncertain", "evidence": "Shell could not create temp file."},
                {"command": alternate, "outcome": "fail", "evidence": "Acceptance failed."},
            ],
        })
        self.assertEqual(self.invoke("start", "--base", self.base, "--card", "review-card.md").returncode, 3)
        wrong = self.invoke(
            "respond", "--id", "C-01", "--action", "replace",
            "--command", "true", "--reason", "Call it equivalent",
        )
        self.assertEqual(wrong.returncode, 2)
        response = self.invoke(
            "respond", "--id", "C-01", "--action", "replace",
            "--command", alternate, "--reason", "Same acceptance assertions without the blocked heredoc",
        )
        self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
        self.response({
            "summary": "Both acceptance checks now pass.", "dispositions": [], "new_findings": [],
            "check_dispositions": [
                {"id": "C-01", "status": "resolved", "evidence": "Equivalent acceptance assertions pass.",
                 "replacement_command": alternate, "equivalent": True},
                {"id": "C-02", "status": "resolved", "evidence": "Original command passes.",
                 "replacement_command": alternate},
            ],
            "checks": [{"command": alternate, "outcome": "pass", "evidence": "Exit 0"}],
        })
        verified = self.invoke("verify")
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertIn("APPROVED WITH REPLACED CHECKS", verified.stdout)
        self.assertIn("C-01 replaced check", verified.stdout)
        self.assertIn(alternate, (self.root / "prompt.txt").read_text())

    def test_uncertain_check_can_use_prior_passing_acceptance_command(self):
        original = "python3 - <<'PY'\nassert True\nPY"
        alternate = "python3 -c 'assert True'"
        self.response({
            "summary": "The shell blocked a heredoc; the same assertion passed with -c.",
            "findings": [],
            "checks": [
                {"command": original, "outcome": "uncertain", "evidence": "Shell blocked heredoc."},
                {"command": alternate, "outcome": "pass", "evidence": "Assertion passed."},
            ],
        })
        self.assertEqual(self.invoke("start", "--base", self.base, "--card", "review-card.md").returncode, 3)
        result = self.invoke(
            "respond", "--id", "C-01", "--action", "replace",
            "--command", alternate, "--reason", "Identical assertion without the blocked heredoc",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_uncertain_card_command_cannot_be_replaced(self):
        self.response({
            "summary": "Card command blocked.", "findings": [],
            "checks": [
                {"command": "python3 -m unittest", "outcome": "uncertain", "evidence": "Blocked."},
                {"command": "python3 -m unittest discover -v", "outcome": "fail", "evidence": "Exit 1"},
            ],
        })
        self.assertEqual(self.invoke("start", "--base", self.base, "--card", "review-card.md").returncode, 3)
        result = self.invoke(
            "respond", "--id", "C-01", "--action", "replace",
            "--command", "python3 -m unittest discover -v", "--reason", "Equivalent suite",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("review card", result.stderr)

    def test_missing_card_command_during_verification_stays_open(self):
        self.response({
            "summary": "Acceptance failed.", "findings": [],
            "checks": [{"command": "python3 -m unittest", "outcome": "fail", "evidence": "Exit 1"}],
        })
        self.assertEqual(self.invoke("start", "--base", self.base, "--card", "review-card.md").returncode, 3)
        self.response({
            "summary": "No card command run.", "dispositions": [], "new_findings": [],
            "check_dispositions": [{"id": "C-01", "status": "unresolved", "evidence": "Not run.",
                                    "replacement_command": ""}],
            "checks": [{"command": "git diff", "outcome": "pass", "evidence": "Diff read.",
                        "purpose": "exploration"}],
        }, complete_card=False)
        result = self.invoke("verify")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("C-02", result.stdout)

    def test_replacement_needs_reviewer_equivalence_judgment(self):
        original = "python3 -m unittest -v"
        alternate = "python3 -m unittest discover -v"
        self.response({
            "summary": "Blocked and failed acceptance.", "findings": [],
            "checks": [
                {"command": original, "outcome": "uncertain", "evidence": "Blocked."},
                {"command": alternate, "outcome": "fail", "evidence": "Exit 1"},
            ],
        })
        self.assertEqual(self.invoke("start", "--base", self.base, "--card", "review-card.md").returncode, 3)
        self.assertEqual(self.invoke("respond", "--id", "C-01", "--action", "replace",
                                     "--command", alternate, "--reason", "Same suite").returncode, 0)
        self.response({
            "summary": "Claims resolution.", "dispositions": [], "new_findings": [],
            "check_dispositions": [
                {"id": "C-01", "status": "resolved", "evidence": "No equivalence proof.",
                 "replacement_command": alternate, "equivalent": False},
                {"id": "C-02", "status": "resolved", "evidence": "Original passes.",
                 "replacement_command": alternate},
            ],
            "checks": [{"command": alternate, "outcome": "pass", "evidence": "Exit 0"}],
        })
        result = self.invoke("verify")
        self.assertEqual(result.returncode, 4)
        self.assertIn("equivalence judgment", result.stderr)

    def test_card_command_section_requires_inline_code_commands(self):
        (self.repo / "review-card.md").write_text(
            "Expected: invalid input fails.\n\n## Test commands\n\n"
            "Run the test from the repository root:\n`python3 -m unittest`\n"
        )
        self.git("add", "review-card.md")
        self.git("commit", "-m", "Add unclear card")
        result = self.invoke("start", "--base", self.base, "--card", "review-card.md")
        self.assertEqual(result.returncode, 2)
        self.assertIn("backticks", result.stderr)
        self.assertFalse(self.ledger.exists())

    def test_exploration_failure_does_not_block_acceptance(self):
        self.response({
            "summary": "Acceptance passes.", "findings": [],
            "checks": [
                {"command": "git remote get-url origin", "outcome": "fail", "evidence": "No remote.",
                 "purpose": "exploration"},
                {"command": "python3 -m unittest", "outcome": "pass", "evidence": "Suite passes.",
                 "purpose": "acceptance"},
            ],
        })
        first = self.invoke("start", "--base", self.base, "--card", "review-card.md")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("0 open check(s)", first.stdout)

    def test_missing_card_command_blocks_approval(self):
        (self.repo / "review-card.md").write_text(
            "Expected behavior: None raises ValueError.\n\n## Test commands\n\n`python3 -m unittest`\n"
        )
        self.git("add", "review-card.md")
        self.git("commit", "-m", "Specify acceptance command")
        self.response({
            "summary": "Looked at diff only.", "findings": [],
            "checks": [{"command": "git diff", "outcome": "pass", "evidence": "Diff visible.",
                        "purpose": "exploration"}],
        }, complete_card=False)
        first = self.invoke("start", "--base", self.base, "--card", "review-card.md")
        self.assertEqual(first.returncode, 3, first.stderr)
        self.assertIn("C-01", first.stdout)
        self.assertIn("python3 -m unittest", first.stdout)

    def test_archive_marks_stale_approval_without_approving_new_work(self):
        self.response({
            "summary": "No issue found.", "findings": [],
            "checks": [{"command": "git diff", "outcome": "pass", "evidence": "Inspected."}],
        })
        self.assertEqual(self.invoke("start", "--base", self.base, "--card", "review-card.md").returncode, 0)
        reviewed_head = self.git("rev-parse", "HEAD").stdout.strip()
        (self.repo / "app.py").write_text("def parse(value):\n    return None\n")
        self.git("add", "app.py")
        self.git("commit", "-m", "Next change after approval")
        archived = self.invoke("archive", "--reason", "Done")
        self.assertEqual(archived.returncode, 0, archived.stderr)
        self.assertFalse(self.ledger.exists())
        history = list((self.ledger.parent / "history").glob("*.json"))
        self.assertEqual(json.loads(history[0].read_text())["close_status"], "STALE REVIEW")
        self.assertEqual(self.invoke("start", "--base", reviewed_head, "--card", "review-card.md").returncode, 0)

    def test_passing_check_cannot_inject_an_open_id(self):
        self.response({
            "summary": "No issue found.", "findings": [],
            "checks": [{"command": "git diff", "outcome": "pass", "evidence": "Inspected.", "id": "C-07"}],
        })
        result = self.invoke("start", "--base", self.base, "--card", "review-card.md")
        self.assertEqual(result.returncode, 4)
        self.assertFalse(self.ledger.exists())

    def test_check_only_review_can_be_verified_without_code_change(self):
        self.response({
            "summary": "The check could not complete.", "findings": [],
            "checks": [{"command": "python3 -m unittest", "outcome": "uncertain", "evidence": "Environment blocked."}],
        })
        first = self.invoke("start", "--base", self.base, "--card", "review-card.md")
        self.assertEqual(first.returncode, 3)
        self.assertIn("C-01", first.stdout)
        report = {
            "summary": "Check completed.", "dispositions": [],
            "check_dispositions": [{
                "id": "C-01", "status": "resolved", "evidence": "The test now passes.",
                "replacement_command": "python3 -m unittest",
            }],
            "new_findings": [],
            "checks": [{"command": "git diff", "outcome": "pass", "evidence": "Inspected."}],
        }
        self.response(report, complete_card=False)
        unsupported = self.invoke("verify")
        self.assertEqual(unsupported.returncode, 4)
        self.assertIn("builder-authorized passing command", unsupported.stderr)
        report["checks"] = [{"command": "python3 -m unittest", "outcome": "pass", "evidence": "Exit 0"}]
        self.response(report)
        verified = self.invoke("verify")
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertIn("APPROVED", verified.stdout)


if __name__ == "__main__":
    unittest.main()
