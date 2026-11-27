/* Store no-code assessment checks in the local profile picker. */
const COMPLIANCE_CHECK_TYPES = {
  file_exists: ["File exists", "/etc/ssh/sshd_config"],
  file_mode: ["File permissions", "/etc/passwd", "Octal mode", "0644"],
  file_owner: ["File owner", "/etc/passwd", "Owner", "root"],
  package_installed: ["Package installed", "auditd"],
  package_absent: ["Package absent", "telnet"],
  service_enabled: ["Service enabled", "auditd"],
  service_disabled: ["Service disabled", "cups"],
};

class ComplianceProfileBuilder {
  constructor(host) {
    this.host = host;
    this.inventory = host.inventory;
    this.dialog = host.el("compliance-profile-builder");
    this.rows = host.el("compliance-builder-rows");
    this.entries = new Map();
    this.request = null;
    this.editing = null;
    host.el("compliance-new-profile").addEventListener("click", () => this.open());
    host.el("compliance-builder-add").addEventListener("click", () => this.addCheck());
    host.el("compliance-builder-cancel").addEventListener("click", () => this.dialog.close());
    host.el("compliance-builder-form").addEventListener("submit", event => {
      event.preventDefault();
      this.save();
    });
    this.dialog.addEventListener("cancel", event => {
      if (this.request) event.preventDefault();
    });
  }

  reset() {
    clearTimeout(this.request?.timer);
    this.request = null;
    this.dialog.close();
    this.replace([]);
  }

  replace(entries) {
    this.entries = new Map(entries.map(entry => [entry.id, entry]));
    this.render();
  }

  open(profile = null) {
    if (!this.inventory.canWrite() || this.request) return;
    this.editing = profile ? { id: profile.id, revision: profile.revision } : null;
    this.host.text("compliance-builder-title", profile ? "Edit profile" : "New profile");
    this.host.el("compliance-builder-name").value = profile?.name || "";
    this.host.text("compliance-builder-status", "");
    this.rows.replaceChildren();
    for (const check of profile?.checks || [{}]) this.addCheck(check);
    this.dialog.showModal();
    this.syncForm();
  }

  addCheck({ kind = "file_exists", target = "", expected = "" } = {}) {
    const node = (...args) => this.host.node(...args);
    const row = node("div", "compliance-builder-row", "");
    const kindLabel = node("label", "", "Check type");
    const type = document.createElement("select");
    for (const [id, [title]] of Object.entries(COMPLIANCE_CHECK_TYPES)) type.add(new Option(title, id));
    type.name = "kind";
    type.setAttribute("aria-label", "Check type");
    type.value = kind;
    kindLabel.append(type);
    const targetLabel = node("label", "", "");
    const targetText = node("span", "", "");
    const value = document.createElement("input");
    value.name = "target";
    value.required = true;
    value.maxLength = 255;
    value.value = target;
    targetLabel.append(targetText, value);
    const expectedLabel = node("label", "", "");
    const expectedText = node("span", "", "");
    const expectedValue = document.createElement("input");
    expectedValue.name = "expected";
    expectedValue.maxLength = 64;
    expectedValue.value = expected;
    expectedLabel.append(expectedText, expectedValue);
    const describe = () => {
      const [, example, label, exampleValue] = COMPLIANCE_CHECK_TYPES[type.value];
      targetText.textContent = type.value.startsWith("file_") ? "File path"
        : type.value.startsWith("package_") ? "Package name" : "Service name";
      value.placeholder = example;
      expectedLabel.hidden = !label;
      expectedText.textContent = label || "";
      expectedValue.required = !!label;
      expectedValue.placeholder = exampleValue || "";
    };
    describe();
    type.addEventListener("change", () => { expectedValue.value = ""; describe(); });
    const remove = node("button", "inventory-link compliance-builder-remove", "Remove check");
    remove.type = "button";
    remove.addEventListener("click", () => { row.remove(); this.syncForm(); });
    row.append(kindLabel, targetLabel, expectedLabel, remove);
    this.rows.append(row);
    this.syncForm();
  }

  syncForm() {
    const saving = !!this.request;
    const writable = this.inventory.canWrite();
    this.host.el("compliance-new-profile").disabled = !writable || saving;
    for (const control of this.dialog.querySelectorAll("input, select, button")) control.disabled = saving;
    this.host.el("compliance-builder-save").disabled = saving || !writable;
    this.host.el("compliance-builder-add").disabled = saving || this.rows.childElementCount >= 30;
    for (const button of this.rows.querySelectorAll(".compliance-builder-remove")) {
      button.disabled = saving || this.rows.childElementCount === 1;
    }
  }

  save() {
    if (!this.inventory.canWrite() || this.request) return;
    const checks = Array.from(this.rows.children, row => {
      const kind = row.querySelector('[name="kind"]').value;
      const target = row.querySelector('[name="target"]').value;
      const expected = COMPLIANCE_CHECK_TYPES[kind][2] ? row.querySelector('[name="expected"]').value : "";
      return { kind, target, expected };
    });
    const draft = { profile: { name: this.host.el("compliance-builder-name").value, checks }, reference: this.editing };
    const id = this.inventory.requestId();
    const context = this.inventory.contextId;
    if (!this.inventory.send({ type: "compliance.custom.save", request_id: id, context_id: context, draft })) {
      this.host.text("compliance-builder-status", "Could not reach the console. Check the connection and retry.");
      return;
    }
    this.request = { id, context, timer: setTimeout(() => {
      this.request = null;
      this.host.text("compliance-builder-status", "Save response timed out. Check My profiles before retrying.");
      this.host.projects.request("compliance.projects");
      this.syncForm();
    }, 15000) };
    this.host.text("compliance-builder-status", "Saving profile…");
    this.syncForm();
  }

  handle(payload) {
    if (!this.request || payload.request_id !== this.request.id || payload.context_id !== this.request.context ||
        this.inventory.contextId !== this.request.context) return;
    clearTimeout(this.request.timer);
    this.request = null;
    this.syncForm();
    if (payload.profile) {
      this.entries.set(payload.profile.id, payload.profile);
      this.dialog.close();
      this.host.el("compliance-profile-search").value = "";
      this.host.el("compliance-profile-source").value = "custom";
      this.host.selectCustomProfile(payload.profile.id);
    } else {
      this.host.text("compliance-builder-status", payload.detail || "Could not save this profile.");
      if (this.editing) this.host.projects.request("compliance.projects");
    }
  }

  render() {
    const node = (...args) => this.host.node(...args);
    const entries = [...this.entries.values()].sort((a, b) => a.name.localeCompare(b.name));
    this.host.el("compliance-custom-profiles").replaceChildren(...entries.map(profile => {
      const card = node("div", "compliance-custom-card", "");
      card.dataset.profileSource = "custom";
      const choose = node("button", "compliance-baseline", "");
      choose.type = "button";
      choose.dataset.customProfile = profile.id;
      choose.setAttribute("aria-pressed", String(this.host.baselineId === profile.id));
      choose.append(node("strong", "", profile.name), node("span", "", `${profile.checks.length} Linux ${profile.checks.length === 1 ? "check" : "checks"}`));
      choose.disabled = !this.inventory.canWrite();
      choose.addEventListener("click", () => this.host.selectCustomProfile(profile.id));
      const edit = node("button", "inventory-link", "Edit");
      edit.type = "button";
      edit.setAttribute("aria-label", "Edit " + profile.name);
      edit.disabled = choose.disabled;
      edit.addEventListener("click", () => this.open(profile));
      card.append(choose, edit);
      return card;
    }));
    this.host.filterProfiles();
    this.syncForm();
  }
}
