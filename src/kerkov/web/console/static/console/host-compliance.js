/* Compliance actions use the shared conversation, execution and approval loop. */
class HostCompliance {
  constructor(inventory) {
    this.inventory = inventory;
    this.form = this.el("compliance-form");
    this.baselines = JSON.parse(this.el("compliance-baseline-catalog").textContent);
    this.selectedId = null;
    this.selectedIdentity = null;
    this.baselineId = this.baselines[0].id;
    this.run = null;
    this.workflowId = null;
    this.accessRequest = null;
    this.accessRefreshAgain = false;
    this.accessTimer = null;
    this.accessCandidates = [];
    this.accessDirty = false;
    this.accessSource = null;
    this.records = new Map();
    this.reviewedPreview = null;
    this.savedRun = null;
    this.assessmentOf = null;
    this.historyContext = null;
    this.controlRequest = null;
    this.exportRequest = null;
    this.exportTimer = null;
    this.controlTimer = null;
    this.el("compliance-control-search").addEventListener("input", () => this.renderControlRows());
    this.el("compliance-control-filter").addEventListener("change", () => this.renderControlRows());
    this.el("compliance-control-close").addEventListener("click", () => this.el("compliance-control-drawer").close());
    this.el("compliance-control-drawer").addEventListener("close", () => {
      if (this.el("compliance-control-drawer").open) return;
      this.controlRequest = null;
      clearTimeout(this.controlTimer);
      this.el("compliance-control-detail").replaceChildren();
    });
    for (const format of ["html", "json"]) this.el(`compliance-export-${format}`).addEventListener("click", () => this.exportReport(format));
    this.el("compliance-history-refresh").addEventListener("click", () => this.refreshHistory());
    this.el("compliance-reassess").addEventListener("click", () => this.restoreSettings(this.savedRun, true));
    this.el("compliance-review-apply").addEventListener("click", () => {
      this.restoreSettings(this.savedRun);
      this.reviewedPreview = this.savedRun;
      this.text("compliance-status", "Preview selected. Review its settings and outcomes, then choose Apply reviewed changes. Tool approvals still apply.");
    });
    this.form.addEventListener("submit", (event) => { event.preventDefault(); this.start("apply"); });
    this.form.addEventListener("input", (event) => {
      if (["user", "port", "private_key_file", "known_hosts_file", "sudo"].includes(event.target.name)) {
        this.accessDirty = true;
        this.accessSource = null;
        this.el("compliance-access-choice").hidden = !this.accessCandidates.length;
      }
      this.connectionSummary();
    });
    this.el("compliance-access-refresh").addEventListener("click", () => this.requestAccess(true));
    this.el("compliance-access-find").addEventListener("click", () => this.findAccess());
    this.el("compliance-access-select").addEventListener("change", (event) => {
      const candidate = this.accessCandidates.find(item => String(item.id) === event.target.value);
      if (candidate) { this.applyAccess(candidate); this.accessDirty = true; }
    });
    for (const button of this.form.querySelectorAll("[data-compliance-action]")) {
      button.addEventListener("click", () => this.start(button.dataset.complianceAction));
    }
    this.el("compliance-add-asset").addEventListener("click", () => {
      if (!this.inventory.canWrite()) return;
      this.inventory.showView("inventory");
      this.inventory.openForm();
      this.el("inventory-role").value = "assets";
      this.el("inventory-kind").value = "ip";
      this.inventory.renderValueHint();
    });
    this.el("compliance-welcome").addEventListener("click", () => this.inventory.showView("compliance"));
    this.el("compliance-open-conversation").addEventListener("click", () => this.inventory.showView("conversation"));
    this.projects = new ComplianceProjects(this);
    this.renderBaselines();
    this.renderSelection();
  }

  el(id) { return document.getElementById(id); }
  field(name) { return this.form.elements.namedItem(name); }
  host() {
    const host = this.inventory.entries.get(this.selectedId);
    return host && ["ip", "domain"].includes(host.entry.kind) ? host : null;
  }
  baseline() { return this.baselines.find(item => item.id === this.baselineId); }
  text(id, value) { this.el(id).textContent = value; }
  node(tag, className, text) {
    const node = document.createElement(tag);
    node.className = className;
    node.textContent = text;
    return node;
  }

  reset() {
    this.projects.reset();
    this.closeReviewRequests();
    this.el("compliance-control-search").value = "";
    this.el("compliance-control-filter").value = "all";
    this.records.clear();
    this.savedRun = null;
    this.reviewedPreview = null;
    this.historyContext = null;
    this.assessmentOf = null;
    this.el("compliance-run-review").hidden = true;
    this.el("compliance-history").replaceChildren();
    this.resetAccess();
    this.form.reset();
    this.selectedId = null;
    this.selectedIdentity = null;
    this.run = null;
    this.workflowId = null;
    this.el("compliance-connection").open = false;
    this.el("compliance-result").hidden = true;
    this.el("compliance-workflow-details").hidden = true;
    this.el("compliance-workflow-output").textContent = "";
    this.el("compliance-assets").replaceChildren();
    this.el("compliance-empty").hidden = false;
    this.renderSelection();
    this.text("compliance-status", "");
  }

  selectResource(id, navigate = true) {
    let entry = this.inventory.entries.get(id);
    if (!entry || !["ip", "domain"].includes(entry.entry.kind)) return;
    entry = this.hosts().find(row => row.entry.kind === entry.entry.kind && row.entry.value === entry.entry.value) || entry;
    const identity = JSON.stringify([this.inventory.contextId, entry.id, entry.entry.kind, entry.entry.value]);
    if (identity !== this.selectedIdentity) {
      this.assessmentOf = null;
      this.reviewedPreview = null;
      this.resetAccess();
      this.form.reset();
      this.el("compliance-connection").open = false;
      this.text("compliance-status", "");
    }
    this.selectedId = entry.id;
    this.selectedIdentity = identity;
    this.renderAssets();
    this.renderSelection();
    this.requestAccess();
    if (navigate) this.inventory.showView("compliance");
  }

  snapshot() {
    this.projects.sync();
    if (this.historyContext !== this.inventory.contextId) {
      this.historyContext = this.inventory.contextId;
      this.refreshHistory();
    }
    const hosts = this.hosts();
    if (hosts.length) this.selectResource(this.host() ? this.selectedId : hosts[0].id, false);
    else {
      this.resetAccess();
      this.selectedId = null;
      this.selectedIdentity = null;
      this.form.reset();
      this.renderAssets();
      this.renderSelection();
    }
  }

  hosts() {
    const rows = Array.from(this.inventory.entries.values())
      .filter(row => ["ip", "domain"].includes(row.entry.kind))
      .sort((a, b) => Number(b.section === "assets") - Number(a.section === "assets") || a.id - b.id);
    const seen = new Set();
    return rows.filter(row => {
      const identity = JSON.stringify([row.entry.kind, row.entry.value]);
      if (seen.has(identity)) return false;
      seen.add(identity);
      return true;
    });
  }

  renderAssets() {
    const hosts = this.hosts();
    this.el("compliance-empty").hidden = !!hosts.length;
    this.el("compliance-assets").replaceChildren(...hosts.map(row => {
      const button = this.node("button", "compliance-asset", "");
      button.type = "button";
      button.dataset.complianceAsset = row.id;
      button.setAttribute("aria-pressed", String(row.id === this.selectedId));
      button.append(this.node("span", "compliance-asset-icon", "▣"),
        this.node("strong", "", row.entry.name || row.entry.value),
        this.node("span", "compliance-asset-address", row.entry.value),
        this.node("span", "compliance-asset-role", row.section === "assets" ? "Asset" : "Target"));
      button.addEventListener("click", () => this.selectResource(row.id));
      return button;
    }));
    this.setState();
  }

  renderBaselines() {
    const items = [...this.baselines, { id: "custom", name: "Existing InSpec project", description: "Use a downloaded or local profile with its prepared dependencies. Remediation is optional.", platform: "Custom" }];
    this.el("compliance-baselines").replaceChildren(...items.map(item => {
      const button = this.node("button", "compliance-baseline", "");
      button.type = "button";
      button.dataset.complianceBaseline = item.id;
      button.setAttribute("aria-pressed", String(item.id === this.baselineId));
      button.append(this.node("span", "compliance-baseline-meta", item.recommended ? "RECOMMENDED START" : item.platform),
        this.node("strong", "", item.name), this.node("span", "", item.description),
        this.node("span", "compliance-baseline-count", item.controls ? `${item.controls.length} checks · fixes included` : "Local project · optional fixes"));
      button.addEventListener("click", () => {
        this.projects.deselect();
        this.baselineId = item.id;
        this.renderBaselines();
        this.renderSelection();
      });
      return button;
    }));
  }

  renderSelection() {
    const host = this.host();
    const baseline = this.baseline();
    const remote = this.baselineId === "remote";
    this.el("compliance-custom").hidden = !!baseline;
    this.el("compliance-local-profile").hidden = remote;
    this.text("compliance-project-settings-title", remote ? "Profile inputs (optional)" : "Existing project settings");
    this.text("compliance-project-help", remote ? "Kerkov downloads and prepares this project. Supply an inputs file when the upstream profile needs settings for your environment." : "Select a local InSpec project containing inspec.yml. Prepare its dependencies with inspec vendor before scanning. Assess asset does not require an Ansible playbook.");
    const bootstrap = this.field("bootstrap_python").closest("label");
    bootstrap.hidden = remote;
    bootstrap.nextElementSibling.hidden = remote;
    this.text("compliance-action-help", remote ? "Community profiles are assessments without automatic fixes. Normal tool approvals apply; review the profile's coverage and any manual checks." : "Applies the selected baseline, then checks it again. Missing Python 3 is installed only when you enable the option above. Tool calls use your current approval mode. Preview does not install Python; other Ansible tasks can still make changes in check mode.");
    for (const field of this.el("compliance-custom").querySelectorAll(".compliance-custom-fix")) field.hidden = remote;
    if (remote) this.field("profile").value = this.projects.ready?.profile || "";
    this.text("compliance-selection", host ? `${host.entry.name || host.entry.value} → ${baseline?.name || this.projects.ready?.name || (remote ? "Community profile" : "Existing InSpec project")}` : "Choose an asset to get started.");
    this.text("compliance-changes", baseline?.changes || (remote ? "Assess this downloaded community profile. Automatic fixes are not included." : "Assess an existing profile project. Configure an Ansible playbook only when requesting remediation."));
    this.text("compliance-coverage", baseline ? `${baseline.platform} · Built-in baseline v${baseline.version} · Covers these specific checks, not full CIS or PCI certification.` : "Your profile defines its coverage and supported operating systems.");
    this.connectionSummary();
    this.setState();
  }

  connectionSummary() {
    const ready = ["user", "private_key_file", "known_hosts_file"].every(name => this.field(name).value.trim());
    this.text("compliance-connection-summary", ready ? (this.accessSource ? "Access from engagement" : "Connection details entered") : "Set up access");
  }

  setState() {
    const writable = this.inventory.canWrite();
    for (const control of this.form.elements) control.disabled = !writable || !this.host();
    if (this.baselineId === "remote") {
      for (const control of [this.form.querySelector('[data-compliance-action="preview"]'), this.form.querySelector('button[type="submit"]'), this.field("bootstrap_python")]) control.disabled = true;
      this.form.querySelector('[data-compliance-action="assess"]').disabled ||= !this.projects.ready || !!this.projects.preparation;
    }
    this.projects.render();
    for (const button of this.el("compliance-assets").querySelectorAll("button")) button.disabled = !writable;
    for (const button of this.el("compliance-baselines").querySelectorAll("button")) button.disabled = !writable;
    this.el("compliance-add-asset").disabled = !writable;
    this.el("compliance-access-select").disabled = !writable || !this.host();
    this.el("compliance-access-find").disabled = !writable || !this.host();
    this.el("compliance-access-refresh").disabled = !writable || !this.host() || !!this.accessRequest;
    this.el("compliance-review-apply").disabled = !writable;
    this.el("compliance-reassess").disabled = !writable || !this.savedRun || this.savedRun.status === "running" || !this.savedRun.plan?.profile;
    this.el("compliance-history-refresh").disabled = !this.inventory.connected;
    for (const button of this.el("compliance-evidence-actions").querySelectorAll("button")) button.disabled = !this.inventory.connected;
    for (const button of this.el("compliance-controls").querySelectorAll("button")) button.disabled = !this.inventory.connected;
    for (const format of ["html", "json"]) this.el(`compliance-export-${format}`).disabled = !this.inventory.connected || !this.savedRun || this.savedRun.status === "running" || !!this.exportRequest;
  }

  resetAccess() {
    clearTimeout(this.accessTimer);
    this.accessRequest = null;
    this.accessRefreshAgain = false;
    this.accessCandidates = [];
    this.accessDirty = false;
    this.accessSource = null;
    this.el("compliance-access-choice").hidden = true;
    this.el("compliance-access-select").replaceChildren();
    this.text("compliance-access-status", "Recorded access is retrieved from this engagement.");
  }

  requestAccess(force = false) {
    const host = this.host();
    if (!host || !this.inventory.connected || !this.inventory.available || !this.inventory.contextId) return;
    if (this.accessRequest && !force) { this.accessRefreshAgain = true; return; }
    this.accessRefreshAgain = false;
    clearTimeout(this.accessTimer);
    const request = { id: this.inventory.requestId(), identity: this.selectedIdentity };
    this.accessRequest = request;
    if (!this.inventory.send({ type: "inventory.ssh_access", request_id: request.id,
      context_id: this.inventory.contextId, entry_id: host.id })) {
      this.accessRequest = null;
      this.text("compliance-access-status", "Recorded access could not be retrieved. Enter details or try Refresh.");
    } else this.accessTimer = window.setTimeout(() => {
      if (this.accessRequest !== request) return;
      this.accessRequest = null;
      this.text("compliance-access-status", "Access lookup did not respond. Enter details or try Refresh.");
      this.setState();
    }, 15000);
    this.setState();
  }

  applyAccess(candidate) {
    const fields = candidate.fields;
    for (const name of ["user", "port", "private_key_file", "known_hosts_file"]) this.field(name).value = fields[name] ?? "";
    this.field("sudo").checked = fields.sudo ?? true;
    this.accessSource = candidate.id;
    this.text("compliance-access-status", candidate.complete
      ? `Using recorded SSH access from ledger #${candidate.id}. Authentication is checked when the workflow runs.`
      : `Loaded partial access from ledger #${candidate.id}. Complete the missing connection details.`);
    this.connectionSummary();
  }

  receiveAccess(type, payload) {
    const request = this.accessRequest;
    if (!request || payload.request_id !== request.id || payload.context_id !== this.inventory.contextId
        || request.identity !== this.selectedIdentity) return;
    if (type !== "inventory.ssh_access" && type !== "inventory.error") return;
    this.accessRequest = null;
    clearTimeout(this.accessTimer);
    if (this.accessRefreshAgain) {
      this.accessRefreshAgain = false;
      queueMicrotask(() => { if (this.selectedIdentity === request.identity) this.requestAccess(); });
    }
    if (type === "inventory.error") {
      this.text("compliance-access-status", payload.detail || "Recorded access could not be retrieved.");
      this.setState();
      return;
    }
    const host = this.host();
    if (!host || payload.entry_id !== host.id || payload.resource?.kind !== host.entry.kind || payload.resource?.value !== host.entry.value) { this.setState(); return; }
    this.accessCandidates = payload.candidates || [];
    const select = this.el("compliance-access-select");
    select.replaceChildren(this.node("option", "", "Choose recorded access…"));
    select.firstChild.value = "";
    for (const candidate of this.accessCandidates) {
      const option = this.node("option", "", `${candidate.fields.user} · port ${candidate.fields.port ?? "not recorded"} · ledger #${candidate.id}`);
      option.value = String(candidate.id);
      select.append(option);
    }
    if (!this.accessDirty && ((!this.accessCandidates.length && this.accessSource) || this.accessCandidates.length > 1 || payload.truncated)) {
      for (const name of ["user", "port", "private_key_file", "known_hosts_file"]) this.field(name).value = "";
      this.accessSource = null;
      this.connectionSummary();
    }
    this.el("compliance-access-choice").hidden = !this.accessCandidates.length || (this.accessCandidates.length === 1 && !this.accessDirty && !payload.truncated);
    if (!this.accessDirty && !payload.truncated && this.accessCandidates.length === 1) {
      this.applyAccess(this.accessCandidates[0]);
      select.value = String(this.accessCandidates[0].id);
    } else this.text("compliance-access-status", this.accessCandidates.length
      ? (this.accessDirty ? "Recorded access is available. Your connection edits were kept." : "Multiple SSH connections are recorded. Choose the intended endpoint.")
      : "No recorded SSH file references found. Use Find known access to recover details from this engagement, or enter them here.");
    this.setState();
  }

  findAccess() {
    const host = this.host();
    if (!host || !this.inventory.canWrite()) return;
    const message = [
      `Find existing SSH access for the selected asset: kind=${JSON.stringify(host.entry.kind)}, host=${JSON.stringify(host.entry.value)}, ledger entry_id=${host.id}.`,
      "Read its ledger profile and credentials, the current conversation, and use recall for earlier container setup/tool output if needed. Recover only previously recorded connection facts: exact reachable SSH host, explicit SSH port, username, existing controller-local private_key_file and independently trusted known_hosts_file references, and sudo when known.",
      "Record confirmed access with ledger op='add', section='credentials', entry containing kind='ssh', host, username, port, private_key_file, known_hosts_file and sudo when known. Different published ports are different SSH endpoints; never guess from a Docker mapping or propagate keys across hosts. If multiple candidates exist, retain their distinct ports and explain the choice.",
      "This is access discovery only: do not start/stop containers, scan, authenticate, remediate, create keys, write credential files, or add host-key trust. Do not print private-key contents, passwords or tokens in your reply. If only a raw secret or password exists and the required file references are unavailable, explain what is missing. The GUI will retrieve the recorded file references; normal tool approvals apply.",
    ].join("\n\n");
    const messageId = this.inventory.startWorkflow(message);
    if (messageId) {
      this.workflowId = messageId;
      this.text("compliance-status", "Finding recorded access…");
      this.el("compliance-workflow-output").textContent = "";
    }
  }

  start(action) {
    const assessmentOf = this.assessmentOf;
    this.assessmentOf = null;
    const host = this.host();
    if (!host || !this.inventory.canWrite() || !["assess", "preview", "apply"].includes(action)) return;
    const baseline = this.baseline();
    if (this.baselineId === "remote" && (action !== "assess" || !this.projects.ready || this.projects.preparation)) {
      this.text("compliance-status", "Download and prepare this assessment profile first. Community profiles do not include automatic fixes.");
      return;
    }
    const required = ["user", "port", "private_key_file", "known_hosts_file"];
    const customFields = ["profile", "inputs_file", "automation_profile", "playbook", "host_alias", "vars_file"];
    if (!baseline) required.push("profile", ...(action === "assess" ? [] : ["automation_profile", "playbook", "host_alias"]));
    const values = {};
    for (const input of this.form.querySelectorAll("input")) {
      if (input.type === "checkbox") continue;
      input.required = required.includes(input.name);
      if (customFields.includes(input.name) && (baseline || (action === "assess" && !["profile", "inputs_file"].includes(input.name)))) continue;
      values[input.name] = input.value.trim();
      if (!input.checkValidity() || (input.required && !values[input.name]) || /[\x00-\x1f\x7f]/.test(values[input.name])) {
        this.el(input.closest("#compliance-custom") ? "compliance-custom" : "compliance-connection").open = true;
        this.text("compliance-status", "Complete the connection and selected profile settings to continue.");
        input.focus();
        input.reportValidity();
        return;
      }
    }
    if (!baseline && action !== "assess" && ["all", "ungrouped"].includes(values.host_alias)) {
      this.text("compliance-status", "Choose a single host alias, not an inventory group.");
      return;
    }
    const scan = {
      host: host.entry.value, user: values.user, port: Number(values.port),
      profile: baseline?.profile || values.profile,
      private_key_file: values.private_key_file, known_hosts_file: values.known_hosts_file, sudo: this.field("sudo").checked,
    };
    if (!baseline && values.inputs_file) scan.inputs_file = values.inputs_file;
    const remediation = baseline || action === "assess" ? null : {
      action: "run", profile: values.automation_profile, playbook: values.playbook,
      limit: values.host_alias, check: action === "preview", bootstrap_python: this.field("bootstrap_python").checked,
      vars_files: values.vars_file ? [values.vars_file] : [],
    };
    const message = `${action} compliance for ${host.entry.value} against ${baseline?.name || "the selected custom baseline"}.`;
    const workflowRequest = {
      action, resource_id: host.id, baseline_id: baseline?.id || null, scan,
      remediation: baseline || action === "assess" ? null : remediation,
      bootstrap_python: action !== "assess" && this.field("bootstrap_python").checked,
      preview_id: action === "apply" ? this.reviewedPreview?.id || null : null,
      assessment_of: action === "assess" ? assessmentOf : null,
    };
    if (action === "apply" && !workflowRequest.preview_id) {
      this.text("compliance-status", "Preview changes first, then review and select the saved preview before applying.");
      return;
    }
    const messageId = this.inventory.startWorkflow(message, workflowRequest);
    if (!messageId) { this.text("compliance-status", "Unable to start. Check the connection and wait for current work to finish."); return; }
    this.savedRun = null;
    this.assessmentOf = null;
    this.el("compliance-run-review").hidden = true;
    this.workflowId = messageId;
    this.run = { messageId, host: host.entry.value, action, label: baseline?.name || "Custom baseline" };
    this.result("Workflow started", `${this.run.host} · ${this.run.label}. Progress, approvals and evidence appear here.`);
    this.el("compliance-workflow-output").textContent = "";
    this.text("compliance-status", "Starting workflow…");
  }

  result(title, detail) {
    this.el("compliance-result").hidden = false;
    this.text("compliance-result-title", title);
    this.text("compliance-result-detail", detail);
  }

  handleWorkflowEvent(type, payload) {
    this.projects.handleWorkflowEvent(type, payload);
    this.handleEvent(type, payload);
    this.el("compliance-workflow-details").hidden = false;
    if (type === "tool.start") {
      const stages = { compliance_scan: this.savedRun?.stage === "verification" ? "Verifying baseline…" : "Assessing baseline…",
        ansible: this.run?.action === "preview" ? "Previewing changes…" : "Applying baseline…",
        write: "Preparing configuration…", ledger: "Updating engagement context…", recall: "Retrieving known context…" };
      this.text("compliance-status", stages[payload.name] || "Working…");
    }
    if (type === "tool.result") {
      const output = this.el("compliance-workflow-output");
      output.textContent += `\n${payload.success ? "Finished" : "Failed"}: ${payload.name}\n${payload.error || payload.output || ""}\n`;
      output.textContent = output.textContent.slice(-32000);
    }
    if (["assistant.message", "error", "provider.error", "log.warning"].includes(type)) {
      const output = this.el("compliance-workflow-output");
      output.textContent = (output.textContent + "\n" + (payload.content || payload.detail || payload.message || "")).slice(-32000);
    }
  }

  handleEvent(type, payload) {
    const projectPreparation = !!this.projects.preparation && payload.message_id === this.projects.preparation.id;
    this.projects.handle(type, payload);
    if (type === "compliance.control" || type === "compliance.export") {
      if (payload.context_id !== this.inventory.contextId || payload.run_id !== this.savedRun?.id) return;
      if (type === "compliance.control" && payload.request_id === this.controlRequest) {
        this.controlRequest = null;
        clearTimeout(this.controlTimer);
        if (payload.control) this.showControlDetail(payload.control);
        else this.text("compliance-control-status", payload.detail || "Control evidence is unavailable.");
      }
      if (type === "compliance.export" && payload.request_id === this.exportRequest) {
        this.exportRequest = null;
        clearTimeout(this.exportTimer);
        this.text("compliance-export-status", payload.detail || "Report downloaded. Assertion messages may contain sensitive target configuration.");
        if (typeof payload.body === "string" && ["text/html", "application/json"].includes(payload.mime)) {
          const url = URL.createObjectURL(new Blob([payload.body], { type: `${payload.mime};charset=utf-8` }));
          const link = document.createElement("a");
          link.href = url;
          link.download = payload.filename;
          link.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        }
        this.setState();
      }
      return;
    }
    if (type === "compliance.snapshot" || type === "compliance.updated") {
      if (payload.context_id !== this.inventory.contextId) return;
      const records = type === "compliance.snapshot" ? payload.runs : [payload.run];
      if (type === "compliance.snapshot") this.records.clear();
      for (const record of records || []) this.records.set(record.id, record);
      this.renderHistory();
      if (type === "compliance.updated") this.showSavedRun(payload.run);
      else if (this.savedRun && this.records.has(this.savedRun.id)) this.showSavedRun(this.records.get(this.savedRun.id));
      else if (!this.savedRun && records?.length) this.showSavedRun(records[0]);
      return;
    }
    if (type === "compliance.evidence") {
      if (payload.context_id !== this.inventory.contextId || payload.run_id !== this.savedRun?.id) return;
      this.el("compliance-evidence-preview").hidden = false;
      this.text("compliance-evidence-preview", payload.body || payload.detail || "No evidence available.");
      return;
    }
    this.receiveAccess(type, payload);
    if (type === "approval.request" && this.workflowId) this.text("compliance-status", "Approval required to continue.");
    if (payload.message_id === this.workflowId && ["user.message.complete", "user.message.rejected"].includes(type)) {
      this.text("compliance-status", payload.stopped ? "Workflow stopped." : type === "user.message.rejected" ? payload.detail : projectPreparation ? "Project preparation finished. Review its ready status above." : "Workflow finished. Results are available to the conversation.");
      this.workflowId = null;
    }
    if (!this.run) return;
    const run = this.run;
    if (["user.message.complete", "user.message.rejected"].includes(type) && payload.message_id === run.messageId) {
      if (!this.savedRun || this.savedRun.status === "running") this.result(
        payload.stopped ? "Workflow stopped" : type === "user.message.rejected" ? "Workflow could not start" : "Refreshing saved assessment",
        payload.detail || "No verified assessment result received yet. Refresh assessment history to review the server-recorded outcome.",
      );
      this.run = null;
      this.workflowId = null;
      this.refreshHistory();
    }
  }

  refreshHistory() {
    if (!this.inventory.connected || !this.inventory.contextId) return;
    this.inventory.send({ type: "compliance.list", context_id: this.inventory.contextId });
  }

  renderHistory() {
    const records = Array.from(this.records.values()).sort((a, b) => b.created_at.localeCompare(a.created_at));
    this.el("compliance-history").replaceChildren(...records.map(record => {
      const button = this.node("button", "compliance-history-item", "");
      button.type = "button";
      button.append(this.node("strong", "", `${record.request.scan.host} · ${record.label}`),
        this.node("span", "", `${record.status} · ${new Date(record.updated_at).toLocaleString()}`));
      button.addEventListener("click", () => this.showSavedRun(record));
      return button;
    }));
    if (!records.length) this.el("compliance-history").append(this.node("p", "compliance-caption", "No saved assessments in this engagement."));
  }

  showSavedRun(record) {
    if (this.savedRun?.id !== record.id) {
      this.closeReviewRequests();
      this.el("compliance-control-search").value = "";
      this.el("compliance-control-filter").value = "all";
    }
    this.savedRun = record;
    this.el("compliance-run-review").hidden = false;
    this.el("compliance-evidence-preview").hidden = true;
    const titles = { running: "Workflow in progress", previewed: "Preview ready for review", verified: "Baseline verified", passed: "Baseline checks passed", attention: "Baseline needs attention", interrupted: "Workflow interrupted", failed: "Workflow needs review", blocked: "Workflow blocked", incomplete: "Workflow incomplete" };
    this.result(titles[record.status] || record.status, `${record.request.scan.host} · ${record.label}. ${record.detail}`);
    this.text("compliance-run-time", new Date(record.updated_at).toLocaleString());
    const stages = record.request.action === "assess" ? ["readiness", "assessment"] : record.request.action === "preview" ? ["readiness", "assessment", "preview"] : ["readiness", "assessment", "remediation", "verification"];
    this.el("compliance-stages").replaceChildren(...stages.map(stage => {
      const node = this.node("li", "", stage);
      if (stage === record.stage) node.setAttribute("aria-current", "step");
      return node;
    }));
    this.el("compliance-readiness").replaceChildren(...(record.readiness || []).map(check => this.node("li", `compliance-check-${check.status}`, `${check.name}: ${check.detail}`)));
    const project = record.plan?.profile_bundle;
    this.text("compliance-plan-summary", [record.plan?.changes || "Preparing plan…", project ? `${project.name} · version ${project.version || "unknown"} · ${project.dependencies.length} bundled ${project.dependencies.length === 1 ? "dependency" : "dependencies"}` : ""].filter(Boolean).join(" · "));
    const evidence = {};
    for (const phase of ["before", "after", "remediation"]) {
      if (record[phase]) evidence[phase] = Object.fromEntries(Object.entries(record[phase]).filter(([key]) => key.endsWith("artifact") || key === "artifact_directory" || key === "profile_sha256"));
    }
    this.text("compliance-plan-evidence", JSON.stringify({ plan: record.plan, evidence }, null, 2));
    this.renderControlRows();
    const report = record.after || record.before;
    const counts = report?.counts;
    this.text("compliance-controls-notice", counts ? `${Object.entries(counts).map(([status, count]) => `${count} ${status.replaceAll("_", " ")}`).join(" · ")}. ${report.controls_truncated ? "Showing a bounded control list; open normalized evidence for complete results. " : ""}An unassessed after column means verification has not been recorded.` : "No verified control evidence received yet.");
    const actions = [];
    for (const phase of ["before", "after", "remediation"]) {
      if (!record[phase]) continue;
      const button = this.node("button", "inventory-button", `${phase === "remediation" ? "Remediation log" : `${phase} evidence`}`);
      button.type = "button";
      button.addEventListener("click", () => this.inventory.send({ type: "compliance.evidence", context_id: this.inventory.contextId, run_id: record.id, phase }));
      actions.push(button);
    }
    this.el("compliance-evidence-actions").replaceChildren(...actions);
    this.el("compliance-review-apply").hidden = record.status !== "previewed";
    this.el("compliance-review-apply").disabled = !this.inventory.canWrite();
    this.el("compliance-reassess").disabled = !this.inventory.canWrite() || record.status === "running" || !record.plan?.profile;
    this.setState();
  }

  renderControlRows() {
    const search = this.el("compliance-control-search").value.trim().toLowerCase();
    const filter = this.el("compliance-control-filter").value;
    const controls = this.savedRun?.controls || [];
    const visible = controls.filter(control => {
      const latest = control.after && control.after !== "unassessed" ? control.after : control.before || "unassessed";
      return (filter === "all" || (filter === "unresolved" ? latest !== "passed" : latest === filter)) &&
        [control.control_id, control.title, control.profile].join(" ").toLowerCase().includes(search);
    });
    this.el("compliance-controls").replaceChildren(...visible.map(control => {
      const row = this.node("tr", "", "");
      const title = this.node("td", "", "");
      const button = this.node("button", "inventory-link compliance-control-link", control.control_id);
      button.type = "button";
      button.disabled = !this.inventory.connected;
      button.setAttribute("aria-haspopup", "dialog");
      button.addEventListener("click", () => this.openControl(control));
      title.append(button, this.node("span", "", control.title || control.profile));
      row.append(title, this.node("td", "", control.impact == null ? "Unknown" : String(control.impact)));
      for (const phase of ["before", "after"]) row.append(this.node("td", `compliance-outcome-${control[phase]}`, (control[phase] || "unassessed").replaceAll("_", " ")));
      return row;
    }));
    this.text("compliance-filter-notice", visible.length ? `${visible.length} of ${controls.length} loaded controls shown. Select a control to review its evidence.` : "No loaded controls match these filters.");
  }

  closeReviewRequests() {
    this.controlRequest = null;
    this.exportRequest = null;
    clearTimeout(this.controlTimer);
    clearTimeout(this.exportTimer);
    this.el("compliance-control-drawer").close();
    this.el("compliance-control-detail").replaceChildren();
    this.text("compliance-export-status", "");
  }

  openControl(control) {
    if (!this.inventory.connected || !this.savedRun) return;
    clearTimeout(this.controlTimer);
    const requestId = crypto.randomUUID();
    this.controlRequest = requestId;
    this.text("compliance-control-title", control.title || control.control_id);
    this.text("compliance-control-profile", `${control.profile} · ${control.control_id}`);
    this.text("compliance-control-status", "Loading saved control evidence…");
    this.el("compliance-control-detail").replaceChildren();
    this.el("compliance-control-drawer").showModal();
    this.inventory.send({ type: "compliance.control", context_id: this.inventory.contextId, run_id: this.savedRun.id, request_id: requestId, profile: control.profile, control_id: control.control_id });
    this.controlTimer = setTimeout(() => {
      if (this.controlRequest !== requestId) return;
      this.controlRequest = null;
      this.text("compliance-control-status", "Evidence did not respond. Close and reopen this control to retry.");
    }, 30000);
  }

  showControlDetail(control) {
    const container = this.el("compliance-control-detail");
    this.text("compliance-control-status", control.notice);
    const mapping = this.node("section", "compliance-control-phase", "");
    mapping.append(this.node("h4", "", "Remediation mapping"), this.node("p", "", control.remediation));
    const sections = [mapping];
    for (const phase of ["before", "after"]) {
      const data = control[phase];
      const section = this.node("section", "compliance-control-phase", "");
      section.append(this.node("h4", "", `${phase === "before" ? "Before" : "After"}: ${data.status.replaceAll("_", " ")}`));
      if (data.description) section.append(this.node("p", "", data.description));
      if (data.detail) section.append(this.node("p", "", data.detail));
      for (const test of data.tests || []) {
        const assertion = this.node("div", "compliance-assertion", "");
        assertion.append(this.node("strong", "", test.status || "Unknown test status"));
        for (const [key, label] of [["code_desc", "Check"], ["message", "Observed result"], ["skip_message", "Skip reason"], ["exception", "Error"]]) {
          if (test[key]) assertion.append(this.node("h5", "", label), this.node("pre", "", test[key]));
        }
        section.append(assertion);
      }
      if (!(data.tests || []).length) section.append(this.node("p", "", data.status === "unassessed" ? "No assessment recorded for this control in this phase." : "No assertion details recorded."));
      if (data.tests_truncated) section.append(this.node("p", "compliance-caption", `Showing ${data.tests.length} of ${data.test_count} assertions. Full results remain in the source evidence.`));
      if (data.report_artifact) section.append(this.node("p", "compliance-caption", `Source: ${data.report_artifact}\nSHA-256: ${data.report_sha256}`));
      sections.push(section);
    }
    container.replaceChildren(...sections);
  }

  exportReport(format) {
    if (!this.inventory.connected || !this.savedRun || this.savedRun.status === "running" || this.exportRequest) return;
    const requestId = crypto.randomUUID();
    this.exportRequest = requestId;
    this.text("compliance-export-status", "Preparing report from complete saved evidence…");
    this.setState();
    this.inventory.send({ type: "compliance.export", context_id: this.inventory.contextId, run_id: this.savedRun.id, request_id: requestId, format });
    this.exportTimer = setTimeout(() => {
      if (this.exportRequest !== requestId) return;
      this.exportRequest = null;
      this.text("compliance-export-status", "Report did not respond. Retry the download.");
      this.setState();
    }, 30000);
  }

  restoreSettings(record, assess = false) {
    if (!record || !this.inventory.canWrite()) return;
    const request = record.request;
    if (!this.inventory.entries.has(request.resource_id)) {
      this.text("compliance-status", "The saved asset is unavailable. Refresh inventory before reassessing.");
      return;
    }
    this.projects.deselect();
    this.selectResource(request.resource_id);
    this.baselineId = request.baseline_id || "custom";
    this.renderBaselines();
    this.renderSelection();
    for (const [key, value] of Object.entries(request.scan)) {
      const field = this.field(key);
      if (!field) continue;
      if (field.type === "checkbox") field.checked = value;
      else field.value = value || "";
    }
    if (!assess && !request.baseline_id && record.plan?.profile && record.plan.profile_bundle?.source) {
      this.field("profile").value = record.plan.profile;
    }
    this.field("bootstrap_python").checked = request.bootstrap_python ?? false;
    if (request.remediation) {
      for (const [field, key] of [["automation_profile", "profile"], ["playbook", "playbook"], ["host_alias", "limit"]]) this.field(field).value = request.remediation[key] || "";
      this.field("vars_file").value = request.remediation.vars_files?.[0] || "";
    }
    this.accessDirty = true;
    this.connectionSummary();
    if (assess) {
      this.assessmentOf = record.id;
      this.start("assess");
    }
  }

  disconnected() {
    for (const request of this.projects.requests.values()) clearTimeout(request.timer);
    this.projects.requests.clear();
    this.projects.preparation = null;
    this.projects.render();
    this.closeReviewRequests();
    if (!this.workflowId) return;
    this.text("compliance-status", "Connection lost. Resume the engagement to review the saved evidence.");
    if (this.run) this.result("Workflow connection lost", "The final outcome is unknown. Review saved evidence before retrying changes.");
    this.run = null;
    this.workflowId = null;
  }

}
