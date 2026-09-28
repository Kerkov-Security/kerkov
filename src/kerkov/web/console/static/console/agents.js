/* Agent snapshots are presentation data; opening a report never consumes it. */
class AgentsPanel {
  constructor() {
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
        <button class="agent-open-report" type="button" hidden>Open report <span aria-hidden="true">↗</span></button>
      </div>`;
    const toggle = article.querySelector(".agent-toggle");
    const detail = article.querySelector(".agent-detail");
    detail.id = `agent-detail-${this.rows.size + 1}`;
    toggle.setAttribute("aria-controls", detail.id);
    const openReport = article.querySelector(".agent-open-report");
    toggle.addEventListener("click", () => {
      const previous = this.expandedId;
      this.expandedId = previous === id ? null : id;
      if (previous) {
        this.renderRow(previous);
      }
      this.renderRow(id);
    });
    openReport.addEventListener("click", () => {
      this.reportId = id;
      this.renderReport();
      this.closeButton.focus();
    });
    this.rows.set(id, { article, toggle, detail, openReport });
    this.list.append(article);
  }

  renderRow(id) {
    const agent = this.agents.get(id);
    const row = this.rows.get(id);
    const { article, toggle, detail, openReport } = row;
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
    if (!agent || !agent.report) {
      this.closeReport();
      return;
    }
    this.report.hidden = false;
    this.setText(this.reportName, agent.name);
    const status = {
      completed: "Completed",
      partial: "Partial report",
      stuck: "Unable to complete",
    }[agent.report.status] || "Report";
    this.setText(this.reportStatus, status);
    this.setText(this.reportFindings, agent.report.findings || "No findings reported.");
    this.reportNext.hidden = !agent.report.next_steps;
    this.setText(this.reportNextText, agent.report.next_steps || "");
  }

  closeReport() {
    const hadFocus = this.report.contains(document.activeElement);
    const row = this.rows.get(this.reportId);
    this.reportId = null;
    this.report.hidden = true;
    if (hadFocus && row) {
      (row.detail.hidden ? row.toggle : row.openReport).focus();
    }
  }

  setConnected(connected) {
    this.connected = connected;
    for (const id of this.agents.keys()) {
      this.renderRow(id);
    }
    this.renderSummary();
  }

  reset() {
    const hadFocus = this.list.contains(document.activeElement) || this.report.contains(document.activeElement);
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
