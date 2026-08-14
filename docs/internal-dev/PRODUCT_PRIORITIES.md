# Product Priorities

Use this ordering for roadmap and architecture decisions that affect major
tools, engagement persistence, proxy integration, remote execution, or safety
policy:

1. Close execution, approval, delegation, scope, and audit safety gaps.
2. Build a typed, redacted engagement ledger separate from conversation JSONL.
3. Add a vendor-neutral intercepting-proxy adapter, with Burp first.
4. Add a local, SSH, and container work-host abstraction beneath existing
   tools.

Defer raw tool-count parity, Mythic integration, and multi-model racer swarms
until these foundations are complete.
