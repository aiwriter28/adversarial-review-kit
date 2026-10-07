---
name: adversarial-review
description: Install and use the Adversarial Review Kit in Claude Code for an independent Codex review of committed code. Use for setup, F- or C-ID responses, fix verification, and restarting a superseded review.
---

# Adversarial review

You are helping a developer use a second model to review a committed change. Explain the next step in plain language and perform safe setup checks yourself. Keep the reviewer's context independent of the builder's conversation.

## Setup

When asked to set up, read `~/.local/share/adversarial-review-kit/README.md`. The kit's executable runner stays in that checkout; only this `SKILL.md` is installed as an agent skill. Explain that Codex is a separate, GPT-powered reviewer: a different model can challenge the builder's assumptions and look for missed edge cases, but its findings still need verification. Check `python3 --version` (3.9 or newer), `git --version`, `codex --version`, and `codex login status`. Tell the user the observed result of each check. They need Codex access through an eligible ChatGPT plan or separately billed API-key login; a paid subscription is useful for repeated reviews but is not universally required. Use the current OpenAI access links in the README rather than assuming plan eligibility. If Codex is missing, use the official Codex CLI installation instructions linked in the README. If login is missing, guide the user through `codex login`; do not request credentials in chat.

Use the personal skill location `~/.claude/skills/adversarial-review/SKILL.md` by default. If this skill is already running from that path, installation is complete. If the user chose a project installation, copy only this `SKILL.md` under that project's `.claude/skills/adversarial-review/` after inspecting existing files. Do not overwrite an existing installation or alter global Claude/Codex configuration silently.

Then ask which Git repository they want to review only if it is not clear from the current directory. Check its Git root, clean working tree, and current branch. If generated cache or build files appear in Git status, help the user ignore only the appropriate paths; keep source changes committed. Copy `~/.local/share/adversarial-review-kit/templates/review-card.md` into the target repo as `review-card.md` if one does not already exist, then help fill it with concrete expected behavior and independent checks. Keep an existing card and complete its required `## Test commands` section. Put each complete command on its own line in backticks, with no prose in that section. Choose commands that can run in Codex's read-only sandbox, since blocked cache or fixture writes remain open checks. Prefer to commit the card with the code change for reproducibility; an ignored card also works because the runner freezes its hash. Identify the baseline commit before the change. Explain that the runner stores its finding ledger inside Git metadata and invokes Codex with `read-only` sandbox access. Run the first review when the target commit and card are ready.

## Review

Run `python3 ~/.local/share/adversarial-review-kit/scripts/review.py start --repo <target-root> --base <baseline-sha> --card review-card.md`. Exit code 3 means findings need a response, checks need verification, or a prior approval is stale; read the printed status to tell which. Exit code 4 means Codex failed or returned invalid output; do not call that a pass. Read the ledger through `status` and inspect the evidence for each finding. For every finding ID, record `fix`, `reject`, or `defer` with a short reason using `respond`. Critical findings cannot be deferred. Keep major and minor findings visible and explicitly dispositioned.

For a `fix` response, change the code, run the relevant behavior checks, and commit the fix. Then run `verify`. A rejection needs evidence that the finding is false. A deferral needs the residual risk and a reason. Do not accept reviewer suggestions blindly or weaken an acceptance criterion to get a passing result.

## Verify

Run `python3 ~/.local/share/adversarial-review-kit/scripts/review.py verify --repo <target-root>`. This checks each open finding, every failed or uncertain acceptance check, the card's commands, and the new fix diff. A failed check must pass its original command. Only an uncertain check outside the card may use a builder-authorized acceptance command that the first review already reported, even if it passed. Explain the equivalence and use `respond --id C-XX --action replace --command <exact-command> --reason <why-equivalent>` before verification. The reviewer must confirm the equivalent behavior, and status will label any approval with replaced checks. If a card command cannot run in the read-only environment, revise the card, archive the old review as superseded, and start again; unresolved items carry forward. Report resolved, deferred, and unresolved findings, plus resolved and unresolved checks. If new or unresolved findings remain, respond and commit another fix before the second verification round. After two rounds, the runner requires a human handoff. Never describe an incomplete or stale review as approved.

After an approved review, or after two rounds and an explicit human handoff decision, use `archive --repo <target-root> --reason <decision>` before starting the next review. This preserves the full ledger under Git metadata. If later work made the review stale, the archive records that state instead of treating the new commit as approved. If history or the card changed before verification, archive it as `SUPERSEDED REVIEW`, then start a fresh review. The runner refuses to archive an otherwise unfinished review before its verification rounds are exhausted.

## Commands and limits

The README contains the exact command sequence and exit codes. The runner reviews committed checkpoints only, makes no code edits, and does not send messages or publish a repository. Keep the user's private code and the finding ledger out of this kit's public repository.
