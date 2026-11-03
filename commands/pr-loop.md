---
description: Loop on an autopilot PR until it is clean. CI green first, then parallel adversarial reviewers by role, triage in this session, one fixer pushes, repeat until CI is green and no reviewer finds anything. Started by an autopilot worker.
argument-hint: "<PR URL> plus the brief: run directory, worktree, path of ap"
---

# PR Review Loop

One round = CI green → adversarial review → triage → fix → push. CI comes first: no reviewer runs until
every CI check on the current head is green. Repeat rounds until a round ends with every CI check green
AND zero accepted findings. That is the only success exit.

Argument: $ARGUMENTS

## Step 0: Resolve the PR

The argument holds the PR URL and the worker brief (run directory, worktree, path of `ap`). Without a
PR URL, write `prloop.json` with `"result": "stopped", "reason": "no PR given"` and stop.

Then collect:

    gh pr view <N> --json number,title,body,author,url,headRefName,headRefOid,baseRefName,files,isDraft,state

Hard stops (say why and end):
- `state` is not OPEN.
- `author.login` is not the login `gh api user --jq .login` prints: this loop pushes commits, so it
  only runs on the user's own PRs.

## Step 1: Pick the working tree (once, before any round)

Use the worktree from the brief. Every fixer in every round uses it. Before round 1:
`git -C <path> fetch origin <headRefName> && git -C <path> status`. If local and remote diverged, stop
and stop.

## Step 2: Classify the PR and choose the review angles

Read the file list and the diff. Pick the PR type(s), then the roles. Always 2 to 4 reviewers. Always
include Correctness.

| PR touches | Roles to add beside Correctness |
|---|---|
| UI / React / React Native | Cross-platform parity (web vs native), Accessibility & UX |
| Backend service / API / RPC / proto | API contract & backward compatibility, Failure modes & observability |
| CI / workflows / scripts / tooling | Portability (shells, OS, runner image), Security of secrets & permissions |
| A tool or harness other teams use | Blast radius (who else runs it, what changes for them), Flakiness & timing |
| Infra / Kubernetes / Terraform | Blast radius & rollback, Security & least privilege |
| Data / SQL / migrations | Data correctness & backfill, Performance & locking |
| Tests only | Test value (does it fail when the code breaks?), Flakiness & isolation |
| Docs / config only | Accuracy against the code it describes, Reader clarity |

Mixed PR: pick the roles for the riskiest part. Write the roles and a one-line reason into the round
report. Keep the same roles across rounds unless the diff changes kind.

## Step 3: CI gate (every round)

Wait for every check on the current head SHA (`gh pr checks <N> --watch` in the background; no tight
polling). Skipped or neutral counts as green; pending does not. A check that only waits for a human
approval (for example a policy bot) is not CI: note it and go on.

- All green: go to Step 4.
- A check fails: read the failed log. Caused by the PR: it becomes an accepted finding (id `ci-<n>`);
  print a CI-only round report and go to Step 6 without reviewers. A flake or infra failure: re-run it
  once; failing again the same way is a stop.

## Step 4: Adversarial review (parallel, read-only)

Spawn all reviewers in one message so they run at the same time. Each reviewer is a FRESH agent every
round: never continue an earlier reviewer, it would defend its own earlier view. Tell each one: read-only,
no edits, no commits, no comments anywhere.

Each reviewer prompt contains, in this order:
1. The role, and the one question that role answers. Frame it adversarially: "your job is to find how
   this PR breaks from your angle; assume it does until the code shows otherwise."
2. PR number, head SHA, base branch, worktree path. Review that SHA.
3. The round number. From round 2 on: the findings already rejected or deferred in earlier rounds, with
   the reason, and "do not re-raise these unless you have new evidence against the reason". Also give
   facts the loop learned (a CI failure, a measurement) as facts, not as conclusions.
4. The return contract:

       - id: <role>-<n>
         severity: blocker | should-fix | nit
         where: file:line (head version)
         trigger: concrete input or state, with real values
         before: what that input did on base
         after: what it does now, step by step
         impact: what a user / caller / CI run actually observes
         fix: smallest change that fixes it

   A reviewer that finds nothing returns exactly NO FINDINGS. Say that NO FINDINGS is a good answer
   and that invented findings waste a round.

## Step 5: Triage (in the main session, never in a subagent)

Verify each finding against the code before accepting it: open the `where`, check the `trigger`, and
run something if that settles it. Reviewer confidence is not evidence. Do not agree to be agreeable;
when a finding is wrong, say why.

Put each finding in exactly one bucket:

| Bucket | Meaning | Goes to the fixer? |
|---|---|---|
| Fix now | Reproducible, real impact, in this PR's scope | yes |
| Real, out of scope | Pre-existing or outside the PR | no: list as a follow-up |
| Invalid | Misread code, impossible trigger, or pure taste | no: record the reason |
| Needs the owner | A design trade-off or product call | no: it ends the loop as `stopped` |

Merge duplicates across reviewers. A nit goes to the fixer only if it is a one-line change with no risk;
otherwise it is taste. A wording fix in the PR body you can make yourself, without a commit.

Print the round report, then: "Fix now" is empty → success exit (re-check that the head SHA is still the
reviewed one). Otherwise → Step 6.

## Step 6: Fix and push (one fixer)

Spawn ONE fixer agent (two would commit over each other) with: the worktree and branch ("first
`git pull --ff-only`"); the accepted findings verbatim with your verification note for each; the rules:
fix only these, no drive-by cleanup; run the checks for the touched area before committing and stop if
one fails and the fix is not obvious; one conventional commit; plain `git push` only, never force,
rebase or merge; never comment on the PR. It returns the SHA, what changed per finding, and the checks.

Confirm the push yourself (`gh pr view <N> --json headRefOid`). A finding it could not fix ends the loop as
`stopped`. Then start the next round at Step 3 with the new head.

## Stop conditions (besides success)

Stop, print the last round report and say why when:
- round 6 ends without success (the loop is not converging);
- the same finding (same place, same problem) is accepted in two rounds (the fix is not working);
- anything is in "Needs the owner";
- the branch moved on the remote without your push.

A round with no behavior finding for the second time in a row also ends the loop: it has converged.

## Round report (every round)

    Round N · head <short SHA> · CI gate: <green | red: which check> · reviewers: <roles> (<why these>)

    Fix now (<n>)
    - <id> <one line> — <file:line> — verified: <how>
    Real, out of scope (<n>)
    - <id> <one line> — follow-up
    Invalid (<n>)
    - <id> <one line> — reason: <why it does not hold>
    Needs the owner (<n>)
    - <id> <question> — options A / B

    Fix: <commit SHA, or none>
    Next: <round N+1 | success | stop, because ...>

## Final report

Write `prloop.json` in the run directory:

```json
{
  "result": "clean|converged|stopped", "reason": "why it stopped, if it did",
  "head": "<SHA>", "ci": "green|red", "rounds": [{"n": 1, "roles": ["..."], "findings": ["id"], "fixed": ["id"], "commit": "<SHA or null>"}],
  "followups": ["real, out of scope"], "questions": ["needs the owner"]
}
```

Then write `evidence.json` in the run directory. Every text field is `{"en": "...", "<reader_lang>": "..."}`
(only `en` when the reader's language is English; the brief says which):

```json
{
  "verdict": {"en": "one line: ready or not, and why"},
  "what_changed": {"en": "what changed and why, in plain words"},
  "criteria": [{"text": {"en": "..."}, "ok": true, "proof": {"en": "test name that fails on base, CI run, screenshot"}}],
  "screenshots": [{"path": "media/after.png", "caption": {"en": "what to look at"}}]
}
```

Pick at most 10 screenshots from `media/`, the ones that show the change (before and after where it
helps). Then run `<ap> kick` and end with one line naming both files.

## Never

- Merge, enable auto-merge, or mark the PR ready (the dispatcher decides that).
- Force-push, rebase, or push to any branch other than `headRefName`.
- Post, reply to or resolve anything on GitHub, chat or the ticket tracker, except resolving a
  review-bot thread whose finding you fixed.
- Let a reviewer edit files, or let two fixers run at once.
