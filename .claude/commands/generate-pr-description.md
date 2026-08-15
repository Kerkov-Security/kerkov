You are generating a PR description for the kerkov project. The user will provide a PR URL or number as the argument: $ARGUMENTS

Follow these steps exactly:

## Step 1: Extract PR details

Use `gh` to fetch the PR metadata and diff:

```
gh pr view <PR> --repo Kerkov-Security/kerkov --json title,body,headRefName,baseRefName,number,url
gh pr diff <PR> --repo Kerkov-Security/kerkov
```

Also fetch the list of commits on the PR:

```
gh pr view <PR> --repo Kerkov-Security/kerkov --json commits
```

## Step 2: Understand the change

Read through the diff carefully. For any files that are substantially modified or newly created, read the full file (not just the diff hunks) to understand the broader context — how the changed code fits into the existing architecture, what it replaces, and why.

Pay attention to:
- New classes, modules, or abstractions introduced
- Changes to existing interfaces or contracts
- Removed or deprecated code
- Test coverage additions
- Configuration or dependency changes

## Step 3: Generate the description

Write the generated markdown to a file called `temp_pr_desc.md` in the project root. Do not use emojis anywhere. After writing, tell the user the file is ready.

Use exactly this format:

---

## Why did you make these changes?

Write 2-4 sentences explaining the motivation. Focus on the problem being solved or capability being added — what was missing, broken, or insufficient before. Be direct and concise.

## What did you change?

Group changes by area of the codebase using `###` subsections (e.g., by directory or logical layer). Under each subsection, use bullet points describing what each file or component change does. Be specific — mention class names, file paths, and behavioral changes. Keep descriptions concise but precise.

Example structure:
```
### CLI Layer (`src/kerkov/cli/`)
  - `some_file.py` -- description of what changed and why

### Tools (`src/kerkov/tools/`)
  - `new_tool.py` -- what it does
  - `existing_tool.py` -- what was modified
```

## Acceptance Criteria

List concrete, verifiable criteria as a markdown checklist. These should be things a reviewer can check — not vague statements. Examples: "No syntax errors", "Unit tests pass", "New tool registered in tool suite", specific behavioral requirements.

## Link to Issue/Ticket

If the PR references an issue (check the PR body, branch name, or commits for issue numbers), include the link here. If none is found, leave this section empty.

---

## Style rules

- No emojis anywhere in the output
- Be concise — avoid filler language and unnecessary elaboration
- Use technical precision: mention specific class names, file paths, patterns
- The "Why" section should be a brief narrative, not a changelog
- The "What" section should be a structured reference for reviewers
- Acceptance criteria should be actionable, not aspirational
- Match the tone of the existing PRs: direct, technical, first-person where appropriate
