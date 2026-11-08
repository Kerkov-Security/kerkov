/* Upstream projects are prepared through the shared approved tool workflow. */
class ComplianceProjects {
  constructor(host) {
    this.host = host;
    this.inventory = host.inventory;
    this.catalog = JSON.parse(host.el("compliance-project-catalog").textContent);
    this.cache = new Map();
    this.requests = new Map();
    this.sequence = 0;
    this.context = null;
    this.selected = null;
    this.validated = null;
    this.ready = null;
    this.cacheError = false;
    this.preparation = null;
    host.el("compliance-project-validate").addEventListener("click", () => this.select(host.el("compliance-project-url").value.trim()));
    host.el("compliance-project-prepare").addEventListener("click", () => this.prepare(false));
    host.el("compliance-project-refresh").addEventListener("click", () => this.prepare(true));
    host.el("compliance-project-url").addEventListener("input", () => {
      this.deselect();
      if (host.baselineId === "remote") host.field("profile").value = "";
      host.renderSelection();
    });
    host.el("compliance-project-url").addEventListener("keydown", event => {
      if (event.key === "Enter") { event.preventDefault(); this.select(event.target.value.trim()); }
    });
    this.render();
  }

  status(text) { this.host.text("compliance-project-status", text); }
  reset() {
    for (const request of this.requests.values()) clearTimeout(request.timer);
    this.requests.clear();
    this.cache.clear();
    this.context = null;
    this.preparation = null;
    this.deselect();
    this.host.el("compliance-project-url").value = "";
    this.host.text("compliance-project-detail", "");
    this.status("Choose a community profile or validate a public GitHub URL.");
  }

  deselect() {
    this.sequence += 1;
    this.selected = null;
    this.validated = null;
    this.ready = null;
    this.cacheError = false;
    this.render();
  }

  sync() {
    if (!this.inventory.connected || !this.inventory.contextId || this.context === this.inventory.contextId) return;
    this.context = this.inventory.contextId;
    this.request("compliance.projects");
  }

  request(type, url = null, notice = null) {
    const id = this.inventory.requestId();
    const request = { type, url, notice, sequence: this.sequence, context: this.inventory.contextId };
    this.requests.set(id, request);
    if (!this.inventory.send({ type, request_id: id, context_id: request.context, ...(url ? { url } : {}) })) {
      this.requests.delete(id);
      this.status("Project request could not be sent. Reconnect and retry.");
    } else request.timer = setTimeout(() => {
      if (!this.requests.delete(id) || request.context !== this.inventory.contextId || request.sequence !== this.sequence) return;
      this.status("Project request timed out. Retry when connected.");
      this.render();
    }, 45000);
    this.render();
  }

  select(url) {
    if (!url || !this.inventory.connected || this.preparation) return;
    this.deselect();
    this.selected = url;
    this.host.el("compliance-project-url").value = url;
    this.host.baselineId = "remote";
    this.host.field("profile").value = "";
    this.host.renderBaselines();
    this.host.renderSelection();
    this.host.text("compliance-project-detail", "");
    const cached = this.cache.has(url);
    this.status(cached ? "Checking the downloaded copy…" : "Validating repository URL and profile metadata…");
    this.request(cached ? "compliance.project.cached" : "compliance.project.validate", url);
  }

  prepare(refresh) {
    if (!this.selected || this.preparation || !this.inventory.canWrite() || (!refresh && !this.validated)) return;
    const message = [
      `Prepare the operator-selected public InSpec project using compliance_profile with action="prepare", url=${JSON.stringify(this.selected)}, refresh=${JSON.stringify(refresh)}.`,
      "This request is project preparation only. Do not assess or remediate any target, call compliance_scan or ansible, or substitute another source. Use the shared compliance_profile tool rather than shell commands. Normal approvals apply to trusted executable profile code. Report preparation errors or the ready project identity when finished.",
    ].join("\n\n");
    const id = this.inventory.startWorkflow(message);
    if (!id) { this.status("Wait for current work to finish and check the provider connection."); return; }
    this.preparation = { id, url: this.selected, sequence: this.sequence, context: this.inventory.contextId, refresh };
    this.host.workflowId = id;
    this.host.el("compliance-workflow-output").textContent = "";
    this.status(refresh ? "Refreshing the project; the previous copy stays available until preparation succeeds…" : "Downloading and preparing the project. Review the tool approval to continue…");
    this.render();
    this.host.setState();
  }

  handle(type, payload) {
    if (type === "tool.result" && payload.name === "compliance_profile" && this.preparation) this.preparation.success = payload.success;
    if (["compliance.projects", "compliance.project.validate", "compliance.project.cached"].includes(type)) {
      const request = this.requests.get(payload.request_id);
      if (!request || request.type !== type || request.context !== this.inventory.contextId || payload.context_id !== request.context) return;
      this.requests.delete(payload.request_id);
      clearTimeout(request.timer);
      if (type === "compliance.projects") {
        if (payload.projects) this.cache = new Map(payload.projects.map(record => [record.url, record]));
        this.render();
        return;
      }
      if (request.sequence !== this.sequence || this.selected !== request.url || this.host.baselineId !== "remote") return;
      if (payload.detail) {
        this.ready = null;
        this.cacheError = type === "compliance.project.cached";
        this.host.field("profile").value = "";
        this.status(payload.detail);
      } else if (payload.validated) {
        this.validated = payload.validated;
        this.selected = payload.validated.url;
        this.host.el("compliance-project-url").value = this.selected;
        this.status("Valid public InSpec project. Choose Download & prepare before assessing a target.");
        this.host.text("compliance-project-detail", `${payload.validated.name} · version ${payload.validated.version || "unknown"}${payload.validated.has_dependencies ? " · dependencies will be prepared" : ""}`);
        if (this.cache.has(this.selected)) this.request("compliance.project.cached", this.selected);
      } else if (payload.project) {
        this.ready = payload.project;
        this.cache.set(payload.project.url, payload.project);
        this.host.field("profile").value = payload.project.profile;
        this.status(request.notice || "Ready. This downloaded copy is reused until you choose Refresh.");
        this.host.text("compliance-project-detail", `${payload.project.name} · version ${payload.project.version || "unknown"} · last prepared ${new Date(payload.project.prepared_at).toLocaleString()}`);
      } else {
        this.status("No ready copy is available. Validate the URL, then download and prepare it.");
      }
      this.host.renderSelection();
      this.render();
    }
    if (["user.message.complete", "user.message.rejected"].includes(type) && payload.message_id === this.preparation?.id) {
      const finished = this.preparation;
      this.preparation = null;
      if (finished.context === this.inventory.contextId && finished.sequence === this.sequence && this.selected === finished.url) {
        this.status(payload.stopped ? "Preparation stopped. Checking the previous ready copy…" : "Checking the prepared project copy…");
        const notice = payload.stopped ? "Preparation stopped. The previous ready copy is still available."
          : finished.refresh && finished.success !== true ? "Refresh did not succeed. The previous ready copy was kept."
          : null;
        this.request("compliance.project.cached", finished.url, notice);
      }
      this.render();
    }
  }

  render() {
    this.host.el("compliance-projects").replaceChildren(...this.catalog.map(item => {
      const button = this.host.node("button", "compliance-baseline", "");
      button.type = "button";
      button.dataset.complianceProject = item.id;
      button.setAttribute("aria-pressed", String(item.url === this.selected && this.host.baselineId === "remote"));
      const cached = this.cache.get(item.url);
      button.append(this.host.node("span", "compliance-baseline-meta", `${item.publisher} · ${item.platform}`), this.host.node("strong", "", item.name), this.host.node("span", "", item.description), this.host.node("span", "compliance-baseline-count", cached ? `Downloaded · version ${cached.version || "unknown"}` : "Not downloaded · assessment profile"));
      button.disabled = !this.inventory.connected || !!this.preparation;
      button.addEventListener("click", () => this.select(item.url));
      return button;
    }));
    const pending = Array.from(this.requests.values()).some(request => request.sequence === this.sequence && request.type !== "compliance.projects");
    this.host.el("compliance-project-url").disabled = !this.inventory.connected || !!this.preparation;
    this.host.el("compliance-project-validate").disabled = !this.inventory.connected || pending || !!this.preparation;
    this.host.el("compliance-project-prepare").disabled = !this.inventory.canWrite() || !this.validated || !!this.ready || pending || !!this.preparation;
    this.host.el("compliance-project-refresh").disabled = !this.inventory.canWrite() || (!this.ready && !this.cacheError) || pending || !!this.preparation;
  }
}
