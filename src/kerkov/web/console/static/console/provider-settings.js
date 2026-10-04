/* Credentials live only in the password field and the outgoing socket message. */
class ProviderSettings {
  constructor(panel) {
    this.panel = panel;
    this.opened = false;
    this.providers = [];
    this.defaultSelection = null;
    this.settingsRequest = null;
    this.settingsFailed = false;
    this.pending = null;
    this.reconfigured = new Set();
    this.notice = "";
    this.error = "";
    this.el("provider-manage").addEventListener("click", () => this.open());
    this.el("provider-settings-back").addEventListener("click", () => this.close());
    this.el("provider-settings-select").addEventListener("change", () => this.selectProvider());
    this.el("provider-api-key").addEventListener("input", () => this.render());
    this.el("provider-base-url").addEventListener("input", () => this.render());
    this.el("provider-test").addEventListener("click", () => this.submit("test"));
    this.el("provider-save").addEventListener("click", () => this.submit("configure"));
    this.el("provider-default").addEventListener("click", () => this.saveDefault());
    this.el("provider-settings-retry").addEventListener("click", () => this.load());
  }

  el(id) { return document.getElementById(id); }
  text(id, text) { this.el(id).textContent = text || ""; }
  get saving() { return ["configure", "default"].includes(this.pending?.operation); }
  get selected() { return this.providers.find((item) => item.id === this.el("provider-settings-select").value); }
  get locked() { return !this.panel.connected || !!this.panel.blockedReason || this.panel.switching; }

  open() {
    if (this.locked || this.saving) return;
    this.opened = true;
    this.notice = "";
    this.error = "";
    this.render();
    this.load();
  }

  close() {
    this.clearSecret();
    this.opened = false;
    clearTimeout(this.settingsTimer);
    this.settingsRequest = null;
    if (this.pending?.operation === "test") {
      clearTimeout(this.operationTimer);
      this.pending = null;
    }
    this.render();
    if (this.panel.el("provider-dialog").open) this.el("provider-manage").focus();
  }

  clearSecret() { this.el("provider-api-key").value = ""; }

  disconnect() {
    this.clearSecret();
    clearTimeout(this.settingsTimer);
    clearTimeout(this.operationTimer);
    this.settingsRequest = null;
    if (this.pending) this.error = "Disconnected before confirmation. Reconnect to check the saved setup.";
    this.pending = null;
    this.providers = [];
    this.reconfigured.clear();
  }

  load() {
    if (this.locked || this.pending || this.settingsRequest) return;
    const id = this.panel.requestId();
    this.settingsRequest = id;
    this.settingsFailed = false;
    this.error = "";
    if (!this.panel.send({ type: "provider.settings", request_id: id })) {
      this.settingsRequest = null;
      this.settingsFailed = true;
      this.error = "Unable to load provider settings. Try again when connected.";
    } else {
      this.settingsTimer = window.setTimeout(() => {
        if (this.settingsRequest !== id) return;
        this.settingsRequest = null;
        this.settingsFailed = true;
        this.error = "Provider settings did not arrive. Try again.";
        this.render();
      }, 25000);
    }
    this.render();
  }

  selectProvider() {
    this.clearSecret();
    this.notice = "";
    this.error = "";
    this.el("provider-persist").checked = false;
    this.el("provider-base-url").value = this.selected?.base_url || "http://localhost:11434";
    this.render();
  }

  canSubmit() {
    const provider = this.selected;
    if (this.locked || this.pending || this.settingsRequest || !provider) return false;
    if (provider.auth_kind === "oauth") return provider.configured;
    if (provider.auth_kind === "local") return !!this.el("provider-base-url").value.trim();
    return provider.configured || !!this.el("provider-api-key").value.trim();
  }

  submit(operation) {
    if (!this.canSubmit() || (operation === "configure" && this.selected.auth_kind === "oauth")) return;
    const provider = this.selected;
    const request = { type: `provider.${operation}`, request_id: this.panel.requestId(), provider: provider.id };
    const apiKey = this.el("provider-api-key").value.trim();
    if (provider.auth_kind === "api_key" && apiKey) request.api_key = apiKey;
    if (provider.auth_kind === "local") request.base_url = this.el("provider-base-url").value.trim();
    if (operation === "configure") request.persist = this.el("provider-persist").checked;
    this.startRequest(operation, request);
  }

  canDefault() {
    const provider = this.panel.el("provider-select").value;
    return !this.locked && !this.pending && !this.panel.catalogRequest && !this.panel.modelsRequest
      && this.panel.modelProvider === provider && this.panel.models.includes(this.panel.selectedModel);
  }

  saveDefault() {
    if (!this.canDefault()) return;
    this.startRequest("default", {
      type: "provider.default", request_id: this.panel.requestId(),
      provider: this.panel.el("provider-select").value, model: this.panel.selectedModel,
    });
  }

  startRequest(operation, request) {
    // Correlation state deliberately excludes credentials and endpoints.
    this.pending = { id: request.request_id, provider: request.provider, operation };
    this.notice = "";
    this.error = "";
    this.panel.resultText = "";
    this.panel.onPendingChange();
    if (!this.panel.send(request)) {
      this.pending = null;
      this.error = "Unable to send the request. Try again when connected.";
      this.panel.onPendingChange();
      this.render();
      return;
    }
    const requestId = request.request_id;
    this.operationTimer = window.setTimeout(() => {
      if (this.pending?.id !== requestId) return;
      if (this.pending.operation === "test") {
        this.pending = null;
        this.error = "The connection test timed out. Try again.";
      } else {
        this.notice = "Still waiting for confirmation…";
      }
      this.render();
    }, 25000);
    this.render();
  }

  handleEvent(type, payload) {
    if (type === "provider.settings") {
      if (!this.opened || !this.panel.el("provider-dialog").open) return true;
      if (!this.settingsRequest || payload.request_id !== this.settingsRequest) return true;
      clearTimeout(this.settingsTimer);
      this.settingsRequest = null;
      this.settingsFailed = false;
      this.providers = Array.isArray(payload.providers) ? payload.providers : [];
      this.defaultSelection = payload.default || null;
      const current = this.el("provider-settings-select").value || this.panel.el("provider-select").value;
      this.el("provider-settings-select").replaceChildren();
      for (const provider of this.providers) {
        const option = document.createElement("option");
        option.value = provider.id;
        option.textContent = `${provider.label}${provider.configured ? " · Configured" : " · Setup needed"}`;
        this.el("provider-settings-select").append(option);
      }
      this.el("provider-settings-select").value = this.providers.some((item) => item.id === current) ? current : this.providers[0]?.id || "";
      this.selectProvider();
      this.notice = payload.warning || "";
      this.render();
      if (this.opened && !this.locked) this.el("provider-settings-select").focus();
      return true;
    }
    if (type === "provider.selection_error" && payload.operation === "settings") {
      if (this.settingsRequest && payload.request_id === this.settingsRequest) {
        clearTimeout(this.settingsTimer);
        this.settingsRequest = null;
        this.settingsFailed = true;
        this.error = payload.detail || "Unable to load provider settings.";
        this.render();
      }
      return true;
    }
    const operation = { "provider.tested": "test", "provider.configured": "configure", "provider.default_saved": "default" }[type]
      || (type === "provider.selection_error" ? payload.operation : null);
    if (!["test", "configure", "default"].includes(operation)) return false;
    if (!this.pending || payload.request_id !== this.pending.id || operation !== this.pending.operation
      || (type !== "provider.selection_error" && operation !== "default" && payload.provider !== this.pending.provider)) return true;
    clearTimeout(this.operationTimer);
    this.pending = null;
    if (type === "provider.selection_error") {
      this.error = payload.detail || "The request failed. Try again.";
    } else {
      if (operation !== "test") this.clearSecret();
      if (operation === "test") {
        this.notice = `Connection verified${Array.isArray(payload.models) ? ` · ${payload.models.length} models available` : ""}.`
          + (this.selected?.auth_kind === "oauth" ? "" : " Choose Save setup to keep these settings.");
      } else if (operation === "configure") {
        this.reconfigured.add(payload.provider);
        const provider = this.providers.find((item) => item.id === payload.provider);
        if (provider) {
          provider.configured = true;
          if (provider.auth_kind === "local") {
            provider.base_url = payload.base_url || this.el("provider-base-url").value.trim();
            this.el("provider-base-url").value = provider.base_url;
          }
          const option = Array.from(this.el("provider-settings-select").options).find((item) => item.value === provider.id);
          if (option) option.textContent = `${provider.label} · Configured`;
        }
        this.notice = payload.persisted ? "Setup saved for future sessions. Choose a model to use it here."
          : "Setup saved for this session. Choose a model to use it here.";
        this.panel.resultText = "Provider setup saved";
        this.panel.loadCatalog();
      } else {
        this.defaultSelection = payload.default;
        this.notice = "Default saved for future sessions.";
        this.panel.resultText = this.notice;
      }
      if (payload.warning) this.notice += ` ${payload.warning}`;
    }
    this.panel.onPendingChange();
    this.render();
    return true;
  }

  render() {
    const provider = this.selected;
    const locked = this.locked || !!this.pending || !!this.settingsRequest;
    this.el("provider-picker-view").hidden = this.opened;
    this.el("provider-settings-view").hidden = !this.opened;
    this.text("provider-heading", this.opened ? "Manage providers" : "Choose your engine");
    this.text("provider-description", this.opened ? "Connect a provider, test access, and choose whether to save its setup for future sessions." : "Use model changes this session. Save as default sets the model for future sessions.");
    this.el("provider-manage").disabled = this.locked || this.saving;
    this.el("provider-settings-back").disabled = this.saving;
    this.el("provider-settings-select").disabled = locked || !this.providers.length;
    this.el("provider-key-fields").hidden = provider?.auth_kind !== "api_key";
    this.el("provider-local-fields").hidden = provider?.auth_kind !== "local";
    this.el("provider-oauth-guidance").hidden = provider?.auth_kind !== "oauth";
    this.el("provider-persist-row").hidden = !provider || provider.auth_kind === "oauth";
    this.text("provider-key-help", provider?.configured ? "Leave blank to keep the existing key. Keys are never displayed." : "Enter the API key for this provider.");
    for (const id of ["provider-api-key", "provider-base-url", "provider-persist"]) this.el(id).disabled = locked;
    this.el("provider-test").disabled = !this.canSubmit();
    this.el("provider-save").disabled = !this.canSubmit() || provider?.auth_kind === "oauth";
    this.el("provider-save").hidden = provider?.auth_kind === "oauth";
    this.text("provider-test", this.pending?.operation === "test" ? "Testing…" : "Test connection");
    this.text("provider-save", this.pending?.operation === "configure" ? "Saving…" : "Save setup");
    this.el("provider-default").disabled = !this.canDefault();
    this.text("provider-default", this.pending?.operation === "default" ? "Saving…" : "Save as default");
    const selection = this.defaultSelection;
    this.text("provider-default-summary", selection?.provider && selection?.model ? `Default: ${this.panel.label(selection.provider)} / ${selection.model}` : "Save a default model from the model picker.");
    this.text("provider-settings-status", this.settingsRequest ? "Loading provider settings…" : this.panel.blockedReason || this.notice);
    this.text("provider-settings-error", this.error);
    this.el("provider-settings-error").hidden = !this.error;
    this.el("provider-settings-retry").hidden = !this.settingsFailed || !this.error;
    this.el("provider-settings-retry").disabled = locked;
    this.text("provider-default-status", this.pending?.operation === "default" ? "Saving default…" : this.opened ? "" : this.error || this.notice);
  }
}
