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
    host.el("compliance-project-refresh").addEventListener("click", () => this.refresh());
    host.el("compliance-project-update").addEventListener("click", () => this.prepare(true));
    host.el("compliance-project-cancel-update").addEventListener("click", () => {
      this.validated = null;
      this.showReady();
      this.render();
    });
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

  request(type, url = null, { notice = null, refresh = false } = {}) {
    const id = this.inventory.requestId();
    const request = { type, url, notice, refresh, sequence: this.sequence, context: this.inventory.contextId };
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

  refresh() {
    if (!this.selected || this.preparation || !this.inventory.canWrite()) return;
    this.validated = null;
    this.status("Checking the upstream revision and its dependencies…");
    this.request("compliance.project.validate", this.selected, { refresh: true });
  }

  prepare(refresh) {
    if (!this.selected || this.preparation || !this.inventory.canWrite() || !this.validated?.commit || refresh !== this.validated.refresh) return;
    const request = { url: this.selected, refresh, commit: this.validated.commit };
    const id = this.inventory.startWorkflow("Prepare profile project", null, request);
    if (!id) { this.status("Wait for current work to finish and check the console connection."); return; }
    this.preparation = { id, url: this.selected, sequence: this.sequence, context: this.inventory.contextId, refresh };
    this.host.workflowId = id;
    this.host.el("compliance-workflow-output").textContent = "";
    this.status(refresh ? "Refreshing the project; the previous copy stays available until preparation succeeds…" : "Downloading and preparing the project. Review the tool approval to continue…");
    this.render();
    this.host.setState();
  }

  revision(project) {
    return `v${project.version || "unknown"} · ${project.ref ? project.ref + " · " : ""}${project.commit.slice(0, 7)}`;
  }

  dependencyLines(entries, parent = "") {
    return entries.flatMap(entry => {
      const name = parent + entry.name;
      return [name + " · " + entry.git + " @ " + entry.ref, ...this.dependencyLines(entry.dependencies || [], name + " / ")];
    });
  }

  showReady(notice) {
    if (!this.ready) return;
    this.status(notice || "Ready. This downloaded copy is reused until you choose to update it.");
    this.host.text("compliance-project-detail", `${this.ready.name} · ${this.revision(this.ready)} · prepared ${new Date(this.ready.prepared_at).toLocaleString()}`);
  }

  handleWorkflowEvent(type, payload) {
    if (!this.preparation) return;
    if (type === "tool.result" && payload.name === "compliance_profile") {
      this.preparation.error = payload.success ? null : payload.error || "Preparation failed.";
    } else if (type === "error") {
      this.preparation.error = payload.detail || "Preparation failed.";
    }
  }

  handle(type, payload) {
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
        if (!request.refresh) {
          this.ready = null;
          this.cacheError = type === "compliance.project.cached";
          this.host.field("profile").value = "";
        }
        this.status(payload.detail);
      } else if (payload.validated) {
        this.validated = { ...payload.validated, refresh: request.refresh };
        this.selected = payload.validated.url;
        this.host.el("compliance-project-url").value = this.selected;
        const upstream = payload.validated;
        if (request.refresh && this.ready?.commit === upstream.commit) {
          this.validated = null;
          this.showReady(`Up to date with ${upstream.ref || "upstream"} (${upstream.commit.slice(0, 7)}). Nothing to download.`);
        } else {
          this.status(request.refresh
            ? "Update available. Review the revision and dependencies before updating."
            : "Review the revision and dependencies, then choose Download & prepare.");
          const installed = this.ready;
          const oldDependencies = installed?.dependency_sources
            ? this.dependencyLines(installed.dependency_sources)
            : (installed?.dependencies || []).map(entry => `${entry.name} @ ${entry.cache_key}`);
          const newDependencies = this.dependencyLines(upstream.dependencies || []);
          this.host.text("compliance-project-detail", [
            ...(request.refresh && installed ? [
              "Installed · " + this.revision(installed),
              ...oldDependencies,
              "",
            ] : []),
            (request.refresh ? "Upstream · " : upstream.name + " · ") + this.revision(upstream),
            "Commit " + upstream.commit,
            ...(newDependencies.length ? newDependencies : ["No dependencies"]),
            ...(request.refresh && installed ? ["Your current copy stays in use if the update fails."] : []),
          ].join("\n"));
        }
        if (!request.refresh && this.cache.has(this.selected)) this.request("compliance.project.cached", this.selected);
      } else if (payload.project) {
        this.ready = payload.project;
        this.cache.set(payload.project.url, payload.project);
        this.host.field("profile").value = payload.project.profile;
        this.showReady(request.notice ? `${request.notice} The ready copy shown below is available.` : null);
      } else {
        this.status(request.notice || "No ready copy is available. Validate the URL, then download and prepare it.");
      }
      this.host.renderSelection();
      this.render();
    }
    if (["user.message.complete", "user.message.rejected"].includes(type) && payload.message_id === this.preparation?.id) {
      const finished = this.preparation;
      this.preparation = null;
      if (finished.context === this.inventory.contextId && finished.sequence === this.sequence && this.selected === finished.url) {
        this.status(payload.stopped ? "Preparation stopped. Checking the previous ready copy…" : "Checking the prepared project copy…");
        const notice = payload.stopped ? "Preparation stopped."
          : type === "user.message.rejected" ? payload.detail
          : finished.error;
        if (finished.refresh) this.validated = null;
        this.request("compliance.project.cached", finished.url, { notice });
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
      button.append(this.host.node("span", "compliance-baseline-meta", `${item.publisher} · ${item.platform}`), this.host.node("strong", "", item.name), this.host.node("span", "", item.description), this.host.node("span", "compliance-baseline-count", cached ? `Downloaded · ${this.revision(cached)}` : "Not downloaded · assessment profile"));
      button.disabled = !this.inventory.connected || !!this.preparation;
      button.addEventListener("click", () => this.select(item.url));
      return button;
    }));
    const pending = Array.from(this.requests.values()).some(request => request.sequence === this.sequence && request.type !== "compliance.projects");
    this.host.el("compliance-project-url").disabled = !this.inventory.connected || !!this.preparation;
    this.host.el("compliance-project-validate").disabled = !this.inventory.connected || pending || !!this.preparation;
    this.host.el("compliance-project-prepare").disabled = !this.inventory.canWrite() || !this.validated?.commit || this.validated.refresh || !!this.ready || pending || !!this.preparation;
    const checking = this.host.el("compliance-project-refresh");
    const update = this.host.el("compliance-project-update");
    const cancel = this.host.el("compliance-project-cancel-update");
    const updateAvailable = !!this.validated?.refresh;
    checking.hidden = updateAvailable || (!this.ready && !this.cacheError);
    checking.disabled = !this.inventory.canWrite() || pending || !!this.preparation;
    update.hidden = cancel.hidden = !updateAvailable;
    update.disabled = cancel.disabled = checking.disabled;
    if (updateAvailable) update.textContent = `Update to ${this.validated.commit.slice(0, 7)}`;
  }
}
