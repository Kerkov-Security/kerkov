/* Compliance actions use the shared conversation, execution and approval loop. */
const COMPLIANCE_SSH_FIELDS = ["user", "port", "private_key_file", "known_hosts_file"];
class HostCompliance {
  constructor(inventory) {
    this.inventory = inventory;
    this.form = this.el("compliance-form");
    this.baselines = JSON.parse(this.el("compliance-baseline-catalog").textContent);
    this.selectedId = null;
    this.selectedIdentity = null;
    this.chosenHostIdentity = null;
    this.assessmentCard = null;
    this.recommendation = null;
    this.baselineId = this.baselines[0].id;
    this.run = null;
    this.workflowId = null;
    this.workflowError = null;
    this.readiness = { blockers: [], mode_notice: null };
    this.accessRequest = null;
    this.accessRefreshAgain = false;
    this.accessTimer = null;
    this.accessCandidates = [];
    this.accessTruncated = false;
    this.pendingAssessment = null;
    this.accessDirty = false;
    this.accessSource = null;
    this.records = new Map();
    this.previewCards = new Map();
    this.savedRun = null;
    this.defaultFilterRunId = null;
    this.autoShowHistory = true;
    this.assessmentOf = null;
    this.scapRequest = null;
    this.ansibleAvailable = null;
    this.ansibleCheckError = "";
    this.historyContext = null;
    this.controlRequest = null;
    this.exportRequest = null;
    this.exportTimer = null;
    this.controlTimer = null;
    this.controlsRequest = null;
    this.controlsKey = null;
    this.controlsRetryDeadline = 0;
    this.controlsResult = null;
    this.controlsPage = 1;
    this.el("compliance-customize").addEventListener("click", () => {
      this.openSettings(this.field("user"));
      this.el("compliance-customize-heading").focus();
    });
    this.el("compliance-customize-done").addEventListener("click", () => {
      this.el("compliance-advanced").open = false;
      this.el("compliance-customize").focus();
    });
    this.el("compliance-advanced").addEventListener("toggle", () => {
      this.el("compliance-customize").setAttribute("aria-expanded", String(this.el("compliance-advanced").open));
    });
    this.el("compliance-profile-search").addEventListener("input", () => this.filterProfiles());
    this.el("compliance-profile-source").addEventListener("change", () => this.filterProfiles());
    this.el("compliance-profile-clear").addEventListener("click", () => {
      this.el("compliance-profile-search").value = "";
      this.el("compliance-profile-source").value = "all";
      this.filterProfiles();
      this.el("compliance-profile-search").focus();
    });
    this.el("compliance-complete-settings").addEventListener("click", () => {
      const missing = this.assessmentFields().find(name => !this.fieldComplete(name));
      if (!missing) return;
      const field = missing === "scap_sha256" ? this.el("compliance-scap-inspect") : this.field(missing);
      this.openSettings(field);
      field.focus();
    });
    this.el("compliance-controls-clear").addEventListener("click", () => {
      this.el("compliance-control-search").value = "";
      this.el("compliance-control-filter").value = "all";
      this.el("compliance-control-impact").value = "all";
      this.renderControlRows();
      this.el("compliance-control-search").focus();
    });
    this.el("compliance-setup").addEventListener("toggle", () => {
      const setup = this.el("compliance-setup");
      if (setup.open && this.savedRun && !this.workflowId && setup.dataset.record !== this.savedRun.id) {
        setup.dataset.record = this.savedRun.id;
        this.restoreSettings(this.savedRun);
      }
      this.updateSetupSummary(setup.open ? null : this.savedRun);
      this.setState();
    });
    this.el("compliance-control-search").addEventListener("input", () => this.renderControlRows());
    this.el("compliance-control-filter").addEventListener("change", () => this.renderControlRows());
    for (const id of ["compliance-control-impact", "compliance-control-sort", "compliance-page-size"]) {
      this.el(id).addEventListener("change", () => this.renderControlRows());
    }
    this.el("compliance-page-previous").addEventListener("click", () => this.renderControlRows(this.controlsPage - 1));
    this.el("compliance-page-next").addEventListener("click", () => this.renderControlRows(this.controlsPage + 1));
    this.el("compliance-controls-retry").addEventListener("click", () => {
      this.controlsKey = null;
      this.renderControlRows(this.controlsPage);
    });
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
    this.el("compliance-result-preview").addEventListener("click", () => this.reviewFixes(this.savedRun));
    this.el("compliance-review-apply").addEventListener("click", () => this.reviewPreview(this.savedRun));
    this.form.addEventListener("submit", event => event.preventDefault());
    this.el("compliance-scap-inspect").addEventListener("click", () => this.inspectDatastream());
    this.el("compliance-scap-recheck").addEventListener("click", () => {
      if (!this.inventory.canWrite() || !this.ansibleBlocked() || this.scapRequest) return;
      this.ansibleCheckError = "";
      this.requestScap("compliance.ansible.check");
    });
    this.field("scap_datastream").addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      this.inspectDatastream();
    });
    this.field("scap_profile_id").addEventListener("change", () => this.updateScapProfileId());
    this.el("compliance-project-url").addEventListener("keydown", event => {
      if (event.key === "Enter") event.preventDefault();
    });
    this.form.addEventListener("input", (event) => {
      if (event.target.name === "scap_datastream") {
        this.resetScap();
        this.setState();
        return;
      }
      if (COMPLIANCE_SSH_FIELDS.includes(event.target.name) || event.target.name === "sudo") {
        this.stopAssessmentSetup("");
        this.accessDirty = true;
        this.accessSource = null;
        this.el("compliance-access-choice").hidden = !this.accessCandidates.length;
        this.setState();
      }
      this.connectionSummary();
    });
    this.el("compliance-access-refresh").addEventListener("click", () => this.requestAccess(true));
    this.el("compliance-access-find").addEventListener("click", () => this.findAccess());
    this.el("compliance-access-select").addEventListener("change", (event) => {
      const candidate = this.accessCandidates.find(item => String(item.id) === event.target.value);
      if (candidate) {
        this.finishAssessmentCard("Endpoint selected. Choose Scan host to continue.");
        this.selectEndpoint(candidate);
      }
    });
    for (const button of this.form.querySelectorAll("[data-compliance-action]")) {
      button.addEventListener("click", () => {
        if (button.dataset.complianceAction === "assess") this.scanHost();
        else this.start("preview");
      });
    }
    this.el("compliance-host").addEventListener("change", (event) => {
      this.selectResource(Number(event.target.value));
    });
    this.el("compliance-add-asset").addEventListener("click", () => {
      if (!this.inventory.canWrite()) return;
      this.inventory.showView("inventory");
      this.inventory.openForm(null, {
        section: "assets",
        kind: "ip",
        onSaved: id => {
          if (this.selectResource(id)) this.el("compliance-host").focus();
        },
      });
    });
    this.el("compliance-welcome").addEventListener("click", () => this.inventory.showView("compliance"));
    this.el("compliance-open-conversation").addEventListener("click", () => this.inventory.showView("conversation"));
    this.builder = new ComplianceProfileBuilder(this);
    this.fixes = new ComplianceFixLibrary(this);
    this.el("compliance-auto").addEventListener("click", () => this.selectLocalChoice("auto"));
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
  text(id, value) {
    this.el(id).textContent = value;
    const card = this.run?.card || this.assessmentCard;
    if (id === "compliance-status" && value && card) card.status.textContent = value;
  }
  node(tag, className, text) {
    const node = document.createElement(tag);
    node.className = className;
    node.textContent = text;
    return node;
  }

  reset() {
    if (this.run && this.run.action !== "assess") this.finishWorkflowCard(this.run.card, "Engagement changed.");
    this.finishAssessmentCard("Assessment setup ended when the engagement changed.");
    this.previewCards.clear();
    this.chosenHostIdentity = null;
    this.el("compliance-profile-search").value = "";
    this.el("compliance-profile-source").value = "all";
    this.projects.reset();
    this.builder.reset();
    this.fixes.reset();
    this.recommendation = null;
    this.el("compliance-auto-goal").value = "hardening";
    this.el("compliance-control-impact").value = "all";
    this.el("compliance-control-sort").value = "priority";
    this.closeReviewRequests();
    this.el("compliance-control-search").value = "";
    this.el("compliance-control-filter").value = "all";
    this.el("compliance-setup").open = true;
    this.el("compliance-advanced").open = false;
    delete this.el("compliance-setup").dataset.record;
    this.el("compliance-run-details").open = false;
    this.el("compliance-run-details").hidden = true;
    this.el("compliance-history-details").hidden = true;
    this.el("compliance-history-details").open = false;
    this.records.clear();
    this.savedRun = null;
    this.autoShowHistory = true;
    this.defaultFilterRunId = null;
    this.historyContext = null;
    this.assessmentOf = null;
    this.el("compliance-run-review").hidden = true;
    this.el("compliance-history").replaceChildren();
    this.resetAccess();
    this.form.reset();
    this.ansibleAvailable = null;
    this.resetScap();
    this.selectedId = null;
    this.selectedIdentity = null;
    this.run = null;
    this.workflowId = null;
    this.el("compliance-connection").open = false;
    this.el("compliance-result").hidden = true;
    this.el("compliance-workflow-details").hidden = true;
    this.el("compliance-workflow-output").textContent = "";
    this.renderHosts();
    this.renderSelection();
    this.text("compliance-status", "");
  }

  selectResource(id, navigate = true, chosen = navigate) {
    let entry = this.inventory.entries.get(id);
    if (!entry || !["ip", "domain"].includes(entry.entry.kind) || (navigate && !this.inventory.canWrite())) return;
    entry = this.hosts().find(row => row.entry.kind === entry.entry.kind && row.entry.value === entry.entry.value) || entry;
    const identity = JSON.stringify([this.inventory.contextId, entry.id, entry.entry.kind, entry.entry.value]);
    const switching = identity !== this.selectedIdentity;
    if (chosen) this.chosenHostIdentity = identity;
    if (navigate && this.assessmentCard && !this.workflowId && !this.pendingAssessment) {
      this.finishAssessmentCard("Host selected. Choose Scan host to continue.");
    }
    if (navigate && this.savedRun) {
      this.el("compliance-setup").dataset.record = this.savedRun.id;
    }
    if (switching) {
      if (this.pendingAssessment) this.stopAssessmentSetup("Host changed. Choose Scan host to assess the new host.");
      this.assessmentOf = null;
      this.resetAccess();
      this.resetHostSettings(chosen);
      this.el("compliance-connection").open = false;
      this.text("compliance-status", "");
      if (navigate || (this.savedRun && this.savedRun.request.scan.host !== entry.entry.value && !this.run)) {
        this.openHostSetup();
      }
    }
    this.selectedId = entry.id;
    this.selectedIdentity = identity;
    this.renderHosts();
    this.renderSelection();
    this.requestAccess();
    if (navigate) this.inventory.showView("compliance");
    return true;
  }

  resetHostSettings(keepBenchmark) {
    const profile = this.field("profile").value;
    const path = this.field("scap_datastream").value;
    const sha256 = this.field("scap_sha256").value;
    const profileId = this.field("scap_profile_id").value;
    const profiles = Array.from(this.field("scap_profile_id").options)
      .filter(option => option.value)
      .map(option => ({ id: option.value, title: option.text }));
    this.form.reset();
    this.resetScap();
    if (!keepBenchmark) return;
    if (this.baselineId === "custom") this.field("profile").value = profile;
    if (this.isOpenScap()) {
      this.field("scap_datastream").value = path;
      this.field("scap_sha256").value = sha256;
      this.setScapProfiles(profiles, profileId);
    }
  }

  openHostSetup() {
    this.autoShowHistory = false;
    this.savedRun = null;
    this.closeReviewRequests();
    this.el("compliance-setup").open = true;
    this.el("compliance-advanced").open = false;
    this.el("compliance-result").hidden = true;
    this.el("compliance-run-review").hidden = true;
    this.el("compliance-run-details").hidden = true;
    this.el("compliance-run-details").open = false;
    this.el("compliance-workflow-details").hidden = true;
    this.el("compliance-workflow-output").textContent = "";
  }

  recordMatchesHost(record) {
    return !!record && record.request.scan.host === this.host()?.entry.value;
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
      if (this.selectedId != null && !this.run) this.openHostSetup();
      this.resetAccess();
      this.selectedId = null;
      this.selectedIdentity = null;
      this.form.reset();
      this.resetScap();
      this.renderHosts();
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

  renderHosts() {
    const picker = this.el("compliance-host");
    const hosts = this.hosts();
    picker.replaceChildren(...hosts.map(row => {
      const { name, value } = row.entry;
      return new Option(name && name !== value ? name + " (" + value + ")" : value, row.id);
    }));
    if (!hosts.length) picker.add(new Option("No hosts recorded", ""));
    picker.value = this.selectedId ?? "";
    this.setState();
  }

  renderBaselines() {
    const items = [...this.baselines,
      { id: "custom", name: "Existing InSpec project", description: "Use a downloaded or local profile with its prepared dependencies. Remediation is optional.", platform: "Custom" },
      { id: "openscap", name: "SCAP datastream", description: "Use a local SCAP benchmark and its XCCDF profile.", platform: "OpenSCAP" },
    ];
    const buttons = items.map(item => {
      const button = this.node("button", "compliance-baseline", "");
      button.type = "button";
      button.dataset.complianceBaseline = item.id;
      button.dataset.profileSource = ["custom", "openscap"].includes(item.id) ? "local" : "builtin";
      button.setAttribute("aria-pressed", String(item.id === this.baselineId));
      button.append(this.node("span", "compliance-baseline-meta", item.recommended ? "BUILT-IN · RECOMMENDED" : `${button.dataset.profileSource === "local" ? "LOCAL" : "BUILT-IN"} · ${item.platform}`),
        this.node("strong", "", item.name), this.node("span", "compliance-baseline-description", item.description),
        this.node("span", "compliance-baseline-count", item.controls ? `${item.controls.length} checks · fixes included` : item.id === "openscap" ? "Assessment only" : "Local project · optional fixes"));
      button.addEventListener("click", () => {
        this.projects.deselect();
        this.baselineId = item.id;
        if (button.dataset.profileSource === "local") this.el("compliance-advanced").open = true;
        this.renderBaselines();
        this.renderSelection();
      });
      return button;
    });
    this.el("compliance-baselines").replaceChildren(...buttons.filter(button => button.dataset.profileSource === "builtin"));
    this.el("compliance-local-profiles").replaceChildren(...buttons.filter(button => button.dataset.profileSource === "local"));
    this.filterProfiles();
  }

  filterProfiles() {
    const terms = this.el("compliance-profile-search").value.toLowerCase().match(/\S+/g) || [];
    const source = this.el("compliance-profile-source").value;
    const cards = [...this.el("compliance-baselines").children, ...this.el("compliance-custom-profiles").children,
      ...this.el("compliance-projects").children, ...this.el("compliance-local-profiles").children];
    let visible = 0;
    for (const card of cards) {
      const text = Array.from(card.children, child => child.textContent).join(" ").toLowerCase();
      card.hidden = (source !== "all" && card.dataset.profileSource !== source) || !terms.every(term => text.includes(term));
      if (!card.hidden) visible += 1;
    }
    for (const [list, heading] of [["compliance-projects", "compliance-community-heading"], ["compliance-local-profiles", "compliance-local-heading"]]) {
      this.el(heading).hidden = !Array.from(this.el(list).children).some(card => !card.hidden);
    }
    const customVisible = Array.from(this.el("compliance-custom-profiles").children).some(card => !card.hidden);
    this.el("compliance-custom-heading").hidden = !customVisible;
    this.el("compliance-custom-profiles").hidden = !customVisible;
    this.el("compliance-profile-empty").hidden = visible > 0;
    this.el("compliance-profile-count").hidden = !terms.length && source === "all";
    this.text("compliance-profile-count", `${visible} of ${cards.length} profiles`);
  }

  assessmentFields() {
    if (this.scapPreset() || this.customProfile() || this.baselineId === "auto") return [...COMPLIANCE_SSH_FIELDS];
    return [...COMPLIANCE_SSH_FIELDS,
      ...(this.isOpenScap() ? ["scap_datastream", "scap_sha256", "scap_profile_id", "scap_distribution", "scap_version"] : !this.baseline() ? ["profile"] : [])];
  }

  fieldComplete(name) {
    const field = this.field(name);
    return !!field.value.trim() && field.validity.valid && !/[\x00-\x1f\x7f]/.test(field.value);
  }

  openSettings(field) {
    for (let parent = field.parentElement; parent; parent = parent.parentElement) {
      if (parent.tagName === "DETAILS") parent.open = true;
    }
  }

  updateSettingsStatus() {
    const missing = this.assessmentFields().filter(name => !this.fieldComplete(name));
    const remotePending = this.baselineId === "remote" && !this.projects?.ready;
    const blocked = this.ansibleBlocked();
    const ready = !!this.host() && !remotePending && !missing.length && !blocked;
    this.el("compliance-settings-status").dataset.ready = String(ready);
    const missingAccess = !this.hasSshAccess();
    this.text("compliance-settings-status", !this.host() ? "Scan host will ask which host to assess."
      : this.workflowId && !this.run && !this.projects?.preparation ? "Finding recorded access in this engagement…"
      : blocked ? (this.scapRequest?.type === "compliance.ansible.check" ? "Checking Ansible on the controller…"
        : this.ansibleCheckError || "Ansible is missing on the controller. Run kerkov setup ansible, then recheck.")
      : missingAccess && this.mustChooseEndpoint() ? "Choose an SSH endpoint next to Host."
      : missingAccess && !this.accessDirty ? "Scan host looks up SSH access recorded in this engagement."
      : remotePending && this.projects.selected && !missingAccess ? "The selected community profile will be prepared on its first scan."
      : missing.length ? "Review the missing details in Customize."
      : this.baselineId === "auto" ? "Scan host checks the Linux release and recommends a profile."
      : "Ready to assess. Access is checked when the workflow runs.");
    const button = this.el("compliance-complete-settings");
    button.hidden = !this.host() || remotePending || !missing.length;
    button.disabled = !this.inventory.canWrite();
  }

  customProfile() { return this.builder?.entries.get(this.baselineId); }

  selectLocalChoice(id) {
    if (!this.inventory.canWrite()) return;
    this.stopAssessmentSetup("");
    this.projects.deselect();
    this.baselineId = id;
    this.renderBaselines();
    this.renderSelection();
  }

  selectCustomProfile(id) {
    if (this.builder.entries.has(id)) this.selectLocalChoice(id);
  }

  scapPreset() {
    return this.projects?.catalog.find(item => item.openscap && item.id === this.baselineId);
  }

  selectScapPreset(id) { this.selectLocalChoice(id); }

  isOpenScap() {
    return this.baselineId === "openscap" || !!this.scapPreset();
  }

  ansibleBlocked() {
    return this.isOpenScap() && this.ansibleAvailable === false;
  }

  setScapProfiles(profiles, selected = "") {
    const select = this.field("scap_profile_id");
    select.replaceChildren(new Option(profiles.length ? "Select a profile" : "Inspect a datastream first", ""));
    for (const { id, title } of profiles) select.add(new Option(title || id, id));
    select.value = profiles.some(profile => profile.id === selected) ? selected : "";
    this.updateScapProfileId();
  }

  updateScapProfileId() {
    const id = this.field("scap_profile_id").value;
    const label = this.el("compliance-scap-profile-id");
    label.textContent = id;
    label.hidden = !id;
  }

  resetScap() {
    clearTimeout(this.scapRequest?.timer);
    this.scapRequest = null;
    this.ansibleCheckError = "";
    this.field("scap_sha256").value = "";
    this.setScapProfiles([]);
    this.text("compliance-scap-status", "");
  }

  inspectDatastream() {
    if (!this.inventory.canWrite() || !this.host() || this.baselineId !== "openscap" || this.scapRequest ||
        !this.fieldComplete("scap_datastream")) return;
    const path = this.field("scap_datastream").value.trim();
    const selected = this.field("scap_profile_id").value;
    this.resetScap();
    this.requestScap("compliance.datastream.inspect", { path }, selected);
  }

  requestScap(type, fields = {}, selected = "") {
    const id = this.inventory.requestId();
    const rechecking = type === "compliance.ansible.check";
    const failed = (detail) => {
      if (rechecking) this.ansibleCheckError = detail;
      else this.text("compliance-scap-status", detail);
    };
    if (!this.inventory.send({ type, context_id: this.inventory.contextId, request_id: id, ...fields })) {
      failed("Unable to reach the controller. Check the connection and retry.");
    } else {
      if (!rechecking) this.text("compliance-scap-status", "Inspecting datastream on the controller…");
      this.scapRequest = { id, type, path: fields.path, selected, timer: window.setTimeout(() => {
        this.scapRequest = null;
        failed("The controller did not respond. Try again.");
        this.setState();
      }, 15000) };
    }
    this.setState();
  }

  receiveScapResult(type, payload) {
    const request = this.scapRequest;
    if (payload.context_id !== this.inventory.contextId || request?.type !== type || payload.request_id !== request.id) return;
    if (type === "compliance.ansible.check") {
      clearTimeout(request.timer);
      this.scapRequest = null;
      this.ansibleCheckError = payload.detail || "";
      if (!payload.detail) {
        this.ansibleAvailable = payload.ansible_available;
        if (this.ansibleAvailable) {
          this.text("compliance-scap-status", "Ansible is available on the controller.");
          this.text("compliance-status", "");
        }
      }
      this.setState();
      return;
    }
    if (request.path !== this.field("scap_datastream").value.trim()) return;
    this.resetScap();
    if (payload.detail) {
      this.text("compliance-scap-status", payload.detail);
    } else {
      const { profiles, sha256, ansible_available: ansibleAvailable } = payload;
      this.ansibleAvailable = ansibleAvailable;
      this.field("scap_sha256").value = sha256;
      this.setScapProfiles(profiles, request.selected || (profiles.length === 1 ? profiles[0].id : ""));
      this.text("compliance-scap-status", ansibleAvailable
        ? "Datastream inspected. Ansible is available on the controller."
        : "Datastream inspected. Install Ansible on the controller before scanning. Run kerkov setup ansible for guidance.");
    }
    this.setState();
  }

  renderSelection() {
    const host = this.host();
    const baseline = this.baseline();
    const remote = this.baselineId === "remote";
    const openscap = this.isOpenScap();
    const preset = this.scapPreset();
    const custom = this.customProfile();
    const auto = this.baselineId === "auto";
    this.el("compliance-auto").setAttribute("aria-pressed", String(auto));
    this.el("compliance-auto-options").hidden = !auto;
    this.el("compliance-custom").hidden = !!baseline || openscap || !!custom || auto;
    const scapSettings = this.el("compliance-openscap");
    scapSettings.hidden = !openscap || !!preset;
    if (openscap && !preset) scapSettings.open = true;
    this.el("compliance-local-profile").hidden = remote;
    this.text("compliance-project-settings-title", remote ? "Profile inputs (optional)" : "Existing project settings");
    this.text("compliance-project-help", remote ? "Kerkov downloads and prepares this project. Supply an inputs file when the upstream profile needs settings for your environment." : "Select a local InSpec project containing inspec.yml. Prepare its dependencies with inspec vendor before scanning. Assess asset does not require an Ansible playbook.");
    const bootstrap = this.field("bootstrap_python").closest("label");
    bootstrap.hidden = remote || openscap || !!custom || auto;
    bootstrap.nextElementSibling.hidden = bootstrap.hidden;
    this.text("compliance-action-help", openscap || custom || auto ? "" : remote ? "Community profiles are assessments without automatic fixes. Normal tool approvals apply; review the profile's coverage and any manual checks." : "Applies the selected baseline, then checks it again. Missing Python 3 is installed only when you enable the option above. Tool calls use your current approval mode. Preview does not install Python; other Ansible tasks can still make changes in check mode.");
    for (const field of this.el("compliance-custom").querySelectorAll(".compliance-custom-fix")) field.hidden = remote;
    if (remote) this.field("profile").value = this.projects.ready?.profile || "";
    const profileLabel = custom?.name || (auto ? "Auto assessment" : preset?.name) || (openscap ? "SCAP datastream" : remote ? this.projects.selectedName() : "Existing InSpec project");
    this.text("compliance-profile-name", baseline?.name || profileLabel);
    this.text("compliance-selection", host ? host.entry.name || host.entry.value : "No host selected");
    this.text("compliance-changes", custom || auto ? "" : baseline?.changes || (openscap ? "Stage the selected datastream and assess with OpenSCAP." : remote ? "Assess this downloaded community profile. Automatic fixes are not included." : "Assess an existing profile project. Configure an Ansible playbook only when requesting remediation."));
    this.text("compliance-coverage", baseline ? `${baseline.platform} · Built-in baseline v${baseline.version} · Covers these specific checks, not full CIS or PCI certification.` : "Your profile defines its coverage and supported operating systems.");
    this.updateSetupSummary(this.el("compliance-setup").open ? null : this.savedRun);
    this.connectionSummary();
    this.setState();
  }

  sshEndpoint(scan) {
    const address = scan.host.includes(":") ? "[" + scan.host + "]" : scan.host;
    return address + ":" + (scan.port || 22);
  }

  profileLabel(record) {
    const settings = record.request.scan.openscap;
    if (!settings || record.request.scap_preset_id) return record.label;
    const id = settings.profile_id;
    const known = this.projects.ready?.profiles?.find(profile => profile.id === id);
    const label = known?.title || id.split("_profile_").pop().replaceAll("_", " ").replace(/^./, letter => letter.toUpperCase());
    return `${settings.distribution} ${settings.version} / ${label}`;
  }

  updateSetupSummary(record = null) {
    const host = record ? this.inventory.entries.get(record.request.resource_id) : this.host();
    const scan = record?.request.scan;
    const address = scan?.host || host?.entry.value;
    const profile = record ? this.profileLabel(record) : this.baseline()?.name || this.scapPreset()?.name || this.customProfile()?.name ||
      (this.baselineId === "auto" ? "Auto assessment" : this.projects.ready?.name) || (this.isOpenScap() ? "SCAP datastream" : "Existing InSpec project");
    this.text("compliance-setup-summary", host?.entry.name || address || "Choose a host and profile");
    this.text("compliance-setup-profile", address ? `${scan ? this.sshEndpoint(scan) : address} · ${profile}` : "");
  }

  connectionSummary() {
    const ready = ["user", "private_key_file", "known_hosts_file"].every(name => this.field(name).value.trim());
    this.text("compliance-connection-summary", ready ? (this.accessSource ? "Access from engagement" : "Connection details entered") : "Set up access");
    this.updateSettingsStatus();
    this.updateFixActions();
  }

  hasFailedChecks(record) {
    if (!record || !["attention", "previewed"].includes(record.status)) return false;
    const counts = (record.after || record.before)?.counts;
    return counts ? counts.failed > 0 : (record.controls || []).some(control =>
      (control.after && control.after !== "unassessed" ? control.after : control.before) === "failed");
  }

  isPciFixSource(record) {
    const request = record?.request;
    return !!request?.scap_preset_id && request.scan.openscap?.profile_id === "xccdf_org.ssgproject.content_profile_pci-dss";
  }

  canPreviewFixes(record) {
    const remediation = record?.request.remediation;
    return this.hasFailedChecks(record) && !!(this.isPciFixSource(record) || record.request.baseline_id ||
      (remediation?.profile && remediation.playbook && remediation.limit));
  }

  customRemediationConfigured() {
    return ["automation_profile", "playbook", "host_alias"].every(name => this.fieldComplete(name));
  }

  reviewFixes(record) {
    if (!this.hasFailedChecks(record)) return;
    if (this.canPreviewFixes(record)) this.previewSavedRun(record);
    else this.fixes.reviewAssessment(record);
  }

  updateFixActions() {
    const record = this.savedRun;
    const scan = record?.request.scan;
    const matching = !!record && record.request.resource_id === this.host()?.id
      && scan.host === this.host()?.entry.value && scan.port === Number(this.field("port").value)
      && (record.request.baseline_id ? record.request.baseline_id === this.baselineId
        : this.baselineId === "custom" && scan.profile === this.field("profile").value.trim());
    const configured = this.baseline() || (this.baselineId === "custom" && this.customRemediationConfigured());
    const available = matching && configured && this.hasFailedChecks(record) && record.status !== "previewed";
    const preview = this.form.querySelector('[data-compliance-action="preview"]');
    preview.hidden = !available;
    preview.disabled = !this.inventory.canWrite() || !available || !!this.readiness.blockers.length;
    const resultPreview = this.el("compliance-result-preview");
    const canPreview = this.canPreviewFixes(record);
    resultPreview.textContent = canPreview ? "Preview fixes" : "Review fixes";
    resultPreview.hidden = !this.hasFailedChecks(record) || record.status === "previewed" ||
      (this.el("compliance-setup").open && available);
    resultPreview.disabled = !this.inventory.connected || (canPreview && (!this.inventory.canWrite() || !!this.readiness.blockers.length));
    this.el("compliance-preview-warning").hidden = preview.hidden;
    const checkMode = canPreview && !this.isPciFixSource(record);
    this.el("compliance-result-preview-warning").hidden = resultPreview.hidden || !checkMode;
    if (checkMode) resultPreview.setAttribute("aria-describedby", "compliance-result-preview-warning");
    else resultPreview.removeAttribute("aria-describedby");
  }

  setState() {
    const writable = this.inventory.canWrite();
    for (const control of this.form.elements) {
      const needsHost = control.name || (control.dataset.complianceAction && control.dataset.complianceAction !== "assess");
      control.disabled = !writable || (!!needsHost && !this.host());
    }
    const assessmentOnly = this.baselineId === "remote" || this.isOpenScap() || !!this.customProfile() || this.baselineId === "auto";
    if (assessmentOnly) this.field("bootstrap_python").disabled = true;
    if (this.baselineId === "remote") {
      this.form.querySelector('[data-compliance-action="assess"]').disabled ||= !this.projects.selected || !!this.projects.preparation;
    }
    this.el("compliance-scap-inspect").disabled = !writable || !this.host() || !!this.scapRequest || !this.fieldComplete("scap_datastream");
    this.field("scap_profile_id").disabled ||= !this.fieldComplete("scap_sha256");
    const recheck = this.el("compliance-scap-recheck");
    recheck.hidden = !this.host() || !this.ansibleBlocked();
    recheck.disabled = !writable || !!this.scapRequest;
    if (this.ansibleBlocked() || (this.baselineId === "openscap" && this.scapRequest)) {
      this.form.querySelector('[data-compliance-action="assess"]').disabled = true;
    }
    if (this.readiness.blockers.length) {
      for (const button of this.form.querySelectorAll('[data-compliance-action]')) button.disabled = true;
    }
    this.form.querySelector('[data-compliance-action="assess"]').disabled ||= !!this.pendingAssessment;
    this.projects.render();
    this.builder.render();
    if (this.fixes.dialog.open) this.fixes.render();
    this.el("compliance-auto").disabled = !writable;
    this.el("compliance-auto-goal").disabled = !writable;
    this.el("compliance-host").disabled = !writable || !this.host();
    for (const button of this.el("compliance-view").querySelectorAll("[data-compliance-baseline]")) button.disabled = !writable;
    this.el("compliance-customize").disabled = !writable;
    this.el("compliance-add-asset").disabled = !writable;
    this.el("compliance-access-select").disabled = !writable || !this.host();
    this.syncAccessSelection();
    this.el("compliance-access-find").disabled = !writable || !this.host() || !!this.pendingAssessment;
    this.el("compliance-access-refresh").disabled = !writable || !this.host() || !!this.accessRequest;
    this.el("compliance-review-apply").disabled = !writable;
    this.el("compliance-reassess").hidden = this.el("compliance-setup").open;
    this.el("compliance-reassess").disabled = !writable || !this.savedRun || this.savedRun.status === "running" || !this.savedRun.plan?.profile;
    this.el("compliance-history-refresh").disabled = !this.inventory.connected;
    for (const button of this.el("compliance-evidence-actions").querySelectorAll("button")) button.disabled = !this.inventory.connected;
    for (const button of this.el("compliance-controls").querySelectorAll("button")) button.disabled = !this.inventory.connected;
    for (const format of ["html", "json"]) this.el(`compliance-export-${format}`).disabled = !this.inventory.connected || !this.savedRun || this.savedRun.status === "running" || !!this.exportRequest || !!this.controlsRequest;
    for (const button of document.querySelectorAll("#message-stream [data-compliance-write]")) {
      button.disabled = !writable || !!this.readiness.blockers.length;
    }
    this.updateSettingsStatus();
    this.updateFixActions();
  }

  resetAccess() {
    clearTimeout(this.accessTimer);
    this.accessRequest = null;
    this.accessRefreshAgain = false;
    this.accessCandidates = [];
    this.accessTruncated = false;
    this.pendingAssessment = null;
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
      this.stopAssessmentSetup("Could not read recorded SSH access. Try Scan host again.");
      this.text("compliance-access-status", "Recorded access could not be retrieved. Enter details or try Refresh.");
    } else this.accessTimer = window.setTimeout(() => {
      if (this.accessRequest !== request) return;
      this.accessRequest = null;
      this.stopAssessmentSetup("The SSH access lookup did not respond. Try Scan host again.");
      this.text("compliance-access-status", "Access lookup did not respond. Enter details or try Refresh.");
      this.setState();
    }, 15000);
    this.setState();
  }

  syncAccessSelection() {
    if (!this.accessCandidates.length) return;
    const saved = this.el("compliance-setup").open ? null : this.savedRun?.request.scan;
    const active = saved || Object.fromEntries(COMPLIANCE_SSH_FIELDS.map(name => [name, this.field(name).value.trim()]));
    const sudo = saved ? saved.sudo : this.field("sudo").checked;
    const candidate = this.accessCandidates.find(({ fields }) =>
      COMPLIANCE_SSH_FIELDS.every(name => String(fields[name] ?? "") === String(active[name] ?? "")) &&
      (fields.sudo ?? true) === sudo);
    this.el("compliance-access-select").value = candidate ? String(candidate.id) : "";
  }

  applyAccess(candidate) {
    const fields = candidate.fields;
    for (const name of COMPLIANCE_SSH_FIELDS) this.field(name).value = fields[name] ?? "";
    this.field("sudo").checked = fields.sudo ?? true;
    this.accessSource = candidate.id;
    this.text("compliance-access-status", candidate.complete
      ? `Using recorded SSH access from ledger #${candidate.id}. Authentication is checked when the workflow runs.`
      : `Loaded partial access from ledger #${candidate.id}. Complete the missing connection details.`);
    this.connectionSummary();
  }

  selectEndpoint(candidate) {
    this.pendingAssessment = null;
    this.assessmentOf = null;
    if (this.savedRun || this.accessSource || this.accessDirty) this.resetHostSettings(true);
    if (this.savedRun) this.openHostSetup();
    this.accessDirty = true;
    this.applyAccess(candidate);
    this.text("compliance-status", "");
    this.setState();
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
      this.stopAssessmentSetup(payload.detail || "Recorded SSH access could not be retrieved.");
      this.text("compliance-access-status", payload.detail || "Recorded access could not be retrieved.");
      this.setState();
      return;
    }
    const host = this.host();
    if (!host || payload.entry_id !== host.id || payload.resource?.kind !== host.entry.kind || payload.resource?.value !== host.entry.value) {
      this.stopAssessmentSetup("The SSH access lookup no longer matches this host. Try Scan host again.");
      this.setState();
      return;
    }
    this.accessCandidates = payload.candidates || [];
    this.accessTruncated = payload.truncated === true;
    const select = this.el("compliance-access-select");
    select.replaceChildren(this.node("option", "", "Choose recorded access…"));
    select.firstChild.value = "";
    select.firstChild.disabled = true;
    for (const candidate of this.accessCandidates) {
      const option = this.node("option", "", `${candidate.fields.user} · port ${candidate.fields.port ?? "not recorded"} · ledger #${candidate.id}`);
      option.value = String(candidate.id);
      select.append(option);
    }
    if (!this.accessDirty && ((!this.accessCandidates.length && this.accessSource) || this.accessCandidates.length > 1 || payload.truncated)) {
      for (const name of COMPLIANCE_SSH_FIELDS) this.field(name).value = "";
      this.accessSource = null;
      this.connectionSummary();
    }
    this.el("compliance-access-choice").hidden = !this.accessCandidates.length || (this.accessCandidates.length === 1 && !this.accessDirty && !payload.truncated);
    if (!this.accessDirty && !payload.truncated && this.accessCandidates.length === 1) {
      this.applyAccess(this.accessCandidates[0]);
    } else this.text("compliance-access-status", this.accessCandidates.length
      ? (this.accessDirty ? "Recorded access is available. Your connection edits were kept." : "Multiple SSH connections are recorded. Choose the intended endpoint.")
      : "No recorded SSH file references found. Use Find known access to recover details from this engagement, or enter them here.");
    this.continueAssessmentSetup();
    this.setState();
  }

  hasSshAccess() {
    return COMPLIANCE_SSH_FIELDS.every(name => this.fieldComplete(name));
  }

  mustChooseEndpoint() {
    return this.accessCandidates.length > 1 || this.accessTruncated;
  }

  newWorkflowCard(action) {
    const titles = { assess: "Compliance assessment", preview: "Compliance preview", apply: "Compliance remediation", review: "Compliance fix review" };
    const status = this.node("p", "", action === "assess" ? "Preparing the assessment…" : "Checking workflow prerequisites…");
    status.setAttribute("role", "status");
    const target = this.node("p", "compliance-chat-target", "");
    target.hidden = true;
    const summary = this.node("div", "compliance-chat-summary", "");
    const controls = this.node("div", "compliance-chat-actions", "");
    const body = this.node("div", "compliance-chat-workflow", "");
    body.append(target, status, summary, controls);
    this.inventory.showView("conversation", false);
    const { article, moveToLatest } = this.inventory.appendComplianceCard(titles[action], body);
    article.tabIndex = -1;
    article.focus();
    return { article, moveToLatest, target, status, summary, controls, action, context: this.inventory.contextId };
  }

  assessmentConversation() {
    this.assessmentCard ||= this.newWorkflowCard("assess");
    this.inventory.showView("conversation", false);
    return this.assessmentCard;
  }

  assessmentButton(card, label, action) {
    const button = this.node("button", "inventory-button", label);
    button.type = "button";
    button.addEventListener("click", () => {
      if (card === this.assessmentCard && card.context === this.inventory.contextId && this.inventory.canWrite()) action();
    });
    return button;
  }

  chooseAssessmentTarget(prompt, choices) {
    const card = this.assessmentConversation();
    card.status.textContent = prompt;
    card.controls.replaceChildren(
      ...choices.map(({ label, select }) => this.assessmentButton(card, label, select)),
      this.assessmentButton(card, "Cancel", () => this.finishAssessmentCard("Assessment cancelled.")),
    );
    card.controls.firstElementChild.focus();
  }

  requestAssessmentHost() {
    const card = this.assessmentConversation();
    card.status.textContent = "Which host should I assess?";
    const form = this.node("form", "compliance-chat-actions", "");
    const label = this.node("label", "", "Hostname or IP address");
    const field = document.createElement("input");
    field.maxLength = 253;
    field.required = true;
    field.autocomplete = "off";
    label.append(field);
    const submit = this.node("button", "inventory-button inventory-primary", "Use host");
    submit.type = "submit";
    form.append(label, submit);
    const cancel = this.assessmentButton(card, "Cancel", () => this.finishAssessmentCard("Assessment cancelled."));
    form.append(cancel);
    form.addEventListener("submit", event => {
      event.preventDefault();
      if (card !== this.assessmentCard || card.context !== this.inventory.contextId || !this.inventory.canWrite()) return;
      const value = field.value.trim();
      if (!value) return;
      const kind = value.includes(":") || /^[0-9]+(?:\.[0-9]+){3}$/.test(value) ? "ip" : "domain";
      submit.disabled = cancel.disabled = field.disabled = true;
      card.status.textContent = "Saving the target…";
      this.inventory.saveEntry("assets", { kind, value }, {
        onSaved: id => {
          if (card !== this.assessmentCard || card.context !== this.inventory.contextId) return;
          if (!this.selectResource(id, false, true)) {
            this.finishAssessmentCard("The saved host could not be loaded. Choose Scan host again.");
            return;
          }
          this.scanHost();
        },
        onError: detail => {
          card.status.textContent = detail;
          submit.disabled = cancel.disabled = field.disabled = false;
          field.focus();
        },
      });
    });
    card.controls.replaceChildren(form);
    field.focus();
  }

  finishAssessmentCard(detail, record = null) {
    const card = this.assessmentCard;
    this.assessmentCard = null;
    this.finishWorkflowCard(card, detail, record);
  }

  setCardTarget(card, scan, label) {
    card.target.textContent = `${this.sshEndpoint(scan)} · ${label}`;
    card.target.hidden = false;
  }

  cardButton(card, label, action, write = false) {
    const button = this.node("button", "inventory-button", label);
    button.type = "button";
    if (write) {
      button.dataset.complianceWrite = "";
      button.disabled = !this.inventory.canWrite() || !!this.readiness.blockers.length;
    }
    button.addEventListener("click", () => {
      if (card.context === this.inventory.contextId && (!write || this.inventory.canWrite())) action();
    });
    return button;
  }

  finishWorkflowCard(card, detail, record = null) {
    if (!card) return;
    card.status.textContent = detail;
    card.controls.replaceChildren();
    if (!record) return;
    if (card.action !== "assess") this.renderRemediationSummary(card, record);
    if (card.action === "assess" && this.canPreviewFixes(record)) {
      const preview = this.cardButton(card, "Preview fixes", () => this.previewSavedRun(record), true);
      if (!this.isPciFixSource(record)) {
        const warning = this.node("p", "compliance-caption", this.el("compliance-result-preview-warning").textContent);
        warning.id = `compliance-chat-preview-warning-${record.id}`;
        card.summary.append(warning);
        preview.setAttribute("aria-describedby", warning.id);
      }
      card.controls.append(preview);
    } else if (card.action === "assess" && this.hasFailedChecks(record)) {
      card.controls.append(this.cardButton(card, "Review fixes", () => this.reviewFixes(record)));
    }
    if (card.action === "preview" && record.status === "previewed") {
      this.previewCards.set(record.id, card);
      const fixes = record.plan?.scap_fixes;
      if (!fixes || fixes.selected.length) {
        const apply = this.cardButton(card, "Apply fixes", () => this.applyPreview(card, record), true);
        apply.classList.add("inventory-primary");
        card.controls.append(apply);
        if (fixes) this.renderScapFixPicker(card, record, apply);
        if (fixes && record.request.scap_rule_ids) {
          card.controls.append(this.cardButton(card, "Preview Auto fixes", () => this.previewScapFixes(record), true));
        }
      }
    }
    const link = { assess: "View assessment results", preview: "View preview evidence", apply: "View remediation results" }[card.action];
    card.controls.append(this.cardButton(card, link, () => {
      this.openSavedRun(this.records.get(record.id) || record);
      this.inventory.showView("compliance");
    }));
    if (card.action !== "assess") card.moveToLatest();
  }

  renderRemediationSummary(card, record) {
    const preview = card.action === "preview";
    const recap = record.remediation;
    card.summary.replaceChildren();
    if (preview && record.plan?.scap_fixes) {
      this.renderScapFixSummary(card, record);
      return;
    }
    if (Number.isInteger(recap?.totals?.changed)) {
      const count = recap.totals.changed;
      const changes = `${count || "no"} task ${count === 1 ? "change" : "changes"}`;
      const summary = preview ? `Ansible check mode predicts ${changes}.` : `Ansible reported ${changes}.`;
      card.summary.append(this.node("p", "", summary));
    }
    if (recap?.changed_tasks?.length) {
      const tasks = this.node("ul", "", "");
      for (const { task } of recap.changed_tasks) tasks.append(this.node("li", "", task === "[no_log]" ? "Task name hidden by the playbook" : task));
      card.summary.append(tasks);
      if (recap.changed_tasks_truncated) card.summary.append(this.node("p", "", "More changes are recorded in the Ansible evidence."));
    }
    if (preview && recap) {
      const mapping = record.plan?.baseline?.remediation || {};
      const fixes = (record.controls || [])
        .filter(control => control.before === "failed" && Object.hasOwn(mapping, control.control_id))
        .map(control => this.node("li", "", mapping[control.control_id]));
      if (fixes.length) {
        const list = this.node("ul", "", "");
        list.append(...fixes);
        card.summary.append(this.node("p", "", "Proposed settings for failed checks"), list);
      } else if (record.request.remediation?.playbook) {
        card.summary.append(this.node("p", "", `Selected playbook is ${record.request.remediation.playbook}.`));
      }
      if (!Array.isArray(recap.changed_tasks)) card.summary.append(this.node("p", "", "This saved preview predates task summaries. Open its remediation log for task output."));
    }
    if (!preview && recap) {
      const counts = record.after?.counts;
      card.summary.append(this.node("p", "", counts
        ? `Follow-up assessment found ${counts.passed} passed, ${counts.failed} failed, and ${counts.error} errored checks.`
        : "Follow-up verification is unavailable. Check the saved evidence before retrying."));
    }
  }

  renderScapFixSummary(card, record) {
    const fixes = record.plan.scap_fixes;
    const selected = new Set(fixes.selected);
    const mode = record.request.scap_rule_ids ? "Selected fixes" : "Auto fixes";
    card.summary.append(this.node("p", "", `${mode} · ${selected.size} failed ${selected.size === 1 ? "check" : "checks"} · ${fixes.task_count} planned ${fixes.task_count === 1 ? "task" : "tasks"}.`));
    if (selected.size) {
      const planned = this.node("details", "", "");
      const ruleList = this.node("ul", "compliance-fix-scroll", "");
      for (const fix of fixes.available) {
        if (!selected.has(fix.id)) continue;
        const item = this.node("li", "", "");
        const tasks = this.node("ul", "", "");
        for (const task of fix.tasks) tasks.append(this.node("li", "", task));
        item.append(this.node("strong", "", fix.title), tasks);
        ruleList.append(item);
      }
      planned.append(this.node("summary", "", "Planned changes"));
      const modules = Object.entries(fixes.modules || {});
      if (modules.length) {
        const versions = modules.map(([name, source]) => name + " " + source.version);
        planned.append(this.node("p", "compliance-caption", "Pinned Ansible modules · " + versions.join(", ")));
      }
      planned.append(ruleList);
      card.summary.append(this.node("p", "compliance-caption",
        "This preview uses saved evidence. Applying can change system, authentication, and network settings."), planned);
    }
    if (fixes.manual.length) {
      const manual = this.node("details", "", "");
      const list = this.node("ul", "compliance-fix-scroll", "");
      for (const fix of fixes.manual) list.append(this.node("li", "", `${fix.title} · ${fix.reason}`));
      manual.append(this.node("summary", "", `${fixes.manual.length} checks need manual review`), list);
      card.summary.append(manual);
    }
  }

  renderScapFixPicker(card, record, apply) {
    const fixes = record.plan.scap_fixes;
    const picker = this.node("details", "", "");
    const options = this.node("fieldset", "compliance-fix-picker compliance-fix-scroll", "");
    options.append(this.node("legend", "", "Checks to fix"));
    const selected = new Set(record.request.scap_rule_ids || []);
    for (const fix of fixes.available) {
      const label = this.node("label", "", "");
      const input = this.node("input", "", "");
      input.type = "checkbox";
      input.value = fix.id;
      input.checked = selected.has(fix.id);
      label.append(input, this.node("span", "", fix.title));
      options.append(label);
    }
    const status = this.node("p", "compliance-caption", "");
    status.setAttribute("role", "status");
    const preview = this.cardButton(card, "Preview selected fixes", () => {
      const ids = Array.from(options.querySelectorAll("input:checked"), input => input.value);
      if (ids.length) this.previewScapFixes(record, ids);
      else status.textContent = "Select at least one check to preview.";
    }, true);
    picker.append(this.node("summary", "", "Choose fixes"), options, status, preview);
    picker.addEventListener("toggle", () => { apply.hidden = picker.open; });
    card.summary.append(picker);
  }

  previewScapFixes(record, selected = null) {
    if (!this.inventory.canWrite() || !this.savedAssetMatches(record)) return;
    const request = record.request;
    this.launch({
      ...request,
      action: "preview",
      assessment_of: null,
      preview_id: null,
      scap_fixes_of: record.status === "previewed" ? request.scap_fixes_of : record.id,
      scap_rule_ids: selected,
    }, this.profileLabel(record));
  }

  previewSavedRun(record) {
    if (!this.canPreviewFixes(record)) return;
    if (this.isPciFixSource(record)) this.previewScapFixes(record);
    else if (this.restoreSettings(record)) this.start("preview");
  }

  reviewPreview(record) {
    if (record?.status !== "previewed") return;
    const existing = this.previewCards.get(record.id);
    if (existing?.article.isConnected) {
      this.inventory.showView("conversation", false);
      existing.article.focus();
      return;
    }
    const card = this.newWorkflowCard("preview");
    this.setCardTarget(card, record.request.scan, this.profileLabel(record));
    this.finishWorkflowCard(card, record.detail, record);
  }

  applyPreview(card, record) {
    const { request } = record;
    if (record.status !== "previewed" || this.readiness.blockers.length) return;
    if (!this.savedAssetMatches(record)) {
      card.status.textContent = "The saved host is unavailable. Refresh inventory and review the preview again.";
      return;
    }
    this.launch({
      ...request, action: "apply", preview_id: record.id,
      remediation: request.remediation ? { ...request.remediation, check: false } : null,
    }, this.profileLabel(record));
  }

  scanHost() {
    if (!this.inventory.canWrite() || this.pendingAssessment) return;
    const remote = this.baselineId === "remote";
    const profileReady = remote ? !!this.projects.selected
      : this.assessmentFields().slice(COMPLIANCE_SSH_FIELDS.length).every(name => this.fieldComplete(name));
    if (!profileReady || this.readiness.blockers.length || this.ansibleBlocked()) {
      this.start("assess");
      return;
    }
    const hosts = this.hosts();
    if (!hosts.length) {
      this.requestAssessmentHost();
      return;
    }
    if (hosts.length > 1 && this.chosenHostIdentity !== this.selectedIdentity) {
      this.chooseAssessmentTarget("Which host should I assess?", hosts.map(row => ({
        label: row.entry.name ? row.entry.name + " (" + row.entry.value + ")" : row.entry.value,
        select: () => {
          if (this.selectResource(row.id, false, true)) this.scanHost();
        },
      })));
      return;
    }
    const card = this.assessmentConversation();
    card.controls.replaceChildren();
    if (this.hasSshAccess() && (!remote || this.projects.ready)) {
      this.start("assess");
      return;
    }
    this.pendingAssessment = {
      baselineId: this.baselineId,
      projectSequence: remote ? this.projects.sequence : null,
      attemptedLookup: false,
      checkedProject: false,
      projectCommit: null,
    };
    this.continueAssessmentSetup();
    this.setState();
  }

  stopAssessmentSetup(detail) {
    if (!this.pendingAssessment) return;
    this.pendingAssessment = null;
    this.text("compliance-status", detail);
    this.finishAssessmentCard(detail || "Assessment setup changed. Choose Scan host again.");
  }

  continueAssessmentSetup() {
    const pending = this.pendingAssessment;
    if (!pending || this.workflowId) return;
    const remote = pending.projectSequence !== null;
    if (!this.inventory.canWrite() || pending.baselineId !== this.baselineId ||
        (remote && pending.projectSequence !== this.projects.sequence)) {
      this.stopAssessmentSetup("Assessment setup changed. Choose Scan host again.");
      return;
    }
    if (!this.hasSshAccess()) {
      this.continueSshSetup(pending);
      return;
    }
    if (remote && !this.projects.ready) {
      this.continueProjectSetup(pending);
      return;
    }
    if (pending.projectCommit && pending.projectCommit !== this.projects.ready?.commit) {
      this.stopAssessmentSetup("The prepared profile revision changed. Review the selected profile before scanning.");
      return;
    }
    this.pendingAssessment = null;
    this.start("assess");
  }

  continueSshSetup(pending) {
    if (this.accessRequest) {
      this.text("compliance-status", "Checking recorded SSH access…");
    } else if (this.mustChooseEndpoint()) {
      this.pendingAssessment = null;
      const identity = this.selectedIdentity;
      this.chooseAssessmentTarget("Which SSH endpoint should I assess?", this.accessCandidates.map(candidate => ({
        label: (candidate.fields.port ? this.sshEndpoint({ host: this.host().entry.value, port: candidate.fields.port })
          : this.host().entry.value + " · port unknown") + " · " + (candidate.fields.user || "No SSH user recorded"),
        select: () => {
          const current = this.accessCandidates.find(item => item.id === candidate.id);
          if (identity !== this.selectedIdentity || !current) {
            this.finishAssessmentCard("The recorded endpoint changed. Choose Scan host again.");
            return;
          }
          this.selectEndpoint(current);
          if (this.hasSshAccess()) this.scanHost();
          else this.finishAssessmentCard("This endpoint has incomplete SSH access. Review its settings in Compliance → Customize.");
        },
      })));
    } else if (pending.attemptedLookup) {
      this.stopAssessmentSetup("No complete SSH access was recovered. Review the missing access in Customize.");
    } else {
      pending.attemptedLookup = true;
      if (!this.findAccess()) this.stopAssessmentSetup("Could not look up SSH access. Try Scan host again.");
    }
  }

  continueProjectSetup(pending) {
    if (this.projects.selectionPending()) {
      pending.checkedProject = true;
      this.text("compliance-status", "Checking the selected community profile…");
    } else if (pending.projectCommit) {
      this.stopAssessmentSetup("The prepared profile could not be verified. Review the profile details before scanning.");
    } else if (this.projects.validated?.commit && !this.projects.validated.refresh) {
      pending.projectCommit = this.projects.validated.commit;
      if (!this.projects.prepare(false)) this.stopAssessmentSetup("Could not start profile preparation. Try Scan host again.");
      else this.text("compliance-status", "Preparing the selected profile before the assessment…");
    } else if (!pending.checkedProject) {
      pending.checkedProject = true;
      this.text("compliance-status", "Checking the selected community profile…");
      this.projects.checkSelection();
    } else {
      this.stopAssessmentSetup("The profile is unavailable. Review the profile details and try again.");
    }
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
    const messageId = this.inventory.startWorkflow(message, null, null, !!this.assessmentCard);
    if (messageId) {
      this.workflowId = messageId;
      this.workflowError = null;
      this.text("compliance-status", "Finding recorded access…");
      this.el("compliance-workflow-output").textContent = "";
      this.setState();
    }
    return !!messageId;
  }

  showInvalidAssessmentSettings(field, detail) {
    this.text("compliance-status", detail);
    if (this.assessmentCard) {
      this.finishAssessmentCard(detail);
      this.inventory.showView("compliance", false);
    }
    this.openSettings(field);
    field.focus();
  }

  start(action) {
    const assessmentOf = this.assessmentOf;
    this.assessmentOf = null;
    const host = this.host();
    if (!host || !this.inventory.canWrite() || !["assess", "preview"].includes(action)) return;
    if (this.readiness.blockers.length) {
      this.text("compliance-status", "Resolve the conditions listed above before running.");
      return;
    }
    const baseline = this.baseline();
    const openscap = this.isOpenScap();
    const preset = this.scapPreset();
    const custom = this.customProfile();
    const auto = this.baselineId === "auto";
    if ((openscap || custom || auto) && action !== "assess") return;
    if (this.ansibleBlocked()) {
      const recheck = this.el("compliance-scap-recheck");
      this.openSettings(recheck);
      recheck.focus();
      return;
    }
    if (openscap && !preset && !this.fieldComplete("scap_sha256")) {
      const field = this.fieldComplete("scap_datastream") ? this.el("compliance-scap-inspect") : this.field("scap_datastream");
      this.openSettings(field);
      this.text("compliance-status", "Inspect the datastream before scanning.");
      field.focus();
      return;
    }
    if (this.baselineId === "remote" && (action !== "assess" || !this.projects.ready || this.projects.preparation)) {
      this.text("compliance-status", "Download and prepare this assessment profile first. Community profiles do not include automatic fixes.");
      return;
    }
    const required = this.assessmentFields();
    const customFields = ["profile", "inputs_file", "automation_profile", "playbook", "host_alias", "vars_file"];
    if (!openscap && !baseline && action !== "assess") required.push("automation_profile", "playbook", "host_alias");
    const values = {};
    for (const input of this.form.querySelectorAll("input, select")) {
      if (input.type === "checkbox" || !input.name) continue;
      input.required = required.includes(input.name);
      if ((!openscap || preset) && input.name.startsWith("scap_")) continue;
      if (customFields.includes(input.name) && (baseline || openscap || custom || auto || (action === "assess" && !["profile", "inputs_file"].includes(input.name)))) continue;
      values[input.name] = input.value.trim();
      if (!input.checkValidity() || (input.required && !values[input.name]) || /[\x00-\x1f\x7f]/.test(values[input.name])) {
        this.showInvalidAssessmentSettings(input, "Complete the connection and selected profile settings to continue.");
        input.reportValidity();
        return;
      }
    }
    if (!baseline && action === "preview" && ["all", "ungrouped"].includes(values.host_alias)) {
      this.text("compliance-status", "Choose a single host alias, not an inventory group.");
      return;
    }
    const scan = {
      host: host.entry.value, user: values.user, port: Number(values.port),
      profile: auto ? "auto" : custom?.id || preset?.filename ||
        (openscap ? values.scap_datastream : baseline?.profile || values.profile),
      private_key_file: values.private_key_file, known_hosts_file: values.known_hosts_file, sudo: this.field("sudo").checked,
    };
    if (auto) {
      this.startRecommendation(host.id, scan);
      return;
    }
    if (openscap) scan.openscap = preset?.openscap || {
      distribution: values.scap_distribution, version: values.scap_version,
      profile_id: values.scap_profile_id, sha256: values.scap_sha256,
    };
    if (!baseline && values.inputs_file) scan.inputs_file = values.inputs_file;
    const remediation = baseline || action === "assess" ? null : {
      action: "run", profile: values.automation_profile, playbook: values.playbook,
      limit: values.host_alias, check: true, bootstrap_python: this.field("bootstrap_python").checked,
      vars_files: values.vars_file ? [values.vars_file] : [],
    };
    const label = custom?.name || baseline?.name || preset?.name || (openscap ? values.scap_profile_id : this.baselineId === "remote" ? this.projects.selectedName() : "Custom baseline");
    this.launch({
      action, resource_id: host.id, baseline_id: baseline?.id || null, scan,
      ...(preset ? { scap_preset_id: preset.id } : {}),
      ...(custom ? { custom_profile: { id: custom.id, revision: custom.revision } } : {}),
      remediation, bootstrap_python: action === "preview" && this.field("bootstrap_python").checked,
      assessment_of: action === "assess" ? assessmentOf : null,
    }, label);
  }

  startRecommendation(resourceId, scan) {
    const card = this.assessmentConversation();
    card.controls.replaceChildren();
    this.setCardTarget(card, scan, "Auto assessment");
    const goal = this.el("compliance-auto-goal").value;
    const request = { action: "recommend", goal, resource_id: resourceId, scan };
    this.workflowError = null;
    const id = this.inventory.startWorkflow("Identify the host platform for assessment", request, null, true);
    if (!id) {
      this.finishAssessmentCard("Could not start host detection. Check the connection and try again.");
      return;
    }
    this.workflowId = id;
    this.recommendation = { id, card, scan, resourceId, result: null };
    this.text("compliance-status", "Checking the selected host's Linux release…");
  }

  finishRecommendation(type, payload) {
    const { card, scan, result, resourceId } = this.recommendation;
    this.recommendation = null;
    this.workflowId = null;
    this.assessmentCard = null;
    const candidate = result && this.projects.catalog.find(item => item.id === result.id);
    const detail = payload.stopped ? "Host detection stopped." : type === "user.message.rejected"
      ? payload.detail : this.workflowError;
    card.status.textContent = detail || (candidate ? `Recommended profile · ${candidate.name}`
      : "No profile recommendation was received. Choose a profile manually or retry Auto assessment.");
    if (!candidate || detail) return;
    card.summary.append(this.node("p", "", result.reason));
    const scanButton = this.cardButton(card, "Scan with recommended profile", () => {
      const current = this.host();
      const sameEndpoint = COMPLIANCE_SSH_FIELDS.every(name =>
        name === "port" ? scan.port === Number(this.field(name).value) :
          scan[name] === this.field(name).value.trim()) && scan.sudo === this.field("sudo").checked;
      if (current?.id !== resourceId || current.entry.value !== scan.host || !sameEndpoint) {
        card.status.textContent = "The selected SSH endpoint changed. Run Auto assessment for the current host.";
        card.controls.replaceChildren();
        return;
      }
      card.controls.replaceChildren();
      if (candidate.openscap) this.selectScapPreset(candidate.id);
      else this.projects.select(candidate.url);
      this.scanHost();
    }, true);
    scanButton.classList.add("inventory-primary");
    card.controls.append(scanButton);
    card.moveToLatest();
  }

  launch(request, label) {
    const { action, scan } = request;
    const assessment = action === "assess";
    if (!assessment) {
      this.pendingAssessment = null;
      this.finishAssessmentCard("Assessment setup ended when the fix workflow started.");
    }
    const card = assessment ? this.assessmentConversation() : this.newWorkflowCard(action);
    card.controls.replaceChildren();
    this.setCardTarget(card, scan, label);
    const message = `${action} compliance for ${scan.host} against ${label}.`;
    const messageId = this.inventory.startWorkflow(message, request, null, true);
    if (!messageId) {
      const detail = "Unable to start. Check the connection and wait for current work to finish.";
      if (assessment) this.finishAssessmentCard(detail);
      else this.finishWorkflowCard(card, detail);
      this.text("compliance-status", detail);
      return;
    }
    this.savedRun = null;
    this.defaultFilterRunId = null;
    this.assessmentOf = null;
    this.el("compliance-run-review").hidden = true;
    this.workflowId = messageId;
    this.workflowError = null;
    this.run = { messageId, action, card, record: null };
    this.result("Workflow started", `${this.sshEndpoint(scan)} · ${label}. Follow progress and approvals in Conversation.`);
    this.el("compliance-workflow-output").textContent = "";
    const status = { assess: "Starting assessment…", preview: "Starting preview…", apply: "Starting remediation…" };
    this.text("compliance-status", status[action]);
  }

  result(title, detail) {
    this.el("compliance-result").hidden = false;
    this.text("compliance-result-title", title);
    this.text("compliance-result-detail", detail);
  }

  handleWorkflowEvent(type, payload, conversation = false) {
    this.projects.handleWorkflowEvent(type, payload);
    this.handleEvent(type, payload);
    if (type === "compliance.recommended" && this.recommendation) {
      this.recommendation.result = payload;
      return;
    }
    if (!conversation) {
      this.el("compliance-workflow-details").hidden = false;
      this.el("compliance-run-details").hidden = false;
    }
    if (type === "tool.start") {
      const stages = { exec: this.recommendation ? "Reading the host Linux release…" : "Working…", compliance_scan: this.savedRun?.stage === "verification" ? "Verifying baseline…" : "Assessing baseline…",
        ansible: this.run?.action === "preview" ? "Previewing changes…" : "Applying baseline…",
        write: "Preparing configuration…", ledger: "Updating engagement context…", recall: "Retrieving known context…" };
      this.text("compliance-status", stages[payload.name] || "Working…");
    }
    if (type === "tool.result" && !conversation) {
      const output = this.el("compliance-workflow-output");
      output.textContent += `\n${payload.success ? "Finished" : "Failed"}: ${payload.name}\n${payload.error || payload.output || ""}\n`;
      output.textContent = output.textContent.slice(-32000);
    }
    if (["error", "provider.error"].includes(type)) this.workflowError = payload.detail || null;
    if (conversation && this.workflowError && ["error", "provider.error"].includes(type)) {
      this.text("compliance-status", this.workflowError);
    }
    if (!conversation && ["assistant.message", "error", "provider.error", "log.warning"].includes(type)) {
      const output = this.el("compliance-workflow-output");
      output.textContent = (output.textContent + "\n" + (payload.content || payload.detail || payload.message || "")).slice(-32000);
    }
  }

  handleEvent(type, payload) {
    if (["compliance.fixes", "compliance.fix.import", "compliance.fix.review", "compliance.fix.plan"].includes(type)) {
      this.fixes.handle(type, payload);
      return;
    }
    if (type === "compliance.custom.save") {
      this.builder.handle(payload);
      return;
    }
    const projectPreparation = payload.message_id === this.projects.preparation?.id ? this.projects.preparation : null;
    this.projects.handle(type, payload);
    if (type === "compliance.datastream.inspect" || type === "compliance.ansible.check") {
      this.receiveScapResult(type, payload);
      return;
    }
    if (type === "compliance.controls") {
      const request = this.controlsRequest;
      if (!request || payload.context_id !== this.inventory.contextId || payload.run_id !== this.savedRun?.id || payload.request_id !== request.id) return;
      this.controlsRequest = null;
      clearTimeout(this.controlsTimer);
      if (payload.retry_after_ms && Date.now() < this.controlsRetryDeadline) {
        clearTimeout(this.controlsDebounce);
        this.controlsDebounce = setTimeout(() => this.requestControls(), payload.retry_after_ms);
        return;
      }
      if (request.key === this.controlsKey) {
        this.controlsResult = payload;
        if (Number.isInteger(payload.page)) {
          this.controlsPage = payload.page;
          this.controlsQuery = { ...this.controlsQuery, page: payload.page };
          this.controlsKey = this.controlQueryKey(this.controlsQuery);
        }
      }
      this.renderControlRows(this.controlsPage);
      this.setState();
      return;
    }
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
    if (["session.ready", "mode.changed", "engagement.resumed"].includes(type) && payload.compliance) {
      this.readiness = payload.compliance;
      this.renderReadiness();
    }
    if (type === "compliance.snapshot" || type === "compliance.updated") {
      if (payload.context_id !== this.inventory.contextId) return;
      if (type === "compliance.updated" && this.run && payload.message_id === this.run.messageId) {
        this.run.record = payload.run;
        if (payload.run.detail) this.run.card.status.textContent = payload.run.detail;
      }
      const records = type === "compliance.snapshot" ? payload.runs : [payload.run];
      if (type === "compliance.snapshot") this.records.clear();
      for (const record of records || []) this.records.set(record.id, record);
      this.renderHistory();
      if (type === "compliance.updated" && (this.recordMatchesHost(payload.run) || payload.message_id === this.run?.messageId)) this.showSavedRun(payload.run);
      else if (type === "compliance.snapshot" && this.savedRun && this.records.has(this.savedRun.id)) this.showSavedRun(this.records.get(this.savedRun.id));
      else if (type === "compliance.snapshot" && !this.savedRun && this.autoShowHistory) {
        const latest = records?.find(record => this.recordMatchesHost(record));
        if (latest) this.showSavedRun(latest);
      }
      return;
    }
    if (type === "compliance.evidence") {
      if (payload.context_id !== this.inventory.contextId || payload.run_id !== this.savedRun?.id) return;
      this.el("compliance-evidence-preview").hidden = false;
      this.text("compliance-evidence-preview", payload.body || payload.detail || "No evidence available.");
      return;
    }
    this.receiveAccess(type, payload);
    if (this.recommendation?.id === payload.message_id && ["user.message.complete", "user.message.rejected"].includes(type)) {
      this.finishRecommendation(type, payload);
      return;
    }
    if (type === "approval.request" && this.workflowId) this.text("compliance-status", "Approval required to continue.");
    if (payload.message_id === this.workflowId && ["user.message.complete", "user.message.rejected"].includes(type)) {
      const accessDiscovery = !this.run && !projectPreparation;
      this.text("compliance-status", this.completionStatus(type, payload, projectPreparation));
      this.workflowId = null;
      if (accessDiscovery) {
        if (payload.stopped || type === "user.message.rejected" || this.workflowError) {
          this.stopAssessmentSetup(this.completionStatus(type, payload, null));
        }
        this.requestAccess(true);
      }
    }
    if (!this.run) return;
    const run = this.run;
    if (["user.message.complete", "user.message.rejected"].includes(type) && payload.message_id === run.messageId) {
      if (!this.savedRun || this.savedRun.status === "running") this.result(
        payload.stopped ? "Workflow stopped" : type === "user.message.rejected" ? "Workflow could not start" : "Refreshing saved assessment",
        payload.detail || "No verified assessment result received yet. Refresh assessment history to review the server-recorded outcome.",
      );
      const record = run.record?.status !== "running" ? run.record : null;
      const detail = this.completionStatus(type, payload, null) || record?.detail || "Workflow ended without a confirmed result.";
      if (run.action === "assess") this.finishAssessmentCard(detail, record);
      else this.finishWorkflowCard(run.card, detail, record);
      this.run = null;
      this.workflowId = null;
      this.refreshHistory();
    }
  }

  completionStatus(type, payload, projectPreparation) {
    if (payload.stopped) return "Workflow stopped.";
    if (type === "user.message.rejected") return payload.detail;
    if (projectPreparation) {
      if (projectPreparation.error) return projectPreparation.error;
      return this.pendingAssessment ? "Checking the prepared profile before the assessment…"
        : "Project preparation finished. Review its ready status above.";
    }
    const run = this.run?.messageId === payload.message_id ? this.run : null;
    const outcomes = { failed: "failed", blocked: "was blocked", interrupted: "was interrupted", incomplete: "is incomplete" };
    if (run?.record && outcomes[run.record.status]) {
      const subject = { assess: "Assessment", preview: "Preview", apply: "Remediation" }[run.action] || "Workflow";
      return `${subject} ${outcomes[run.record.status]}: ${run.record.detail}`;
    }
    if (!run?.record && this.workflowError) return `Workflow failed: ${this.workflowError}`;
    if (!run) return this.pendingAssessment ? "Checking the recovered SSH access…"
      : "Access lookup finished. Review the SSH connection, then choose Scan host.";
    if (this.savedRun && this.savedRun.status !== "running") return "";
    return "No saved assessment result was received. Check the activity log or refresh assessment history.";
  }

  renderReadiness() {
    const { blockers = [], mode_notice: notice } = this.readiness;
    const items = blockers.map(text => this.node("li", "compliance-check-blocked", `${text}.`));
    if (notice) items.push(this.node("li", "", notice));
    this.el("compliance-precondition-list").replaceChildren(...items);
    this.el("compliance-preconditions").hidden = !items.length;
    this.setState();
  }

  refreshHistory() {
    if (!this.inventory.connected || !this.inventory.contextId) return;
    this.inventory.send({ type: "compliance.list", context_id: this.inventory.contextId });
  }

  openSavedRun(record) {
    const asset = this.inventory.entries.get(record.request.resource_id);
    if (asset?.entry.value === record.request.scan.host) this.selectResource(asset.id, false, true);
    this.showSavedRun(record);
  }

  renderHistory() {
    const records = Array.from(this.records.values()).sort((a, b) => b.created_at.localeCompare(a.created_at));
    this.el("compliance-history-details").hidden = !records.length;
    this.el("compliance-history").replaceChildren(...records.map(record => {
      const button = this.node("button", "compliance-history-item", "");
      button.type = "button";
      button.append(this.node("strong", "", `${this.sshEndpoint(record.request.scan)} · ${this.profileLabel(record)}`),
        this.node("span", "", `${record.status} · ${new Date(record.updated_at).toLocaleString()}`));
      button.addEventListener("click", () => this.openSavedRun(record));
      return button;
    }));
    if (!records.length) this.el("compliance-history").append(this.node("p", "compliance-caption", "No saved assessments in this engagement."));
  }

  showSavedRun(record) {
    if (this.savedRun?.id !== record.id) {
      this.closeReviewRequests();
      this.el("compliance-control-search").value = "";
      this.el("compliance-control-filter").value = "all";
      this.defaultFilterRunId = null;
      this.el("compliance-control-impact").value = "all";
      this.el("compliance-control-sort").value = "priority";
      this.el("compliance-setup").open = false;
    }
    this.savedRun = record;
    this.updateSetupSummary(record);
    this.el("compliance-run-review").hidden = false;
    this.el("compliance-run-details").hidden = false;
    this.el("compliance-evidence-preview").hidden = true;
    const titles = { running: "Workflow in progress", previewed: "Preview ready for review", verified: "Baseline verified", passed: "Baseline checks passed", attention: "Baseline needs attention", interrupted: "Workflow interrupted", failed: "Workflow needs review", blocked: "Workflow blocked", incomplete: "Workflow incomplete" };
    this.result(titles[record.status] || record.status, `${this.sshEndpoint(record.request.scan)} · ${this.profileLabel(record)}`);
    this.el("compliance-result").dataset.status = record.status;
    this.text("compliance-run-detail", record.detail);
    this.el("compliance-outcome-notice").hidden = ["attention", "passed", "verified"].includes(record.status);
    this.text("compliance-outcome-notice", record.detail);
    this.text("compliance-run-time", new Date(record.updated_at).toLocaleString());
    const stages = record.request.action === "assess" ? ["readiness", "assessment"] : record.request.action === "preview" ? ["readiness", "assessment", "preview"] : ["readiness", "assessment", "remediation", "verification"];
    this.el("compliance-stages").replaceChildren(...stages.map(stage => {
      const node = this.node("li", "", stage);
      if (stage === record.stage) node.setAttribute("aria-current", "step");
      return node;
    }));
    this.text("compliance-readiness-context", record.status === "running" ? "Checks performed before execution." : "Preflight checks recorded before execution; assessment outcomes are shown above.");
    this.el("compliance-readiness").replaceChildren(...(record.readiness || []).map(check => this.node("li", `compliance-check-${check.status}`, `${check.name}: ${check.detail}`)));
    const project = record.plan?.profile_bundle;
    const scapSource = record.plan?.scap_source;
    this.text("compliance-plan-summary", [record.plan?.changes || "Preparing plan…", scapSource ? `${scapSource.publisher} · SCAP Security Guide ${scapSource.release}` : "", project ? `${project.name} · version ${project.version || "unknown"} · ${project.dependencies.length} bundled ${project.dependencies.length === 1 ? "dependency" : "dependencies"}` : ""].filter(Boolean).join(" · "));
    const evidence = {};
    for (const phase of ["before", "after", "remediation"]) {
      if (record[phase]) evidence[phase] = Object.fromEntries(Object.entries(record[phase]).filter(([key]) => key.endsWith("artifact") || key === "artifact_directory" || key === "profile_sha256"));
    }
    this.text("compliance-plan-evidence", JSON.stringify({ plan: record.plan, evidence }, null, 2));
    const latestPhase = this.assessmentPhase(record);
    const report = record.request.action === "assess" ? record[latestPhase] : record.after || record.before;
    const counts = report?.counts;
    if (counts && this.defaultFilterRunId !== record.id) {
      this.el("compliance-control-filter").value = counts.failed ? "failed" : counts.error ? "error" : "all";
      this.defaultFilterRunId = record.id;
    }
    this.el("compliance-metrics").hidden = !counts;
    this.el("compliance-metrics").replaceChildren(...(counts ? [
      ["Evaluated", (counts.passed || 0) + (counts.failed || 0), ""],
      ["Passed", counts.passed, "passed"], ["Failed", counts.failed, "failed"], ["Errors", counts.error, "error"],
    ].map(([label, value, status]) => {
      const metric = this.node(status ? "button" : "div", `compliance-metric ${status ? "compliance-metric-" + status : ""}`, "");
      if (status) {
        metric.type = "button";
        metric.dataset.outcome = status;
        metric.setAttribute("aria-label", `Show ${status === "error" ? "errored" : status} checks`);
        metric.addEventListener("click", () => {
          this.el("compliance-control-search").value = "";
          this.el("compliance-control-impact").value = "all";
          this.el("compliance-control-filter").value = status;
          this.renderControlRows();
        });
      }
      metric.append(this.node("strong", "", String(value ?? 0)), this.node("span", "", label));
      return metric;
    }) : []));
    const excluded = report?.xccdf_counts;
    this.text("compliance-coverage-note", excluded
      ? `${excluded.notapplicable || 0} not applicable · ${excluded.notselected || 0} outside this profile${report.has_inconclusive_results ? " · Some results are inconclusive" : ""}`
      : counts ? Object.entries(counts).filter(([key, value]) => !["passed", "failed", "error"].includes(key) && value).map(([key, value]) => `${value} ${key.replaceAll("_", " ")}`).join(" · ") : "");
    this.renderControlRows(this.controlsPage);
    this.text("compliance-controls-notice", counts ? (report.controls_truncated && !this.hasControlEvidence() ? "Only the saved summary is available here. Complete results require the original evidence." : "") : record.controls?.length ? "Showing the saved summary." : "No verified assessment evidence received yet.");
    const actions = [];
    for (const phase of ["before", "after", "remediation"]) {
      if (!record[phase]) continue;
      const button = this.node("button", "inventory-button", `${phase === "remediation" ? "Remediation log" : record.request.action === "assess" ? (latestPhase === "after" ? `${phase === "after" ? "Latest" : "Previous"} evidence` : "Assessment evidence") : `${phase} evidence`}`);
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

  assessmentPhase(record) {
    return record?.after || record?.request.assessment_of ? "after" : "before";
  }

  hasControlEvidence() {
    return [this.savedRun?.before, this.savedRun?.after].some(phase => phase?.report_artifact);
  }

  controlQueryKey(query) {
    return JSON.stringify([this.inventory.contextId, this.savedRun?.id, this.savedRun?.before?.report_sha256, this.savedRun?.after?.report_sha256, query]);
  }

  controlOutcome(control, phase) {
    return control[phase + "_xccdf_result"] === "notselected" ? "outside_profile" : control[phase] || "unassessed";
  }

  outcomeCell(status = "unassessed") {
    const cell = this.node("td", `compliance-outcome-${status}`, "");
    cell.append(this.node("span", "compliance-status-badge", status.replaceAll("_", " ")));
    return cell;
  }

  impactBand(impact) {
    if (impact == null) return "unknown";
    return impact >= 0.7 ? "high" : impact >= 0.4 ? "medium" : "low";
  }

  impactCell(impact) {
    const band = this.impactBand(impact);
    const label = band[0].toUpperCase() + band.slice(1);
    return this.node("td", "compliance-impact", impact == null ? label : `${label} · ${impact}`);
  }

  requestControls() {
    if (this.controlsRequest || !this.inventory.connected || !this.hasControlEvidence() || !this.controlsQuery) return;
    if (this.exportRequest || this.controlRequest) {
      this.controlsDebounce = setTimeout(() => this.requestControls(), 200);
      return;
    }
    const request = { id: crypto.randomUUID(), key: this.controlsKey };
    this.controlsRequest = request;
    const sent = this.inventory.send({ type: "compliance.controls", context_id: this.inventory.contextId,
      run_id: this.savedRun.id, request_id: request.id, query: this.controlsQuery });
    const failed = () => {
      if (this.controlsRequest !== request) return;
      this.controlsRequest = null;
      if (request.key === this.controlsKey) this.controlsResult = { detail: "Checks could not be loaded. Retry when connected." };
      this.renderControlRows(this.controlsPage);
      this.setState();
    };
    if (!sent) failed();
    else this.controlsTimer = setTimeout(failed, 30000);
    this.setState();
  }

  renderControlRows(page = 1) {
    const search = this.el("compliance-control-search").value.trim().toLowerCase();
    const filter = this.el("compliance-control-filter").value;
    const impact = this.el("compliance-control-impact").value;
    const sort = this.el("compliance-control-sort").value;
    const complete = this.hasControlEvidence();
    const query = { search, outcome: filter, impact, sort, page, page_size: Number(this.el("compliance-page-size").value) };
    const key = this.controlQueryKey(query);
    if (complete && key !== this.controlsKey) {
      this.controlsKey = key;
      this.controlsRetryDeadline = Date.now() + 15000;
      this.controlsResult = null;
      this.controlsPage = page;
      this.controlsQuery = query;
      clearTimeout(this.controlsDebounce);
      this.controlsDebounce = setTimeout(() => this.requestControls(), 200);
    } else if (complete && !this.controlsResult && !this.controlsRequest) {
      clearTimeout(this.controlsDebounce);
      this.controlsDebounce = setTimeout(() => this.requestControls(), 200);
    }
    const result = complete ? this.controlsResult || (!this.inventory.connected ? { detail: "Connection lost. Reconnect and retry loading checks." } : null) : null;
    const controls = complete ? result?.controls || [] : this.savedRun?.controls || [];
    const assessmentOnly = this.savedRun?.request.action === "assess";
    const assessmentPhase = this.assessmentPhase(this.savedRun);
    this.el("compliance-impact-heading").hidden = false;
    this.el("compliance-before-heading").textContent = assessmentOnly ? "Result" : "Before";
    this.el("compliance-after-heading").hidden = !!assessmentOnly;
    const visible = complete ? controls : controls.filter(control => {
      const phase = !assessmentOnly && control.after && control.after !== "unassessed" ? "after"
        : assessmentOnly ? assessmentPhase : "before";
      const latest = this.controlOutcome(control, phase);
      return (impact === "all" || this.impactBand(control.impact) === impact) &&
        (filter === "all" || (filter === "unresolved" ? !["passed", "outside_profile"].includes(latest) : latest === filter)) &&
        [control.control_id, control.title, control.profile].join(" ").toLowerCase().includes(search);
    });
    if (!complete && sort !== "priority") visible.sort((a, b) => {
      if (sort.startsWith("impact_")) return (a.impact == null) - (b.impact == null) || ((a.impact || 0) - (b.impact || 0)) * (sort === "impact_desc" ? -1 : 1);
      return String(a[sort] || a.control_id).localeCompare(String(b[sort] || b.control_id));
    });
    this.el("compliance-controls").replaceChildren(...visible.map(control => {
      const row = this.node("tr", "", "");
      const title = this.node("td", "", "");
      const button = this.node("button", "inventory-link compliance-control-link", control.title || control.control_id);
      button.dataset.controlId = control.control_id;
      button.type = "button";
      button.disabled = !this.inventory.connected;
      button.setAttribute("aria-haspopup", "dialog");
      button.addEventListener("click", () => this.openControl(control));
      title.append(button);
      if (complete) title.append(this.node("span", "compliance-control-identity", `${control.control_id} · ${control.profile}`));
      row.append(title);
      row.append(this.impactCell(control.impact));
      if (assessmentOnly) {
        const latest = this.controlOutcome(control, assessmentPhase);
        row.append(this.outcomeCell(latest));
      } else {
        for (const phase of ["before", "after"]) row.append(this.outcomeCell(this.controlOutcome(control, phase)));
      }
      return row;
    }));
    this.text("compliance-filter-notice", visible.length ? `${visible.length} of ${controls.length} loaded controls shown. Select a control to review its evidence.` : "No loaded controls match these filters.");
    this.el("compliance-controls-clear").hidden = !search && filter === "all" && impact === "all";
    this.el("compliance-pagination").hidden = !complete;
    this.el("compliance-controls-retry").hidden = !result?.detail;
    this.el("compliance-controls-retry").disabled = !this.inventory.connected;
    this.el("compliance-run-review").setAttribute("aria-busy", String(complete && !result));
    if (complete) {
      const loaded = !!result && !result.detail;
      const matched = result?.matched || 0;
      const start = matched ? (result.page - 1) * result.page_size + 1 : 0;
      this.text("compliance-filter-notice", result?.detail || (!loaded ? "Searching complete saved evidence…" : matched ? "Select a check to review its saved evidence." : "No checks match these filters."));
      this.text("compliance-page-summary", loaded ? `${start}–${matched ? start + controls.length - 1 : 0} of ${matched} matching checks · ${result.total} total` : "");
      this.text("compliance-page-number", loaded ? `Page ${result.page} of ${result.pages}` : "");
      this.el("compliance-page-previous").disabled = !loaded || result.page <= 1 || !this.inventory.connected;
      this.el("compliance-page-next").disabled = !loaded || result.page >= result.pages || !this.inventory.connected;
    }
    for (const metric of this.el("compliance-metrics").querySelectorAll("[data-outcome]")) metric.setAttribute("aria-pressed", String(metric.dataset.outcome === filter));
  }

  closeReviewRequests() {
    this.controlsRequest = null;
    this.controlsKey = null;
    this.controlsRetryDeadline = 0;
    this.controlsResult = null;
    this.controlsPage = 1;
    clearTimeout(this.controlsTimer);
    clearTimeout(this.controlsDebounce);
    this.el("compliance-page-previous").disabled = true;
    this.el("compliance-page-next").disabled = true;
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
    const assessmentOnly = this.savedRun?.request.action === "assess";
    const sections = assessmentOnly ? [] : [mapping];
    const current = this.assessmentPhase(this.savedRun);
    const phases = assessmentOnly ? [current] : ["before", "after"];
    if (assessmentOnly && current === "after" && this.savedRun.before) phases.push("before");
    for (const phase of phases) {
      const data = control[phase];
      const section = this.node("section", "compliance-control-phase", "");
      const label = assessmentOnly ? (phase === current ? "Result" : "Previous") : phase === "before" ? "Before" : "After";
      section.append(this.node("h4", "", `${label}: ${data.status.replaceAll("_", " ")}`));
      if (data.description) section.append(this.node("p", "", data.description));
      if (data.rationale) section.append(this.node("p", "", `Rationale: ${data.rationale}`));
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

  savedAssetMatches(record) {
    const asset = this.inventory.entries.get(record.request.resource_id);
    return ["ip", "domain"].includes(asset?.entry.kind) && asset.entry.value === record.request.scan.host;
  }

  restoreSettings(record, assess = false) {
    if (!record || !this.inventory.canWrite()) return;
    const request = record.request;
    if (!this.savedAssetMatches(record)) {
      this.text("compliance-status", "The saved host is unavailable. Refresh inventory to continue.");
      return;
    }
    this.projects.deselect();
    this.selectResource(request.resource_id, false);
    this.baselineId = request.custom_profile?.id || request.scap_preset_id || (request.scan.openscap ? "openscap" : request.baseline_id || "custom");
    this.renderBaselines();
    this.renderSelection();
    for (const [key, value] of Object.entries(request.scan)) {
      const field = this.field(key);
      if (!field) continue;
      if (field.type === "checkbox") field.checked = value;
      else field.value = value || "";
    }
    this.resetScap();
    if (request.scan.openscap && !request.scap_preset_id) {
      const { profile_id: id, sha256, distribution, version } = request.scan.openscap;
      this.field("scap_datastream").value = request.scan.profile;
      this.field("scap_sha256").value = sha256;
      this.setScapProfiles([{ id }], id);
      this.field("scap_distribution").value = distribution;
      this.field("scap_version").value = version;
      this.text("compliance-scap-status", "Using the saved digest and profile. Inspect again to load the current profiles.");
    }
    this.field("bootstrap_python").checked = request.bootstrap_python ?? false;
    if (request.remediation) {
      for (const [field, key] of [["automation_profile", "profile"], ["playbook", "playbook"], ["host_alias", "limit"]]) this.field(field).value = request.remediation[key] || "";
      this.field("vars_file").value = request.remediation.vars_files?.[0] || "";
    }
    this.accessDirty = true;
    this.el("compliance-setup").dataset.record = record.id;
    this.el("compliance-setup").open = !assess;
    this.el("compliance-advanced").open = !assess;
    this.connectionSummary();
    this.setState();
    if (assess) {
      if (request.custom_profile) this.launch({ ...request, assessment_of: record.id }, record.label);
      else {
        this.assessmentOf = record.id;
        this.start("assess");
      }
    }
    return true;
  }

  disconnected() {
    this.fixes.reset();
    const detail = "Connection lost. Resume the engagement to review saved results.";
    if (this.run && this.run.action !== "assess") this.finishWorkflowCard(this.run.card, detail);
    this.finishAssessmentCard(detail);
    this.recommendation = null;
    this.pendingAssessment = null;
    this.ansibleAvailable = null;
    if (this.scapRequest?.type === "compliance.ansible.check") {
      clearTimeout(this.scapRequest.timer);
      this.scapRequest = null;
    } else if (this.scapRequest) {
      this.resetScap();
      this.text("compliance-scap-status", "Connection lost. Reconnect to inspect the datastream.");
    }
    for (const request of this.projects.requests.values()) clearTimeout(request.timer);
    this.projects.requests.clear();
    this.projects.preparation = null;
    this.projects.render();
    this.closeReviewRequests();
    if (this.hasControlEvidence()) {
      this.text("compliance-filter-notice", "Connection lost. Reconnect to load complete checks.");
      this.el("compliance-run-review").setAttribute("aria-busy", "false");
    }
    if (!this.workflowId) return;
    this.text("compliance-status", "Connection lost. Resume the engagement to review the saved evidence.");
    if (this.run) this.result("Workflow connection lost", "The final outcome is unknown. Review saved evidence before retrying changes.");
    this.run = null;
    this.workflowId = null;
  }

}
