---
name: review-pr
description: Review a Kerkov pull request (or branch) from an isolated git worktree so the surrounding code on that branch can be read directly, then clean the worktree up. Optionally post findings to the PR as inline comments. Use when the user asks to review a PR/branch, e.g. "/review-pr 214" or "/review-pr 214 --post".
argument-hint: <PR number | branch> [--post]
---

Review target: $ARGUMENTS

`--post` means publish the findings to the PR when the review is done. Without it, report in the terminal only and post nothing.

## 1. Open the worktree

The point of the worktree is that you are reading the branch as it actually is — whole files, callers, tests, config — not just diff hunks, and without touching the user's checkout (which may have uncommitted work).

For a PR number:

```bash
gh pr view <N> --repo Kerkov-Security/kerkov --json number,title,body,baseRefName,headRefOid,url
git fetch origin "pull/<N>/head" "<baseRefName>"
git worktree add --detach "<scratchpad>/review-pr<N>" <headRefOid>
```

For a branch name, fetch it and add a detached worktree at `origin/<branch>` (or the local ref if it has no remote), under `<scratchpad>/review-<branch-slug>`. `--post` does not apply to a bare branch unless it has an open PR.

Use a detached worktree — do not create a local branch; PR branches get squashed on the remote and a local copy just goes stale. Before adding, `git worktree prune` and remove any leftover worktree at the same path from an earlier run.

Diff against the merge base: `git -C <wt> diff origin/<base>...HEAD`. Run every other command with `git -C <wt>` or absolute paths inside `<wt>`; do not `cd` into it and forget to leave.

## 2. Review

How to review is your call — scale depth to the size and risk of the change. A few things that tend to matter here:

- `AGENTS.md` **in the worktree** is the bar for conventions and for the architectural invariants the change must not break. If the PR changes behaviour that `AGENTS.md` describes and doesn't update it, that is a finding.
- Read the code the diff calls into and the code that calls it. Most real bugs in this repo live at those seams (agent lifecycle, approval paths, KB/ledger keying, compaction) rather than inside a hunk.
- Running the relevant tests or `mypy` in the worktree is fine when it would settle a question (`uv run --directory <wt> ...`). Not required.
- Prefer a few findings you have actually checked over a long list of maybes. Say how confident you are when it isn't obvious.

Each finding needs: file (repo-relative), line on the PR's head commit, severity, what's wrong, and a concrete scenario where it bites.

Write findings tersely — a sentence or two saying what breaks and when, plus a fix if it isn't obvious. No preamble, no restating the code, no praise. Be concise per finding, not selective about findings: every real issue gets raised, however small, but one related problem is one comment (point at the other sites in it rather than repeating it at each). Skip pure style nits that `black`/`isort`/`mypy` would catch anyway.

## 3. Report

Print the findings in the terminal, most severe first, with a one-line overall verdict. If there are none worth raising, say so.

With `--post`, publish them as **one** review, not a comment per call:

```bash
gh api repos/Kerkov-Security/kerkov/pulls/<N>/reviews --method POST --input <scratchpad>/review-<N>.json
```

with a body like `{"commit_id": "<headRefOid>", "event": "COMMENT", "body": "<summary>", "comments": [{"path": ..., "line": ..., "side": "RIGHT", "body": ...}]}`.

- Keep the review `body` to a short verdict plus any out-of-diff findings; don't summarise the PR back to its author.
- Always `event: "COMMENT"` — never `APPROVE` or `REQUEST_CHANGES`; that decision stays with the user.
- Inline comments can only anchor to lines inside the diff. Findings on lines outside it go in the review `body` with a `path:line` reference instead.
- If the API rejects a comment's position, move that finding into the body and retry rather than dropping it.
- Report the review URL afterwards.

## 4. Clean up

Always, including when the review or posting failed partway:

```bash
git worktree remove --force "<scratchpad>/review-pr<N>"
git worktree prune
```

Confirm with `git worktree list` that it is gone, and delete the review JSON if one was written.
