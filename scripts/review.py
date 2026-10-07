#!/usr/bin/env python3
"""A bounded, read-only Codex review with an explicit finding ledger."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
MAX_VERIFY = 2


class ReviewError(Exception):
    pass


class ProviderError(Exception):
    pass


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True)
    if result.returncode:
        raise ReviewError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def is_ancestor(repo, ancestor, descendant):
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
        text=True, capture_output=True,
    )
    if result.returncode not in (0, 1):
        raise ReviewError(result.stderr.strip() or "Cannot check Git ancestry.")
    return result.returncode == 0


def commit_exists(repo, revision):
    result = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{revision}^{{commit}}"],
        text=True, capture_output=True,
    )
    return result.returncode == 0


def repository(path):
    requested = Path(path).expanduser().resolve()
    top = Path(git(requested, "rev-parse", "--show-toplevel")).resolve()
    if requested != top:
        raise ReviewError(f"Use the Git repository root: {top}")
    return top


def ledger_path(repo):
    path = Path(git(repo, "rev-parse", "--git-path", "adversarial-review/ledger.json"))
    return path if path.is_absolute() else repo / path


def clean_head(repo):
    changes = git(repo, "status", "--short")
    if changes:
        raise ReviewError(
            "Commit or ignore working tree changes before review; the runner compares stable commits:\n"
            + "\n".join(changes.splitlines()[:10])
        )
    return git(repo, "rev-parse", "HEAD^{commit}")


def load_ledger(path):
    if not path.is_file():
        raise ReviewError("No review ledger found. Run start first.")
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewError(f"Cannot read ledger: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 3:
        raise ReviewError("Ledger format is invalid or unsupported.")
    return data


def save_ledger(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".ledger-", delete=False) as tmp:
        json.dump(data, tmp, indent=2)
        tmp.write("\n")
        temporary = Path(tmp.name)
    temporary.replace(path)


def card_text(repo, card):
    path = (repo / card).resolve()
    if not path.is_relative_to(repo) or not path.is_file():
        raise ReviewError("The review card must be an existing file inside the target repository.")
    content = path.read_text()
    if not content.strip():
        raise ReviewError("The review card is empty.")
    return str(path.relative_to(repo)), content


def card_commands(card):
    section = re.search(r"(?ims)^## Test commands[ \t]*\n(.*?)(?=^## |\Z)", card)
    if not section:
        raise ReviewError("Add a ## Test commands section with the commands the reviewer must run.")
    commands = []
    for line in section.group(1).splitlines():
        command = line.strip()
        if not command:
            continue
        if command.startswith(("- ", "* ")):
            command = command[2:].strip()
        command = re.sub(r"^\d+\.\s+", "", command)
        if not (command.startswith("`") and command.endswith("`") and command.count("`") == 2):
            raise ReviewError("Put each test command on its own line in backticks under ## Test commands; remove prose and code fences.")
        command = command[1:-1].strip()
        if command.startswith("$ "):
            command = command[2:].strip()
        if not command or command == "<replace-with-test-command>" or command.endswith((":", "\\")):
            raise ReviewError("Replace the placeholder with complete, single-line test commands.")
        commands.append(command)
    if not commands:
        raise ReviewError("List at least one test command in backticks under ## Test commands.")
    return list(dict.fromkeys(commands))


def text_field(item, key):
    if not isinstance(item.get(key), str) or not item[key].strip():
        raise ProviderError(f"Model output has an empty or missing {key}.")


def validate_checks(items):
    if not isinstance(items, list):
        raise ProviderError("Model output checks must be an array.")
    for item in items:
        if not isinstance(item, dict):
            raise ProviderError("Model output has an invalid check.")
        for key in ("command", "outcome", "evidence", "purpose"):
            text_field(item, key)
        if set(item) != {"command", "outcome", "evidence", "purpose"}:
            raise ProviderError("Model output check has unexpected fields.")
        if item["outcome"] not in ("pass", "fail", "uncertain"):
            raise ProviderError("Model output has an invalid check outcome.")
        if item["purpose"] not in ("acceptance", "exploration"):
            raise ProviderError("Model output has an invalid check purpose.")


def ensure_card_commands(report, required_commands):
    for command in required_commands:
        matches = [item for item in report["checks"] if item["command"] == command]
        if matches:
            for item in matches:
                item["purpose"] = "acceptance"
        else:
            report["checks"].append({
                "command": command, "outcome": "uncertain", "purpose": "acceptance",
                "evidence": "The reviewer did not report this command from the review card.",
            })


def tag_incomplete_checks(report, previous_ids=()):
    next_number = max((int(id_[2:]) for id_ in previous_ids), default=0) + 1
    for item in report["checks"]:
        if item["purpose"] == "acceptance" and item["outcome"] != "pass":
            item["id"] = f"C-{next_number:02d}"
            next_number += 1


def validate_findings(items, previous_ids=()):
    if not isinstance(items, list):
        raise ProviderError("Model output findings must be an array.")
    seen = set(previous_ids)
    for item in items:
        if not isinstance(item, dict):
            raise ProviderError("Model output has an invalid finding.")
        for key in ("id", "severity", "axis", "location", "problem", "evidence", "suggested_check"):
            text_field(item, key)
        if set(item) != {"id", "severity", "axis", "location", "problem", "evidence", "suggested_check"}:
            raise ProviderError("Model output finding has unexpected fields.")
        if not re.fullmatch(r"F-[0-9]{2,}", item["id"]) or item["id"] in seen:
            raise ProviderError(f"Model output has a duplicate or invalid finding ID: {item['id']}")
        if item["severity"] not in ("critical", "major", "minor") or item["axis"] not in (
            "spec", "standards", "both"
        ):
            raise ProviderError(f"Model output has invalid severity or axis for {item['id']}.")
        seen.add(item["id"])


def validate_discovery(data):
    if not isinstance(data, dict):
        raise ProviderError("Model output must be a JSON object.")
    text_field(data, "summary")
    validate_findings(data.get("findings"))
    validate_checks(data.get("checks"))


def validate_verification(data, open_items, open_check_items, responses, previous_ids):
    if not isinstance(data, dict):
        raise ProviderError("Model output must be a JSON object.")
    text_field(data, "summary")
    validate_checks(data.get("checks"))
    validate_findings(data.get("new_findings"), previous_ids)
    dispositions = data.get("dispositions")
    if not isinstance(dispositions, list):
        raise ProviderError("Model output dispositions must be an array.")
    expected = {item["finding"]["id"]: item for item in open_items}
    seen = set()
    for item in dispositions:
        if not isinstance(item, dict):
            raise ProviderError("Model output has an invalid disposition.")
        for key in ("id", "status", "evidence"):
            text_field(item, key)
        finding_id = item["id"]
        if finding_id not in expected or finding_id in seen:
            raise ProviderError(f"Model output has an unknown or duplicate disposition: {finding_id}")
        if item["status"] not in ("resolved", "deferred", "unresolved"):
            raise ProviderError(f"Model output has an invalid status for {finding_id}.")
        if item["status"] == "deferred" and (
            expected[finding_id]["response"]["action"] != "defer"
            or expected[finding_id]["finding"]["severity"] == "critical"
        ):
            raise ProviderError(f"Model output cannot defer {finding_id}.")
        seen.add(finding_id)
    if seen != set(expected):
        raise ProviderError("Model output omitted dispositions for: " + ", ".join(sorted(set(expected) - seen)))
    check_dispositions = data.get("check_dispositions")
    if not isinstance(check_dispositions, list):
        raise ProviderError("Model output check_dispositions must be an array.")
    expected_checks = {item["id"]: item for item in open_check_items}
    seen_checks = set()
    passing_commands = {item["command"] for item in data["checks"] if item["outcome"] == "pass"}
    for item in check_dispositions:
        if not isinstance(item, dict):
            raise ProviderError("Model output has an invalid check disposition.")
        for key in ("id", "status", "evidence"):
            text_field(item, key)
        if not isinstance(item.get("replacement_command"), str):
            raise ProviderError("Model output check disposition is missing replacement_command.")
        if not isinstance(item.get("equivalent"), bool):
            raise ProviderError("Model output check disposition is missing equivalent judgment.")
        if item["id"] not in expected_checks or item["id"] in seen_checks:
            raise ProviderError(f"Model output has an unknown or duplicate check disposition: {item['id']}")
        if item["status"] not in ("resolved", "unresolved"):
            raise ProviderError(f"Model output has an invalid check disposition for {item['id']}.")
        if item["status"] == "resolved":
            authorized = {expected_checks[item["id"]]["command"]}
            response = responses.get(item["id"], {})
            if response.get("action") == "replace":
                authorized.add(response["command"])
            if item["replacement_command"] not in authorized or item["replacement_command"] not in passing_commands:
                raise ProviderError(f"Resolved check {item['id']} needs the original or builder-authorized passing command.")
            if item["replacement_command"] != expected_checks[item["id"]]["command"] and item.get("equivalent") is not True:
                raise ProviderError(f"Resolved check {item['id']} needs an explicit equivalence judgment.")
        seen_checks.add(item["id"])
    if seen_checks != set(expected_checks):
        raise ProviderError("Model output omitted check dispositions for: " + ", ".join(sorted(set(expected_checks) - seen_checks)))


def codex(repo, prompt, schema, model):
    with tempfile.TemporaryDirectory(prefix="adversarial-review-") as directory:
        output = Path(directory) / "answer.json"
        command = [
            "codex", "exec", "--ignore-user-config", "-s", "read-only",
            "-c", "project_doc_max_bytes=0", "-c", "model_reasoning_effort=high", "-C", str(repo),
            "--output-schema", str(ROOT / "schemas" / schema),
            "--output-last-message", str(output),
        ]
        if model:
            command.extend(["--model", model])
        command.append("-")
        try:
            result = subprocess.run(command, input=prompt, text=True, capture_output=True, timeout=900)
        except FileNotFoundError as exc:
            raise ProviderError("Codex CLI is missing. Install it, then run codex login.") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError("Codex timed out after 15 minutes. The ledger was not changed.") from exc
        if result.returncode or not output.is_file() or not output.read_text().strip():
            detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "no response"
            raise ProviderError(f"Codex did not complete (exit {result.returncode}): {detail}")
        try:
            return json.loads(output.read_text())
        except json.JSONDecodeError as exc:
            raise ProviderError("Codex returned malformed JSON. The ledger was not changed.") from exc


def all_findings(ledger):
    findings = {item["id"]: item for item in ledger["discovery"]["findings"]}
    for round_ in ledger["rounds"]:
        findings.update({item["id"]: item for item in round_["new_findings"]})
    return findings


def all_incomplete_checks(ledger):
    checks = {}
    for report in (ledger["discovery"], *ledger["rounds"]):
        checks.update({item["id"]: item for item in report["checks"] if "id" in item})
    return checks


def superseded_context(ledger_file):
    history = ledger_file.parent / "history"
    files = sorted(history.glob("*.json")) if history.is_dir() else []
    if not files:
        return None
    previous = load_ledger(files[-1])
    if previous.get("close_status") != "SUPERSEDED REVIEW":
        return None
    findings = open_findings(previous)
    checks = open_checks(previous)
    if not findings and not checks:
        return None
    return {"archive": files[-1].name, "card": previous.get("card_body", ""),
            "findings": findings, "checks": checks}


def carry_superseded(report, context):
    if not context:
        return
    next_id = max((int(item["id"][2:]) for item in report["findings"]), default=0) + 1
    for old in context["findings"]:
        report["findings"].append({
            **old, "id": f"F-{next_id:02d}",
            "problem": f"Prior unresolved finding {old['id']}: {old['problem']}",
            "evidence": f"Carried from {context['archive']}. {old['evidence']}",
        })
        next_id += 1
    for old in context["checks"]:
        report["findings"].append({
            "id": f"F-{next_id:02d}", "severity": "major", "axis": "spec",
            "location": "review-card.md", "problem": f"Prior unresolved acceptance check {old['id']}: {old['command']}",
            "evidence": f"Carried from {context['archive']}. Prior outcome: {old['outcome']}. {old['evidence']}",
            "suggested_check": f"Run the original command or explain why it no longer applies: {old['command']}",
        })
        next_id += 1


def open_checks(ledger):
    open_ids = set(all_incomplete_checks(ledger))
    for round_ in ledger["rounds"]:
        for item in round_["check_dispositions"]:
            if item["status"] == "resolved":
                open_ids.discard(item["id"])
    checks = all_incomplete_checks(ledger)
    return [checks[id_] for id_ in sorted(open_ids)]


def replaced_checks(ledger):
    checks = all_incomplete_checks(ledger)
    return [
        (item["id"], checks[item["id"]]["command"], item["replacement_command"])
        for round_ in ledger["rounds"]
        for item in round_["check_dispositions"]
        if item["status"] == "resolved" and item["replacement_command"] != checks[item["id"]]["command"]
    ]


def open_findings(ledger):
    open_ids = set(all_findings(ledger))
    for round_ in ledger["rounds"]:
        for item in round_["dispositions"]:
            if item["status"] in ("resolved", "deferred"):
                open_ids.discard(item["id"])
    findings = all_findings(ledger)
    return [findings[id_] for id_ in sorted(open_ids)]


def final_status(ledger):
    if open_findings(ledger) or open_checks(ledger):
        if len(ledger["rounds"]) >= MAX_VERIFY:
            return "MANUAL HANDOFF"
        return "ACTION REQUIRED" if open_findings(ledger) else "NEEDS HUMAN CHECK"
    deferred = any(
        item["status"] == "deferred"
        for round_ in ledger["rounds"]
        for item in round_["dispositions"]
    )
    replaced = bool(replaced_checks(ledger))
    if deferred and replaced:
        return "APPROVED WITH DEFERRED FINDINGS AND REPLACED CHECKS"
    if deferred:
        return "APPROVED WITH DEFERRED FINDINGS"
    return "APPROVED WITH REPLACED CHECKS" if replaced else "APPROVED"


def command_preview(command):
    first = command.splitlines()[0]
    preview = first[:117] + "..." if len(first) > 120 else first
    return preview + " [multiline]" if "\n" in command else preview


def print_status(ledger, repo):
    head = git(repo, "rev-parse", "HEAD^{commit}")
    changes = git(repo, "status", "--short")
    stale = head != ledger["reviewed_head"] or bool(changes)
    if stale:
        print(f"STALE REVIEW: last reviewed {ledger['reviewed_head'][:12]}, current HEAD {head[:12]}.")
        if changes:
            print("Working tree changed after the review:")
            print("\n".join(changes.splitlines()[:10]))
    else:
        status = final_status(ledger)
        print(
            f"{status}: {len(open_findings(ledger))} open finding(s), "
            f"{len(open_checks(ledger))} open check(s), {len(ledger['rounds'])} verification round(s)."
        )
    for item in open_findings(ledger):
        response = ledger["responses"].get(item["id"], {}).get("action", "response needed")
        print(f"  {item['id']} [{item['severity']}] {item['location']}: {item['problem']} ({response})")
    for item in open_checks(ledger):
        print(f"  {item['id']} [{item['outcome']}] {command_preview(item['command'])}: {item['evidence']}")
    for check_id, original, replacement in replaced_checks(ledger):
        print(f"  {check_id} replaced check: {command_preview(original)} -> {command_preview(replacement)}")
    return 0 if not stale and status.startswith("APPROVED") else 3


def start(args, repo, ledger_file):
    if ledger_file.exists():
        raise ReviewError(f"A ledger already exists at {ledger_file}. Finish or archive it before starting another review.")
    head = clean_head(repo)
    base = git(repo, "rev-parse", f"{args.base}^{{commit}}")
    if not is_ancestor(repo, base, head):
        raise ReviewError("The base must be an ancestor of HEAD.")
    if base == head or not git(repo, "diff", "--name-only", base, head):
        raise ReviewError("The base and HEAD have no changed files to review.")
    card_path, card = card_text(repo, args.card)
    required_commands = card_commands(card)
    print("Required card commands: " + ", ".join(required_commands), flush=True)
    prior = superseded_context(ledger_file)
    prompt = (ROOT / "prompts" / "discovery.md").read_text().format(
        base=base, head=head, card=card, card_commands=json.dumps(required_commands),
        superseded=json.dumps(prior, indent=2) if prior else "None.",
    )
    report = codex(repo, prompt, "discovery.json", args.model)
    validate_discovery(report)
    ensure_card_commands(report, required_commands)
    carry_superseded(report, prior)
    tag_incomplete_checks(report)
    ledger = {
        "version": 3,
        "repo": str(repo),
        "base": base,
        "reviewed_head": head,
        "card_path": card_path,
        "card_sha256": hashlib.sha256(card.encode()).hexdigest(),
        "card_body": card,
        "card_commands": required_commands,
        "discovery": report,
        "responses": {},
        "rounds": [],
    }
    save_ledger(ledger_file, ledger)
    return print_status(ledger, repo)


def respond(args, ledger, ledger_file):
    if not args.reason.strip():
        raise ReviewError("Give an evidence-based reason for the response.")
    if args.id in {item["id"] for item in open_checks(ledger)}:
        if args.action != "replace" or not args.replacement_command or not args.replacement_command.strip():
            raise ReviewError("An open check accepts only replace with a nonempty --command.")
        check = all_incomplete_checks(ledger)[args.id]
        if check["outcome"] != "uncertain":
            raise ReviewError("Only an uncertain check can use a replacement; a failed check must pass its original command.")
        if check["command"] in ledger["card_commands"]:
            raise ReviewError("A review card command cannot be replaced. Revise the card, archive this review as superseded, and restart; unresolved items carry forward.")
        candidates = {
            item["command"]
            for item in ledger["discovery"]["checks"]
            if item["purpose"] == "acceptance"
            and item["command"] != check["command"]
        }
        if args.replacement_command.strip() not in candidates:
            raise ReviewError("Replacement must be an acceptance command reported in the first review.")
        ledger["responses"][args.id] = {
            "action": "replace", "reason": args.reason.strip(), "command": args.replacement_command.strip(),
        }
    elif args.id in {item["id"] for item in open_findings(ledger)}:
        if args.action == "replace" or args.replacement_command:
            raise ReviewError("A finding accepts fix, reject, or defer without --command.")
        if args.action == "defer" and all_findings(ledger)[args.id]["severity"] == "critical":
            raise ReviewError("Critical findings cannot be deferred.")
        ledger["responses"][args.id] = {"action": args.action, "reason": args.reason.strip()}
    else:
        raise ReviewError(f"{args.id} is not an open finding or check.")
    save_ledger(ledger_file, ledger)
    print(f"Recorded {args.action} response for {args.id}.")
    return 0


def verify(args, repo, ledger, ledger_file):
    findings = open_findings(ledger)
    check_items = open_checks(ledger)
    if not findings and not check_items:
        return print_status(ledger, repo)
    if len(ledger["rounds"]) >= MAX_VERIFY:
        raise ReviewError("Two verification rounds have already run. Review remaining findings with a person.")
    missing = [item["id"] for item in findings if item["id"] not in ledger["responses"]]
    if missing:
        raise ReviewError("Respond to every open finding first: " + ", ".join(missing))
    head = clean_head(repo)
    if not is_ancestor(repo, ledger["reviewed_head"], head):
        raise ReviewError("Current HEAD does not descend from the reviewed commit. Start a new review.")
    if head == ledger["reviewed_head"] and any(
        ledger["responses"][item["id"]]["action"] == "fix" for item in findings
    ):
        raise ReviewError("A fix response needs a new committed revision before verification.")
    _, card = card_text(repo, ledger["card_path"])
    if hashlib.sha256(card.encode()).hexdigest() != ledger.get("card_sha256"):
        raise ReviewError("The review card changed since discovery. Restore it or start a new review against revised criteria.")
    open_items = [
        {"finding": item, "response": ledger["responses"][item["id"]]}
        for item in findings
    ]
    prompt = (ROOT / "prompts" / "verification.md").read_text().format(
        previous_head=ledger["reviewed_head"],
        head=head,
        card=card,
        card_commands=json.dumps(ledger["card_commands"]),
        open_items=json.dumps(open_items, indent=2),
        open_checks=json.dumps([
            {"check": item, "builder_response": ledger["responses"].get(item["id"])}
            for item in check_items
        ], indent=2),
    )
    report = codex(repo, prompt, "verification.json", args.model)
    validate_verification(report, open_items, check_items, ledger["responses"], all_findings(ledger))
    ensure_card_commands(report, ledger["card_commands"])
    tag_incomplete_checks(report, all_incomplete_checks(ledger))
    report["builder_responses"] = dict(ledger["responses"])
    report["from_head"] = ledger["reviewed_head"]
    report["to_head"] = head
    ledger["rounds"].append(report)
    ledger["responses"] = {}
    ledger["reviewed_head"] = head
    save_ledger(ledger_file, ledger)
    return print_status(ledger, repo)


def archive(args, repo, ledger, ledger_file):
    status = final_status(ledger)
    current_head = git(repo, "rev-parse", "HEAD^{commit}")
    try:
        _, card = card_text(repo, ledger["card_path"])
        changed_card = hashlib.sha256(card.encode()).hexdigest() != ledger["card_sha256"]
    except ReviewError:
        changed_card = True
    superseded = changed_card or not commit_exists(repo, ledger["reviewed_head"]) or not is_ancestor(
        repo, ledger["reviewed_head"], current_head,
    )
    if (open_findings(ledger) or open_checks(ledger)) and len(ledger["rounds"]) < MAX_VERIFY and not superseded:
        raise ReviewError("Respond to open findings and finish verification before archiving.")
    stale = current_head != ledger["reviewed_head"] or bool(git(repo, "status", "--short"))
    if not args.reason.strip():
        raise ReviewError("Give the human decision or completion reason before archiving.")
    history = ledger_file.parent / "history"
    history.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archived = history / f"{stamp}-{uuid4().hex[:8]}.json"
    if superseded:
        close_status = "SUPERSEDED REVIEW"
    elif stale and status == "MANUAL HANDOFF":
        close_status = "STALE MANUAL HANDOFF"
    else:
        close_status = "STALE REVIEW" if stale else status
    record = {**ledger, "closed_at": stamp, "close_status": close_status, "close_reason": args.reason.strip()}
    with archived.open("x") as file:
        json.dump(record, file, indent=2)
        file.write("\n")
    ledger_file.unlink()
    print(f"Archived {close_status} review to {archived}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "respond", "verify", "status", "archive"):
        command = commands.add_parser(name)
        command.add_argument("--repo", default=".", help="Git repository root to review")
        if name in ("start", "verify"):
            command.add_argument("--model", help="Codex model; otherwise use your configured default")
        if name == "start":
            command.add_argument("--base", required=True, help="committed baseline ref")
            command.add_argument("--card", required=True, help="review input card inside the repository")
        if name == "respond":
            command.add_argument("--id", required=True)
            command.add_argument("--action", required=True, choices=("fix", "reject", "defer", "replace"))
            command.add_argument("--reason", required=True)
            command.add_argument("--command", dest="replacement_command", help="replacement command for an open check")
        if name == "archive":
            command.add_argument("--reason", required=True, help="completion or human handoff decision")
    args = parser.parse_args()
    try:
        repo = repository(args.repo)
        ledger_file = ledger_path(repo)
        if args.command == "start":
            return start(args, repo, ledger_file)
        ledger = load_ledger(ledger_file)
        if args.command == "respond":
            return respond(args, ledger, ledger_file)
        if args.command == "verify":
            return verify(args, repo, ledger, ledger_file)
        if args.command == "archive":
            return archive(args, repo, ledger, ledger_file)
        return print_status(ledger, repo)
    except ReviewError as exc:
        print(f"review: {exc}", file=sys.stderr)
        return 2
    except ProviderError as exc:
        print(f"review: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
