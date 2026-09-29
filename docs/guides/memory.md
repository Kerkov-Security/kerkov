# Memory: Recall and the Ledger

Long engagements outgrow the model's context window. Kerkov keeps two kinds of
memory so nothing important is lost when older messages are summarized away:

- The **findings ledger** — a small, structured record of confirmed facts that
  is re-shown to the model every turn.
- **Recall** — a searchable archive of every tool result, so bulky output stays
  retrievable even after it leaves the conversation.

Both are on by default (`KB_ENABLED=true`) and stored locally, per engagement
tree, in `~/.kerkov/engagements/<root-id>.kb.sqlite`.

## The findings ledger

The ledger holds confirmed facts in fixed sections: **hosts, services,
credentials, findings, flags, and next steps**. It is re-injected at the top of
context on every turn, so it survives compaction verbatim — this is the fix for
"the model forgot the credential it found forty messages ago."

The model writes to it as it verifies things, and you can prompt it to:

```text
kerkov › Record the admin credential we recovered and the SSH host in the ledger.
kerkov › Show me the ledger.
```

What to expect:

- **Entries merge, they don't duplicate.** A later detail about the same host or
  credential enriches the existing row rather than adding a second one.
- **Secrets are stored in cleartext.** Harvested credentials are kept and
  re-shown unmasked. The file never leaves your machine on its own, but the ledger
  is part of every request Kerkov makes, so a hosted provider sees those
  credentials every turn.
- **It's bounded.** If the ledger grows very large, entries are trimmed from the
  injected copy — host and service enumeration first, credentials, flags, and
  findings last. Nothing is deleted — a full listing still shows everything —
  only the always-in-context copy is trimmed.

## Recall

Every tool result is indexed into the knowledge base as it's produced. Large
results are replaced in the conversation with a compact `artifact#N` reference,
keeping context lean while the full output stays retrievable.

Ask for it in plain language, or the model uses the `recall` tool directly:

```text
kerkov › Recall the full nmap output from earlier.
kerkov › What did the directory brute-force turn up? Search recall for it.
```

How it behaves:

- **Snippet-first.** A recall search returns short matches; expanding a specific
  `artifact#N` returns the full stored output.
- **Offline by default.** Search is local full-text (FTS5); no data leaves the
  machine. Semantic search is opt-in (`KB_EMBEDDINGS`) and inert until a backend
  is configured.
- **Tree-wide.** Recall spans the whole engagement, including the raw output
  behind a delegated agent's summarized findings.

## What survives compaction

When context fills up, Kerkov compacts in a fixed order: it evicts large tool
results (leaving their `artifact#N` handles), summarizes the remaining
narrative, and eventually rolls summaries up. Through all of it:

- The **ledger** is never compacted — it lives outside the summarized history.
- Evicted tool output remains **recallable** by its artifact handle.
- Summaries keep the artifact handles inline, so references survive every level.

The practical upshot: confirmed facts stay in front of the model, and the
detailed evidence behind them stays one `recall` away — even in an engagement
long enough to have been summarized several times.

## Turning it off

Set `KB_ENABLED=false` to disable ingestion, `recall`, and `ledger` entirely.
Without the knowledge base, evicted tool output is not recoverable and the
ledger is unavailable — so leave it on unless you have a specific reason not to.
