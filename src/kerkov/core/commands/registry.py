"""The one table of slash commands: names, intent, and docs.

Both front ends read this. The CLI pairs each spec with a handler that builds a
terminal `UserMessage`; the web console exposes a subset in its suggestion menu.
`HelpEntry` and `CATEGORIES` live here (not in `cli/`) because this is the shared
layer — `cli/messages/help_entry.py` re-exports them for existing importers.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from kerkov.core.commands.intents import Intent

# Category display order for the grouped /help listing.
CATEGORIES = [
    "Session",
    "Modes & Autonomy",
    "Providers & Models",
    "Skills",
    "Agents",
    "Engagements",
    "Tools",
    "Auth",
]


@dataclass(frozen=True)
class HelpEntry:
    """Docs for a single slash command (paired with a CommandSpec)."""

    category: str
    summary: str
    syntax: List[str] = field(default_factory=list)
    details: str = ""
    examples: List[str] = field(default_factory=list)
    related: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class CommandSpec:
    """One slash command's identity (names + intent) and its docs.

    `names[0]` is canonical; the rest are aliases (e.g. `quit`, `exit` for `/q`).
    """

    names: List[str]
    intent: Intent
    help: HelpEntry

    @property
    def name(self) -> str:
        return self.names[0]

    @property
    def aliases(self) -> List[str]:
        return self.names[1:]


def _resource_spec(role: str, intent: Intent) -> CommandSpec:
    meaning = (
        "resources we protect"
        if role == "asset"
        else "subjects of offensive assessments"
    )
    return CommandSpec(
        names=[f"/{role}", f"/{role}s"],
        intent=intent,
        help=HelpEntry(
            category="Engagements",
            summary=f"Manage {role}s: {meaning}.",
            syntax=[
                f"/{role}",
                f"/{role} list",
                f'/{role} add <kind> <value> ["name"]',
                f"/{role} remove <id>",
            ],
            details=(
                "Kinds: domain, ip, url (HTTP/S), repository (absolute local path). "
                "Quote values or names containing spaces. Records belong to the "
                "current engagement, survive /resume, and are shared with its agents. "
                "Adding before the first chat message starts an engagement. "
                "Requires KB_ENABLED=true. Roles do not change tool approvals."
            ),
            examples=[
                f'/{role} add url https://api.example.com "Example API"',
                f"/{role}",
                f"/{role} remove 1",
            ],
            related=["/asset", "/target", "/resume"],
        ),
    )


COMMAND_SPECS: List[CommandSpec] = [
    # --- Session ------------------------------------------------------------
    CommandSpec(
        names=["/help"],
        intent=Intent.HELP,
        help=HelpEntry(
            category="Session",
            summary="Show this command help; pass a command for detailed docs.",
            syntax=["/help", "/help <command>"],
            details=(
                "With no argument, lists every slash command grouped by category. "
                "Pass a command name (with or without the leading slash) to see its "
                "full syntax, aliases, examples, and related commands."
            ),
            examples=["/help", "/help skill", "/help /mode"],
            related=["/mode", "/skill"],
        ),
    ),
    CommandSpec(
        names=["/prompt"],
        intent=Intent.PROMPT,
        help=HelpEntry(
            category="Session",
            summary="Compose a message in your editor, or send one loaded from a file.",
            syntax=["/prompt", "/prompt <path>"],
            details=(
                "With no argument, opens your $EDITOR to compose a multi-line "
                "message. With a path, loads the file's contents and sends it as "
                "your next message. Useful for long or pre-written prompts."
            ),
            examples=["/prompt", "/prompt ./notes/recon-brief.md"],
        ),
    ),
    CommandSpec(
        names=["/clear"],
        intent=Intent.CLEAR,
        help=HelpEntry(
            category="Session",
            summary="Clear conversation history and start a fresh session.",
            syntax=["/clear"],
            details=(
                "Resets the conversation history, token tracker, and session "
                "approval rules. The current mode, autonomy, provider, and model "
                "are kept."
            ),
        ),
    ),
    CommandSpec(
        names=["/q", "quit", "exit"],
        intent=Intent.QUIT,
        help=HelpEntry(
            category="Session",
            summary="Exit Kerkov.",
            syntax=["/q"],
            details="Exits the CLI and prints the command to resume this engagement later.",
            related=["/resume"],
        ),
    ),
    # --- Modes & Autonomy ---------------------------------------------------
    CommandSpec(
        names=["/mode"],
        intent=Intent.MODE,
        help=HelpEntry(
            category="Modes & Autonomy",
            summary="Show the current operational mode, or switch to another.",
            syntax=["/mode", "/mode [recon|audit|offensive|ctf]"],
            details=(
                "With no argument, shows the current mode and lists all modes. With "
                "a mode name, switches to it. Each mode carries its own system "
                "prompt and posture: recon (passive), audit (default), offensive "
                "(aggressive), and ctf (capture-the-flag)."
            ),
            examples=["/mode", "/mode offensive", "/mode ctf"],
            related=["/autonomy"],
        ),
    ),
    CommandSpec(
        names=["/autonomy"],
        intent=Intent.AUTONOMY,
        help=HelpEntry(
            category="Modes & Autonomy",
            summary="Show or set the autonomy preset (verbosity + approval together).",
            syntax=[
                "/autonomy",
                "/autonomy <1-4|guided|standard|aggressive|autonomous>",
            ],
            details=(
                "With no argument, shows the current preset. Setting a level adjusts "
                "both narration verbosity and approval gating at once. Levels: "
                "1 guided, 2 standard (default), 3 aggressive, 4 autonomous. Level 4 "
                "requires ALLOW_AUTONOMOUS=true."
            ),
            examples=["/autonomy", "/autonomy 3", "/autonomy aggressive"],
            related=["/verbosity", "/approval"],
        ),
    ),
    CommandSpec(
        names=["/verbosity"],
        intent=Intent.VERBOSITY,
        help=HelpEntry(
            category="Modes & Autonomy",
            summary="Change narration verbosity only (not approval gating).",
            syntax=["/verbosity", "/verbosity <guided|standard|aggressive|silent>"],
            details=(
                "Controls how much the assistant narrates its actions, independent "
                "of approval gating. To change both at once, use /autonomy."
            ),
            examples=["/verbosity", "/verbosity silent"],
            related=["/autonomy", "/approval"],
        ),
    ),
    CommandSpec(
        names=["/approval"],
        intent=Intent.APPROVAL,
        help=HelpEntry(
            category="Modes & Autonomy",
            summary="Change tool-approval gating only (not narration).",
            syntax=["/approval", "/approval <strict|normal|permissive|yolo>"],
            details=(
                "Controls when tool calls require your approval, independent of "
                "verbosity. 'yolo' bypasses all approvals. To change both at once, "
                "use /autonomy."
            ),
            examples=["/approval", "/approval permissive"],
            related=["/autonomy", "/yolo", "/verbosity"],
        ),
    ),
    CommandSpec(
        names=["/yolo"],
        intent=Intent.YOLO,
        help=HelpEntry(
            category="Modes & Autonomy",
            summary="Toggle the approval bypass (sugar for /approval yolo|normal).",
            syntax=["/yolo", "/yolo [on|off]"],
            details=(
                "With no argument, toggles YOLO mode. YOLO bypasses all "
                "tool-approval prompts for the session; turning it off restores "
                "normal gating."
            ),
            examples=["/yolo", "/yolo on", "/yolo off"],
            related=["/approval"],
        ),
    ),
    # --- Providers & Models -------------------------------------------------
    CommandSpec(
        names=["/provider", "/providers"],
        intent=Intent.PROVIDER,
        help=HelpEntry(
            category="Providers & Models",
            summary="Choose an LLM provider or switch directly by name.",
            syntax=["/provider", "/provider <name>"],
            details=(
                "With no name, opens an interactive provider picker and highlights "
                "the active provider. `/providers` is a compatibility alias. "
                "Providers: anthropic (alias claude), gemini (aliases google, "
                "google-gemini), ollama, codex (alias openai-codex). Selecting "
                "Codex starts browser sign-in when needed."
            ),
            examples=["/provider", "/provider claude", "/provider codex"],
            related=["/model", "/auth", "/logout"],
        ),
    ),
    CommandSpec(
        names=["/model", "/models"],
        intent=Intent.MODEL,
        help=HelpEntry(
            category="Providers & Models",
            summary="Choose a model or switch directly by name.",
            syntax=["/model", "/model <name>"],
            details=(
                "With no name, opens an interactive model picker and highlights "
                "the active model. `/models` is a compatibility alias. A direct "
                "model name switches without opening the picker."
            ),
            examples=[
                "/model",
                "/model gpt-5.6-sol",
                "/models gpt-5.6-terra",
            ],
            related=["/provider"],
        ),
    ),
    # --- Skills -------------------------------------------------------------
    CommandSpec(
        names=["/skill"],
        intent=Intent.SKILL,
        help=HelpEntry(
            category="Skills",
            summary="List skills, search them, or load one with optional input.",
            syntax=["/skill", "/skill search <query>", "/skill <name> [args]"],
            details=(
                "With no argument, lists available skills. 'search <query>' filters "
                "by keyword. '<name> [args]' loads a skill, optionally passing input "
                "to it."
            ),
            examples=[
                "/skill",
                "/skill search sqli",
                "/skill localhost-scan 10.0.0.1",
            ],
        ),
    ),
    # --- Engagements --------------------------------------------------------
    _resource_spec("asset", Intent.ASSET),
    _resource_spec("target", Intent.TARGET),
    # --- Agents -------------------------------------------------------------
    CommandSpec(
        names=["/agents"],
        intent=Intent.AGENTS,
        help=HelpEntry(
            category="Agents",
            summary="List this session's agents and switch which one you are watching.",
            syntax=[
                "/agents",
                "/agents <name|id>",
                "/agents stop <name|id>",
                "/agents review <name|id> [on|off]",
            ],
            details=(
                "With no argument, lists every agent spawned this session and opens "
                "a picker to switch focus. Pass a name or id (a1) to attach directly, "
                "or `master` to return to the main conversation. Attaching replays "
                "the agent's output so far and then follows it live; what you type "
                "while attached is queued for that agent and read at its next loop "
                "boundary. `/agents stop <name|id>` asks one agent to stop and "
                "report what it has so far. `/agents review <name|id>` puts that "
                "agent's tool calls to you instead of auto-approving them, for as "
                "long as you stay attached to it."
            ),
            examples=[
                "/agents",
                "/agents google-pinger",
                "/agents a1",
                "/agents stop a1",
                "/agents review a1 on",
            ],
            related=["/back"],
        ),
    ),
    CommandSpec(
        names=["/back"],
        intent=Intent.BACK,
        help=HelpEntry(
            category="Agents",
            summary="Return focus to the main conversation.",
            syntax=["/back"],
            details="Detaches from the agent you are watching. Esc in the picker does the same.",
            related=["/agents"],
        ),
    ),
    CommandSpec(
        names=["/engagements"],
        intent=Intent.ENGAGEMENTS,
        help=HelpEntry(
            category="Engagements",
            summary="List saved engagements, or the agent sub-runs behind them.",
            syntax=["/engagements", "/engagements agents"],
            details=(
                "Engagements persist as JSONL under ~/.kerkov/engagements/. "
                "Agent sub-runs are stored there too but hidden by default; "
                "`/engagements agents` lists them with the conversation each "
                "was spawned from. `/resume <id>` on one opens it read-only."
            ),
            examples=["/engagements", "/engagements agents"],
            related=["/resume", "/agents"],
        ),
    ),
    CommandSpec(
        names=["/resume"],
        intent=Intent.RESUME,
        help=HelpEntry(
            category="Engagements",
            summary="Resume a previous engagement.",
            syntax=["/resume", "/resume <id>"],
            details=(
                "With no argument, resumes the most recent engagement. Pass an id to "
                "resume a specific one (see /engagements for ids). An agent sub-run "
                "id opens that run's transcript read-only: an agent conversation "
                "belongs to its task and cannot be continued."
            ),
            examples=["/resume", "/resume abc123"],
            related=["/engagements", "/agents"],
        ),
    ),
    # --- Tools --------------------------------------------------------------
    CommandSpec(
        names=["/zap"],
        intent=Intent.ZAP,
        help=HelpEntry(
            category="Tools",
            summary="Inspect or stop Kerkov's automatically managed ZAP scanner.",
            syntax=["/zap", "/zap status", "/zap stop"],
            details=(
                "Kerkov automatically starts the official stable ZAP Docker image "
                "on the first spider or active scan, generates its API key, waits "
                "for readiness, and keeps the API on loopback. `/zap` is shorthand "
                "for status. `/zap stop` resets managed state so the next scan "
                "starts a fresh ZAP runtime automatically."
            ),
            examples=["/zap status", "/zap stop"],
            related=["/auth"],
        ),
    ),
    CommandSpec(
        names=["/searxng"],
        intent=Intent.SEARXNG,
        help=HelpEntry(
            category="Tools",
            summary="Install and run a local SearXNG instance for `web_search`.",
            syntax=[
                "/searxng",
                "/searxng setup",
                "/searxng start",
                "/searxng stop",
                "/searxng status",
            ],
            details=(
                "SearXNG is a self-hosted metasearch engine: no API key and no "
                "per-query cost, so it is the keyless alternative to Tavily. "
                "`/searxng setup` writes a settings file that enables the JSON "
                "results API — without it every request Kerkov makes is answered "
                "403 — then pulls the official image, publishes it on loopback "
                "and points SEARCH_PROVIDER/SEARCH_BASE_URL at it. Requires "
                "Docker. `/searxng` is shorthand for status."
            ),
            examples=["/searxng setup", "/searxng stop"],
            related=["/auth", "/zap"],
        ),
    ),
    # --- Auth ---------------------------------------------------------------
    CommandSpec(
        names=["/auth", "/credentials"],
        intent=Intent.AUTH,
        help=HelpEntry(
            category="Auth",
            summary="Show, enter, or remove API credentials securely.",
            syntax=[
                "/auth",
                "/auth set <provider|tool|PROFILE_ENV_VAR> [--session]",
                "/auth remove <provider|tool|PROFILE_ENV_VAR>",
            ],
            details=(
                "Secret values are collected in a separate masked prompt and never "
                "belong in the command itself. By default credentials are saved in "
                "the operating-system keyring, or a user-only local file when no "
                "keyring is available. Pass --session to keep a value only until "
                "Kerkov exits."
            ),
            examples=[
                "/auth",
                "/auth set gemini",
                "/auth tavily",
                "/auth set openrouter --session",
                "/auth set HTB_API_TOKEN",
                "/auth remove HTB_API_TOKEN",
                "/auth remove gemini",
            ],
            related=["/provider", "/logout"],
        ),
    ),
    CommandSpec(
        names=["/logout", "/signout"],
        intent=Intent.LOGOUT,
        help=HelpEntry(
            category="Auth",
            summary="Remove a local credential through a confirmed picker.",
            syntax=["/logout", "/logout <provider|tool|PROFILE_ENV_VAR>"],
            details=(
                "With no name, opens an interactive picker containing locally "
                "available provider, Tavily, and Codex credentials. After "
                "confirmation, removes the selected value from session memory and "
                "persistent storage. If its provider is active, Kerkov disconnects "
                "it. This removes the local credential but does not revoke the key "
                "at the provider."
            ),
            examples=["/logout", "/logout gemini", "/logout codex"],
            related=["/auth", "/provider"],
        ),
    ),
]


_SPEC_BY_NAME: Dict[str, CommandSpec] = {
    name: spec for spec in COMMAND_SPECS for name in spec.names
}


def spec_by_name(name: str) -> Optional[CommandSpec]:
    """Exact-name lookup (case-insensitive on the first token), as the parser uses."""
    return _SPEC_BY_NAME.get(name.strip().lower())


def find_spec(token: str) -> Optional[CommandSpec]:
    """Look up a spec by any of its names (slash optional, case-insensitive)."""
    bare = token.strip().lower().lstrip("/")
    candidates = {bare, "/" + bare}
    for spec in COMMAND_SPECS:
        if candidates & set(spec.names):
            return spec
    return None
