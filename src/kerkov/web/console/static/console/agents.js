/* Agent snapshots are presentation data; opening a report never consumes it. */
class AgentsPanel {
  constructor({ send, requestId }) {
    this.send = send;
    this.requestId = requestId;
    this.sessions = new Map();
    this.requests = new Map();
    this.inspectRequest = null;
    this.pollTimer = null;
    this.selectedRunId = null;
    this.view = "output";
    this.opener = null;
    this.agents = new Map();
    this.rows = new Map();
    this.expandedId = null;
    this.reportId = null;
    this.connected = false;
    this.list = document.querySelector("#agent-list");
    this.count = document.querySelector("#agent-count");
    this.status = document.querySelector("#agents-status");
    this.empty = document.querySelector("#agents-empty");
    this.report = document.querySelector("#agent-report");
    this.reportName = document.querySelector("#agent-report-name");
    this.reportStatus = document.querySelector("#agent-report-status");
    this.reportFindings = document.querySelector("#agent-report-findings");
    this.reportNext = document.querySelector("#agent-report-next");
    this.reportNextText = document.querySelector("#agent-report-next-text");
    this.closeButton = document.querySelector("#agent-report-close");
    this.output = document.querySelector("#agent-output-text");
    this.activity = document.querySelector("#agent-activity-text");
    this.outputStatus = document.querySelector("#agent-output-status");
    this.messageInput = document.querySelector("#agent-message-input");
    this.messageSend = document.querySelector("#agent-message-send");
    this.stopButton = document.querySelector("#agent-stop");
    this.controlStatus = document.querySelector("#agent-control-status");
    this.controlError = document.querySelector("#agent-control-error");
    document.querySelector("#agent-message-form").addEventListener("submit", (event) => {
      event.preventDefault();
      this.sendInstruction();
    });
    this.messageInput.addEventListener("input", () => {
      const state = this.selectedState();
      if (state) state.draft = this.messageInput.value;
      this.renderControls();
    });
    this.stopButton.addEventListener("click", () => this.stopAgent());
    for (const view of ["output", "report"]) {
      const tab = document.querySelector(`#agent-${view}-tab`);
      tab.addEventListener("click", () => this.showView(view));
      tab.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const next = event.key === "Home" ? "output" : event.key === "End" ? "report"
          : this.view === "output" ? "report" : "output";
        if (document.querySelector(`#agent-${next}-tab`).hidden) return;
        this.showView(next);
        document.querySelector(`#agent-${next}-tab`).focus();
      });
    }
    document.addEventListener("visibilitychange", () => this.visibilityChanged());
    this.closeButton.addEventListener("click", () => this.closeReport());
    this.report.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        this.closeReport();
      }
    });
  }

  isActive(agent) {
    return ["queued", "running", "blocked", "reporting"].includes(agent.state);
  }

  stateLabel(agent) {
    if (!this.connected && this.isActive(agent)) {
      return "Disconnected";
    }
    if (agent.cancel_requested && this.isActive(agent)) return "Stopping";
    return {
      queued: "Queued",
      running: "Running",
      blocked: "Awaiting input",
      reporting: "Reporting",
      done: "Done",
      failed: "Failed",
      cancelled: "Cancelled",
    }[agent.state] || "Unknown";
  }

  setText(element, value) {
    const text = String(value ?? "");
    if (element.textContent !== text) {
      element.textContent = text;
    }
  }

  update(agent) {
    if (!agent || typeof agent.id !== "string") {
      return;
    }
    const previous = this.agents.get(agent.id);
    if (previous && previous.agent_run_id !== agent.agent_run_id && this.reportId === agent.id) {
      this.closeReport();
    }
    this.agents.set(agent.id, agent);
    if (!this.rows.has(agent.id)) {
      this.createRow(agent.id);
    }
    this.renderRow(agent.id);
    this.renderSummary();
    if (this.reportId === agent.id) {
      this.renderReport();
    }
  }

  createRow(id) {
    const article = document.createElement("article");
    article.className = "agent-row";
    // Only static markup goes through innerHTML; all agent content uses textContent.
    article.innerHTML = `
      <button class="agent-toggle" type="button" aria-expanded="false">
        <span class="agent-symbol" aria-hidden="true">✳</span>
        <span class="agent-overview"><span class="agent-name"></span><span class="agent-current"></span></span>
        <span class="agent-state"></span><span class="agent-chevron" aria-hidden="true">⌄</span>
      </button>
      <div class="agent-detail" hidden>
        <p class="agent-task"></p>
        <dl><div><dt>Current tool</dt><dd class="agent-tool"></dd></div><div><dt>Step</dt><dd class="agent-step"></dd></div></dl>
        <button class="agent-open-output" type="button">Inspect output <span aria-hidden="true">↗</span></button>
        <button class="agent-open-report" type="button" hidden>Open report <span aria-hidden="true">↗</span></button>
      </div>`;
    const toggle = article.querySelector(".agent-toggle");
    const detail = article.querySelector(".agent-detail");
    detail.id = `agent-detail-${this.rows.size + 1}`;
    toggle.setAttribute("aria-controls", detail.id);
    const openOutput = article.querySelector(".agent-open-output");
    const openReport = article.querySelector(".agent-open-report");
    toggle.addEventListener("click", () => {
      const previous = this.expandedId;
      this.expandedId = previous === id ? null : id;
      if (previous) {
        this.renderRow(previous);
      }
      this.renderRow(id);
    });
    openOutput.addEventListener("click", () => this.openAgent(id, "output", openOutput));
    openReport.addEventListener("click", () => this.openAgent(id, "report", openReport));
    this.rows.set(id, { article, toggle, detail, openReport, openOutput });
    this.list.append(article);
  }

  renderRow(id) {
    const agent = this.agents.get(id);
    const row = this.rows.get(id);
    const { article, toggle, detail, openReport, openOutput } = row;
    article.dataset.state = agent.state;
    article.classList.toggle("agent-offline", !this.connected && this.isActive(agent));
    this.setText(article.querySelector(".agent-symbol"), agent.state === "done" ? "✓" : "✳");
    this.setText(article.querySelector(".agent-name"), agent.name);
    this.setText(article.querySelector(".agent-state"), this.stateLabel(agent));
    let activity = agent.report ? "Report ready" : "Waiting to start";
    if (this.isActive(agent)) {
      activity = agent.current_tool ? `Tool: ${agent.current_tool}` : this.stateLabel(agent);
      if (!this.connected) {
        activity = "Updates unavailable";
      }
    } else if (!agent.report) {
      activity = "No report available";
    }
    this.setText(article.querySelector(".agent-current"), activity);
    this.setText(article.querySelector(".agent-task"), agent.task);
    this.setText(article.querySelector(".agent-tool"), agent.current_tool || "—");
    const step = agent.max_iterations
      ? `${agent.iteration} of ${agent.max_iterations}`
      : String(agent.iteration);
    this.setText(article.querySelector(".agent-step"), step);
    const expanded = this.expandedId === id;
    toggle.setAttribute("aria-expanded", String(expanded));
    detail.hidden = !expanded;
    openOutput.hidden = !agent.agent_run_id;
    openOutput.setAttribute("aria-label", `Inspect ${agent.name}`);
    openReport.hidden = !agent.report;
    openReport.setAttribute("aria-label", `Open report from ${agent.name}`);
  }

  renderSummary() {
    this.setText(this.count, this.agents.size);
    this.empty.hidden = this.agents.size > 0;
    const active = Array.from(this.agents.values()).filter((agent) => this.isActive(agent)).length;
    const status = this.agents.size === 0 ? "" : !this.connected ? "Offline" : active ? `${active} active` : "All finished";
    this.setText(this.status, status);
  }

  renderReport() {
    const agent = this.agents.get(this.reportId);
    if (!agent) {
      this.closeReport();
      return;
    }
    this.report.hidden = false;
    this.setText(this.reportName, agent.name);
    const report = agent.report || {};
    const status = {
      completed: "Completed",
      partial: "Partial report",
      stuck: "Unable to complete",
    }[report.status] || "No report yet";
    this.setText(this.reportStatus, status);
    this.setText(this.reportFindings, report.findings || (agent.report ? "No findings reported." : "The agent’s report will appear here when available."));
    this.reportNext.hidden = !report.next_steps;
    this.setText(this.reportNextText, report.next_steps || "");
    this.setText(document.querySelector("#agent-detail-state"), this.stateLabel(agent));
    document.querySelector("#agent-output-tab").hidden = !agent.agent_run_id;
    this.renderOutput();
    this.renderControls();
  }

  closeReport() {
    const hadFocus = this.report.contains(document.activeElement);
    const row = this.rows.get(this.reportId);
    const opener = this.opener;
    clearTimeout(this.pollTimer);
    this.retireInspection();
    this.reportId = null;
    this.selectedRunId = null;
    this.opener = null;
    this.report.hidden = true;
    if (hadFocus && row) {
      (row.detail.hidden || !opener || opener.hidden ? row.toggle : opener).focus();
    }
  }

  setConnected(connected) {
    this.connected = connected;
    for (const id of this.agents.keys()) {
      this.renderRow(id);
    }
    this.renderSummary();
    if (!connected) {
      clearTimeout(this.pollTimer);
      for (const request of this.requests.values()) clearTimeout(request.timer);
      this.requests.clear();
      this.inspectRequest = null;
      for (const state of this.sessions.values()) {
        if (state.messageRequest || state.stopRequest) {
          state.error = "Connection lost before confirmation. The action may have been accepted; your draft is preserved.";
        }
        state.messageRequest = null;
        state.stopRequest = null;
      }
    }
    if (this.reportId) this.renderReport();
    this.visibilityChanged();
  }

  stateFor(agent) {
    const key = JSON.stringify([agent.id, agent.agent_run_id || null]);
    if (!this.sessions.has(key)) this.sessions.set(key, {
      output: "", activity: "", loaded: false, truncated: false, draft: "",
      messageRequest: null, stopRequest: null, stopped: false, status: "", error: "", inspectError: "",
    });
    return this.sessions.get(key);
  }

  selectedState() {
    const agent = this.agents.get(this.reportId);
    return agent && agent.agent_run_id === this.selectedRunId ? this.stateFor(agent) : null;
  }

  openAgent(id, view, opener) {
    const agent = this.agents.get(id);
    if (!agent) return;
    const changedTarget = this.reportId !== id || this.selectedRunId !== agent.agent_run_id;
    if (changedTarget) this.retireInspection();
    this.reportId = id;
    this.selectedRunId = agent.agent_run_id;
    this.opener = opener;
    this.messageInput.value = this.stateFor(agent).draft;
    this.showView(view);
    this.renderReport();
    if (changedTarget) document.querySelector(".agent-output-body").scrollTop = 0;
    this.closeButton.setAttribute("aria-label", view === "report" ? "Close agent report" : "Close agent output");
    this.closeButton.focus();
    this.visibilityChanged();
  }

  showView(view) {
    this.view = view;
    for (const name of ["output", "report"]) {
      const active = name === view;
      document.querySelector(`#agent-${name}-view`).hidden = !active;
      const tab = document.querySelector(`#agent-${name}-tab`);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
    }
    this.visibilityChanged();
  }

  canControl(agent) {
    const state = agent && this.stateFor(agent);
    return Boolean(this.connected && agent?.agent_run_id && this.isActive(agent)
      && !agent.cancel_requested && !state.stopRequest && !state.stopped);
  }

  renderControls() {
    const agent = this.agents.get(this.reportId);
    const state = this.selectedState();
    if (!agent || !state) return;
    const enabled = this.canControl(agent);
    this.messageInput.disabled = !enabled;
    this.messageInput.readOnly = Boolean(state.messageRequest);
    this.messageSend.disabled = !enabled || Boolean(state.messageRequest) || !state.draft.trim();
    this.stopButton.disabled = !enabled;
    this.setText(this.messageSend, state.messageRequest ? "Sending…" : "Send instructions");
    this.setText(this.stopButton, this.isActive(agent) && (state.stopRequest || agent.cancel_requested || state.stopped) ? "Stopping…" : "Stop agent");
    this.setText(this.controlStatus, !this.connected ? "Disconnected. Controls are unavailable."
      : !this.isActive(agent) ? "This agent has finished. Output remains available."
      : state.status);
    this.controlError.hidden = !state.error;
    this.setText(this.controlError, state.error);
  }

  renderOutput() {
    const state = this.selectedState();
    if (!state) return;
    const body = document.querySelector(".agent-output-body");
    const atEnd = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    const selection = window.getSelection();
    const selectingOutput = selection && !selection.isCollapsed && body.contains(selection.anchorNode);
    const changed = this.output.textContent !== (state.output || "No conversation output yet.")
      || this.activity.textContent !== (state.activity || "No activity yet.");
    this.setText(this.output, state.output || "No conversation output yet.");
    this.setText(this.activity, state.activity || "No activity yet.");
    document.querySelector("#agent-output-truncated").hidden = !state.truncated;
    this.setText(this.outputStatus, !this.connected ? "Disconnected · showing last received output"
      : state.inspectError || (state.loaded ? "Output updates while this panel is open." : "Loading output…"));
    if (changed && atEnd && !selectingOutput) body.scrollTop = body.scrollHeight;
  }

  panelVisible() {
    return this.reportId !== null && this.view === "output" && !this.report.hidden && !document.hidden
      && !document.querySelector("#conversation-view").hidden;
  }

  visibilityChanged() {
    clearTimeout(this.pollTimer);
    if (this.panelVisible() && this.connected) this.inspect();
    else this.retireInspection();
  }

  retireInspection() {
    if (!this.inspectRequest) return;
    const request = this.requests.get(this.inspectRequest);
    if (request) clearTimeout(request.timer);
    this.requests.delete(this.inspectRequest);
    this.inspectRequest = null;
  }

  scheduleInspect() {
    clearTimeout(this.pollTimer);
    if (this.panelVisible() && this.connected) {
      this.pollTimer = setTimeout(() => this.inspect(), 1000);
    }
  }

  request(operation, agent, extra = {}) {
    const request = { operation, agent_id: agent.id, agent_run_id: agent.agent_run_id,
      request_id: this.requestId(), ...extra };
    this.requests.set(request.request_id, request);
    const sent = this.send({ type: `agent.${operation}`, request_id: request.request_id,
      agent_id: request.agent_id, agent_run_id: request.agent_run_id, ...extra });
    if (!sent) {
      this.requests.delete(request.request_id);
      return null;
    }
    request.timer = setTimeout(() => this.handleEvent("agent.control_error", {
      ...request, detail: operation === "inspect" ? "Output could not be refreshed. Retrying…"
        : "No confirmation received. The action may have been accepted; inspect the agent before retrying.",
    }), 15000);
    return request.request_id;
  }

  inspect() {
    const agent = this.agents.get(this.reportId);
    if (!this.panelVisible() || !this.connected || !agent?.agent_run_id || this.inspectRequest) return;
    this.inspectRequest = this.request("inspect", agent);
    if (!this.inspectRequest) {
      this.stateFor(agent).inspectError = "Unable to request output.";
      this.renderOutput();
      this.scheduleInspect();
    }
  }

  sendInstruction() {
    const agent = this.agents.get(this.reportId);
    const state = this.selectedState();
    if (!state || !this.canControl(agent) || state.messageRequest || !state.draft.trim()) return;
    state.error = "";
    state.status = "";
    state.messageRequest = this.request("message", agent, { message: state.draft.trim() });
    if (!state.messageRequest) state.error = "Unable to send instructions. Your draft is preserved.";
    this.renderControls();
  }

  stopAgent() {
    const agent = this.agents.get(this.reportId);
    const state = this.selectedState();
    if (!state || !this.canControl(agent)) return;
    state.error = "";
    state.status = "Stopping this agent…";
    state.stopRequest = this.request("stop", agent);
    if (!state.stopRequest) {
      state.status = "";
      state.error = "Unable to stop the agent. Try again when connected.";
    }
    this.renderControls();
  }

  handleEvent(type, payload) {
    const request = this.requests.get(payload.request_id);
    if (!request || request.agent_id !== payload.agent_id || request.agent_run_id !== payload.agent_run_id) return;
    const expected = { inspect: "agent.output", message: "agent.message_sent", stop: "agent.stopped" }[request.operation];
    if (type !== expected && !(type === "agent.control_error" && payload.operation === request.operation)) return;
    clearTimeout(request.timer);
    this.requests.delete(request.request_id);
    if (this.inspectRequest === request.request_id) this.inspectRequest = null;
    const agent = this.agents.get(request.agent_id);
    if (!agent || agent.agent_run_id !== request.agent_run_id) {
      this.scheduleInspect();
      return;
    }
    const state = this.stateFor(agent);
    const failed = type === "agent.control_error";
    if (request.operation === "inspect") {
      state.inspectError = failed ? payload.detail || "Output is unavailable." : "";
      if (!failed) {
        state.output = String(payload.output ?? "");
        state.activity = String(payload.activity ?? "");
        state.loaded = true;
        state.truncated = Boolean(payload.truncated);
      }
      this.scheduleInspect();
    } else {
      if (request.operation === "message") {
        state.messageRequest = null;
        if (!failed) state.draft = "";
      } else {
        state.stopRequest = null;
        if (!failed) state.stopped = true;
      }
      state.error = failed ? payload.detail || "The action could not be completed." : "";
      state.status = failed ? "" : payload.detail || (request.operation === "message" ? "Instructions queued for the next step." : "Stop requested.");
    }
    if (this.reportId === request.agent_id && this.selectedRunId === request.agent_run_id) {
      if (request.operation === "message" && !failed) this.messageInput.value = state.draft;
      this.renderReport();
    }
  }

  reset() {
    const hadFocus = this.list.contains(document.activeElement) || this.report.contains(document.activeElement);
    clearTimeout(this.pollTimer);
    for (const request of this.requests.values()) clearTimeout(request.timer);
    this.requests.clear();
    this.sessions.clear();
    this.inspectRequest = null;
    this.selectedRunId = null;
    this.opener = null;
    this.output.textContent = "";
    this.activity.textContent = "";
    this.messageInput.value = "";
    this.controlStatus.textContent = "";
    this.controlError.textContent = "";
    this.reportId = null;
    this.expandedId = null;
    this.report.hidden = true;
    this.reportName.textContent = "";
    this.reportStatus.textContent = "";
    this.reportFindings.textContent = "";
    this.reportNextText.textContent = "";
    this.agents.clear();
    this.rows.clear();
    this.list.replaceChildren();
    this.renderSummary();
    if (hadFocus) {
      document.querySelector("#message-stream").focus();
    }
  }
}
