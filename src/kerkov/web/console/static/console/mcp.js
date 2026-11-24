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
    } else if (type === "mcp.result" && payload.status) {
      this.show(payload.status);
    }
    this.render();
  }

  show(payload) {
    this.config.textContent = payload.config_path ? `Config: ${payload.config_path}` : "";
    this.config.hidden = !payload.config_path;
    this.list.replaceChildren(...this.serverRows(payload));
    this.status.textContent = this.summary(payload);
  }

  // `/mcp` and `/mcp list` draw the status they carry with the panel's rows.
  commandReply(payload) {
    const failed = payload.failed === true;
    if (payload.view !== "status" && payload.view !== "list") return { body: payload.detail || "", failed };
    const status = payload.status || {};
    const listing = payload.view === "list";
    const body = document.createElement("div");
    if (status.config_path) body.append(this.paragraph(`Config: ${status.config_path}`, "mcp-config-line"));
    const servers = this.servers(status);
    if (!servers.length) {
      body.append(this.paragraph(this.summary(status)));
      return { body, failed };
    }
    const list = document.createElement("ul");
    list.className = "mcp-servers";
    list.setAttribute("aria-label", "MCP servers");
    const only = listing && typeof payload.server === "string" ? payload.server : null;
    list.append(...this.serverRows(status, { only, open: listing }));
    body.append(list);
    return { meta: only === null ? this.summary(status) : "", body, failed };
  }

  servers(payload) {
    return Array.isArray(payload.servers) ? payload.servers : [];
  }

  serverRows(payload, { only = null, open = false } = {}) {
    const servers = this.servers(payload).filter((server) => only === null || server.key === only);
    const rows = servers.map((server) => this.serverItem(server, open));
    if (only !== null) return rows;
    for (const warning of Array.isArray(payload.warnings) ? payload.warnings : []) {
      rows.push(this.note(`mcp.json: ${warning}`, "mcp-warning"));
    }
    return rows;
  }

  summary(payload) {
    const servers = this.servers(payload);
    if (!payload.enabled) return "MCP is disabled. Set MCP_ENABLED=true and restart Kerkov.";
    if (payload.load_error) return payload.load_error;
    if (!servers.length) return "No MCP servers configured. Add one to mcp.json or with /mcp add in the CLI.";
    const connected = servers.filter((server) => server.state === "connected").length;
    const tools = servers.reduce((total, server) => total + (server.tools?.length || 0), 0);
    return `${connected} of ${servers.length} server${servers.length === 1 ? "" : "s"} connected · ${tools} tool${tools === 1 ? "" : "s"}`;
  }

  serverItem(server, open = false) {
    const item = document.createElement("li");
    const details = document.createElement("details");
    details.open = open;
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
    const trust = server.trusted ? " · trusted" : "";
    meta.textContent = `${server.transport} · auth: ${server.auth}${trust} · ${tools}`;
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
    if (server.resources) {
      const row = document.createElement("li");
      const mapped = document.createElement("code");
      mapped.textContent = server.resources;
      row.append(mapped, " ← resources");
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

  paragraph(text, className = "") {
    const element = document.createElement("p");
    if (className) element.className = className;
    element.textContent = text;
    return element;
  }

  note(text, className) {
    const element = document.createElement("li");
    element.className = className;
    element.textContent = text;
    return element;
  }
}
