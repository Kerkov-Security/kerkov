# Security policy

Kerkov is alpha software (`0.1.0`). Only the `main` branch receives fixes; there
are no maintained release branches yet.

## Reporting a vulnerability

Report privately through GitHub's **[private vulnerability
reporting](https://github.com/Kerkov-Security/kerkov/security/advisories/new)** — open a
draft advisory rather than a public issue or a pull request.

Please include the affected version or commit, the provider and front end (CLI
or web console) involved, and the minimum steps to reproduce. We are a small
team and do not offer a response-time guarantee or a bounty; we will acknowledge
a report and tell you whether we intend to fix it.

## In scope

Flaws in Kerkov itself, such as:

- Approval bypass — a tool call reaching execution without a matching rule or an
  operator decision, or an `approval_scope()` that generalizes an approval
  further than the operator agreed to.
- Credential exposure — API keys or OAuth state leaking into chat, logs,
  transcripts, ordinary settings or a subprocess environment.
- Web console access control — defeating the loopback, host, origin or
  access-token checks, or reading another browser session's engagement.
- Agent containment — a delegated agent escaping its budget, its run workspace or
  its engagement's knowledge base.

## Known and intentional properties

These are design decisions, documented in `docs/`, not vulnerabilities:

- **Kerkov executes attacker-adjacent tooling on purpose.** It is built for
  authorized penetration testing; that the operator can run destructive commands
  against a target they have approved is the product working.
- **The knowledge base and findings ledger are plaintext on local disk** and hold
  whatever the tools returned, harvested credentials included. Nothing is
  redacted or masked, and nothing leaves the machine.
- **The web console has no user-account authentication.** It is loopback-only and
  trusts whoever can reach it on the local machine.
- Using Kerkov against systems you are not authorized to test is a misuse report
  for us, not a vulnerability report — but tell us anyway if you see it happening
  under the project's name.
