# `read` and `write`

File access. `read` returns a file's contents as text; `write` creates,
overwrites, or appends to a file. Both resolve paths the same way, and neither
knows anything about the engagement beyond that.

## `read`

Reads a file and returns its contents. A file over the size limit is refused,
not truncated — filter it with [`exec`](exec-process.md) instead.

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `file_path` | string | **required** | Path to read. Relative paths resolve inside the engagement workspace; absolute paths are honoured as explicit operator paths. |
| `max_size` | integer | `10485760` (10 MB) | Size ceiling in bytes, checked before reading. Must be positive. It *replaces* the 10 MB default rather than capping it, so a larger value raises the limit as well as lowers it. |

### Approval

Auto-approved by the packaged rules (`allow: "read({})"`), in every mode except
`strict`. If you remove that rule, an approval saved from a `read` prompt widens
to **the directory containing the file** — `read` of one file in `/etc` offers
`/etc/*`, not all of `read`. A bare filename with no directory component has no
parent to widen to, so it saves as that call only. `max_size` never reaches a
saved rule.

### Returns

The file's text as output. Metadata carries `file_path` (fully resolved),
`file_size`, and `encoding`.

A file that will not decode as UTF-8 still succeeds, with `encoding: "binary"`:
the output is a `[Binary file - N bytes]` line followed by the first 500 bytes as
hex, so the model can identify the format without the bytes flooding context.

### Examples

```json
{ "file_path": "/etc/nginx/nginx.conf" }
```

```json
{ "file_path": "artifacts/sast/opengrep-toolu_01abc-9d2f4x.json", "max_size": 262144 }
```

The second path is relative, so it resolves under the engagement workspace —
which is where [`sast_scan`](sast.md) put that report, and where its
`metadata.artifacts.report` said it was.

## `write`

Writes content to a file, creating parent directories as needed. `content` must
be non-empty — writing an empty or whitespace-only string is rejected as a
probable mistake, so truncating a file is `exec`'s job.

### Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `file_path` | string | **required** | Path to write. Relative paths land in the engagement workspace; absolute paths are honoured as explicit operator paths. |
| `content` | string | **required** | Content to write. |
| `append` | boolean | `false` | Append instead of overwriting. |

### Approval

Prompts by default — there is no allow rule for `write`. An approval saved from
the prompt widens to **the directory containing the file**, so granting one
write into the workspace does not grant writes to `~/.ssh`.

`content` is never written into a saved rule. A rule pinned to one body would
never match a second time, and writing it down would copy whatever the file held
— a key, a token, a captured hash — into `approvals.yaml` in cleartext.

Set [`WRITE_TOOL_ENABLED=false`](../configuration.md#tool-execution) to refuse
every write regardless of approval mode.

### Returns

A confirmation line as output. Metadata carries `file_path`, `file_size`, `mode`
(`w` or `a`), and `workspace_path` — the workspace the relative path
resolved against, or `null` when none was active.

### Examples

```json
{ "file_path": "notes/10.10.10.5-enum.md", "content": "# 10.10.10.5\n\n- 22/tcp open ssh\n" }
```

```json
{ "file_path": "notes/10.10.10.5-enum.md", "content": "- 445/tcp open smb\n", "append": true }
```

## Path resolution

Both tools resolve a path through the same rule:

- **Absolute** (`/etc/passwd`, `~/loot/creds.txt`) — resolved as given. Treated
  as a deliberate operator path, and subject to approval like anything else.
- **Relative** (`notes/enum.md`) — resolved under the active engagement
  workspace. A relative path that would resolve outside the workspace, including
  through `..` or a symlink, is rejected.
- **Relative with no workspace active** — resolved against the current working
  directory.

The workspace lives under
[`WORKSPACE_DIR`](../configuration.md#storage-and-logging) and is shared by a
master and all of its agents, keyed on the root engagement. Two agents writing
the same relative filename will overwrite each other.

## Related

- [`exec`](exec-process.md) for anything requiring a command — `read` and `write`
  are not a shell, and a large file is often better filtered with `grep` than
  read whole.
- [`recall`](recall.md) — a file already read in this engagement is in the
  knowledge base; recalling it costs nothing and re-reading it costs context.
- [Approvals](../../guides/approvals.md) for the prompt and the rules file.
