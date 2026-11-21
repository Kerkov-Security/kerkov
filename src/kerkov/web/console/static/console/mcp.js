/* Read-only MCP server status. Keys and OAuth logins are entered in the CLI. */
class McpPanel {
  constructor({ send, requestId }) {
    this.send = send;
    this.requestId = requestId;
    this.pending = null;
    this.connected = false;
    this.status = document.getElementById("mcp-status");
    this.config = document.getElementById("mcp-config");
    this.list = document.getElementById("mcp-servers");
    this.refreshButton = document.getElementById("mcp-refresh");
    this.refreshButton.addEventListener("click", () => this.refresh());
  }

  setConnected(connected) {
    const wasConnected = this.connected;
    this.connected = connected;
    if (wasConnected && !connected) {
      this.pending = null;
      clearTimeout(this.timer);
      this.status.textContent = "Offline. The servers below may have changed since.";
    }
    this.render();
  }

  render() {
    this.refreshButton.disabled = !this.connected || !!this.pending;
  }

  refresh() {
    if (!this.connected || this.pending) return;
    const request_id = this.requestId();
    if (!this.send({ type: "mcp.status", request_id })) {
      this.status.textContent = "Unable to request MCP status. Try again when connected.";
      return;
    }
    this.pending = request_id;
    this.status.textContent = "Loading MCP servers…";
    this.timer = setTimeout(() => {
      if (this.pending !== request_id) return;
      this.pending = null;
      this.status.textContent = "MCP status timed out. Refresh to try again.";
      this.render();
    }, 30000);
    this.render();
  }

  handleEvent(type, payload) {
    if (payload.request_id && payload.request_id === this.pending) {
      this.pending = null;
      clearTimeout(this.timer);
    }
    if (type === "mcp.error") {
      this.status.textContent = payload.detail || "MCP status is unavailable.";
    } else if (type === "mcp.status") {
      this.show(payload);
    }
    this.render();
  }

  show(payload) {
    this.config.textContent = payload.config_path ? `Config: ${payload.config_path}` : "";
    this.config.hidden = !payload.config_path;
    const servers = Array.isArray(payload.servers) ? payload.servers : [];
    this.list.replaceChildren(...servers.map((server) => this.serverItem(server)));
    for (const warning of Array.isArray(payload.warnings) ? payload.warnings : []) {
      this.list.append(this.note(`mcp.json: ${warning}`, "mcp-warning"));
    }
    if (!payload.enabled) {
      this.status.textContent = "MCP is disabled. Set MCP_ENABLED=true and restart Kerkov.";
    } else if (payload.load_error) {
      this.status.textContent = payload.load_error;
    } else if (!servers.length) {
      this.status.textContent = "No MCP servers configured. Add one to mcp.json or with /mcp add in the CLI.";
    } else {
      const connected = servers.filter((server) => server.state === "connected").length;
      const tools = servers.reduce((total, server) => total + (server.tools?.length || 0), 0);
      this.status.textContent = `${connected} of ${servers.length} server${servers.length === 1 ? "" : "s"} connected · ${tools} tool${tools === 1 ? "" : "s"}`;
    }
  }

  serverItem(server) {
    const item = document.createElement("li");
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    const name = document.createElement("strong");
    name.textContent = server.key;
    const state = document.createElement("span");
    state.className = `mcp-state ${this.stateClass(server.state)}`;
    state.textContent = server.state;
    const meta = document.createElement("span");
    meta.className = "mcp-meta";
    const tools = server.advertised === null || server.advertised === undefined
      ? "no tools listed"
      : `${server.exposed} of ${server.advertised} tools`;
    meta.textContent = `${server.transport} · auth: ${server.auth} · ${tools}`;
    summary.append(name, state, meta);
    details.append(summary);

    const body = document.createElement("ul");
    body.className = "mcp-tools";
    for (const tool of server.tools || []) {
      const row = document.createElement("li");
      const mapped = document.createElement("code");
      mapped.textContent = tool.name;
      row.append(mapped, ` ← ${tool.original}`);
      body.append(row);
    }
    for (const skipped of server.skipped || []) {
      body.append(this.note(`Skipped ${skipped.name}: ${skipped.reason}`, "mcp-warning"));
    }
    if (!body.children.length) body.append(this.note("No tools exposed.", "mcp-empty"));
    details.append(body);
    item.append(details);
    return item;
  }

  stateClass(state) {
    if (state === "connected") return "is-ok";
    if (typeof state === "string" && (state.startsWith("failed") || state.startsWith("invalid"))) return "is-error";
    return "is-idle";
  }

  note(text, className) {
    const element = document.createElement("li");
    element.className = className;
    element.textContent = text;
    return element;
  }
}
