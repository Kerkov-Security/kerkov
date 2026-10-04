/* A selection is only active after the server confirms the matching request. */
class ProviderPanel {
  constructor({ send, requestId, onPendingChange, onChanged }) {
    this.send = send;
    this.requestId = requestId;
    this.onPendingChange = onPendingChange;
    this.onChanged = onChanged;
    this.connected = false;
    this.blockedReason = "Connecting to the session.";
    this.active = null;
    this.providerReady = false;
    this.providers = null;
    this.models = [];
    this.modelProvider = null;
    this.selectedModel = "";
    this.catalogRequest = null;
    this.modelsRequest = null;
    this.pendingSwitch = null;
    this.errorOperation = null;
    this.resultText = "";
    this.elements = {};
    for (const id of [
      "provider-picker", "provider-picker-label", "provider-picker-status", "provider-dialog",
      "provider-close", "provider-select", "provider-model-search", "provider-model-count", "provider-model-refresh",
      "provider-model-list", "provider-status", "provider-error", "provider-retry",
      "provider-selection", "provider-apply",
    ]) this.elements[id] = document.getElementById(id);
    this.el("provider-picker").addEventListener("click", () => this.open());
    this.el("provider-close").addEventListener("click", () => {
      this.settings.close();
      this.el("provider-dialog").close();
    });
    this.el("provider-dialog").addEventListener("cancel", () => this.settings.close());
    this.el("provider-dialog").addEventListener("close", () => {
      this.settings.close();
      // Dismissing the dialog never cancels an in-flight switch or releases its gate.
      const target = this.el("provider-picker").disabled ? document.getElementById("message-stream") : this.el("provider-picker");
      target.focus({ preventScroll: true });
    });
    this.el("provider-select").addEventListener("change", () => this.loadModels());
    this.el("provider-model-refresh").addEventListener("click", () => this.loadModels());
    this.el("provider-model-search").addEventListener("input", () => this.renderModels());
    this.el("provider-retry").addEventListener("click", () => {
      if (this.errorOperation === "catalog") this.loadCatalog();
      else if (this.errorOperation === "models") this.loadModels();
      else if (this.errorOperation === "switch") this.apply();
    });
    this.el("provider-apply").addEventListener("click", () => this.apply());
    this.settings = new ProviderSettings(this);
    this.renderControls();
  }

  el(id) { return this.elements[id]; }

  setText(id, value) {
    const text = String(value ?? "");
    if (this.el(id).textContent !== text) this.el(id).textContent = text;
  }

  get switching() { return this.pendingSwitch !== null; }
  get saving() { return !!this.settings?.saving; }

  label(provider) {
    return this.providers?.find((item) => item.id === provider)?.label || {
      anthropic: "Anthropic", gemini: "Gemini", nvidia: "NVIDIA", openrouter: "OpenRouter",
      deepseek: "DeepSeek", ollama: "Ollama", codex: "Codex",
    }[provider] || provider;
  }

  setInitial(provider, ready) {
    this.providerReady = !!ready;
    this.active = provider?.model ? {
      provider: String(provider.provider || "").replace(/Provider$/, "").toLowerCase(),
      model: provider.model,
    } : null;
    this.renderControls();
  }

  setState({ connected, busy, saving, activeAgents }) {
    if (!connected && this.connected) {
      this.settings.disconnect();
      clearTimeout(this.catalogTimer);
      clearTimeout(this.modelsTimer);
      clearTimeout(this.switchTimer);
      this.catalogRequest = null;
      this.modelsRequest = null;
      if (this.switching) {
        this.pendingSwitch = null;
        this.showError("Disconnected before the switch was confirmed. Reopen the console to check the active model.", null);
      }
    }
    this.connected = connected;
    this.blockedReason = !connected ? "Offline · last confirmed model" : busy ? "Available after this turn finishes."
      : saving ? "Available after the resource is saved." : activeAgents ? "Available after all agents finish." : "";
    this.renderControls();
  }

  open() {
    if (!this.connected || this.blockedReason || this.switching || this.saving) return;
    this.resultText = "";
    this.el("provider-dialog").showModal();
    if (!this.providers) this.loadCatalog();
    else if (this.modelProvider !== this.el("provider-select").value && !this.modelsRequest) this.loadModels();
    this.renderControls();
    if (!this.el("provider-select").disabled) this.el("provider-select").focus();
  }

  clearError() {
    this.errorOperation = null;
    this.setText("provider-error", "");
    this.el("provider-error").hidden = true;
  }

  showError(detail, operation) {
    this.errorOperation = operation;
    this.setText("provider-error", detail);
    this.el("provider-error").hidden = false;
  }

  loadCatalog() {
    if (!this.connected || this.catalogRequest || this.switching) return;
    clearTimeout(this.modelsTimer);
    this.modelsRequest = null;
    this.models = [];
    this.modelProvider = null;
    this.selectedModel = "";
    this.clearError();
    const id = this.requestId();
    this.catalogRequest = id;
    this.renderModels();
    if (!this.send({ type: "provider.catalog", request_id: id })) {
      this.catalogRequest = null;
      this.showError("Unable to load providers. Try again when connected.", "catalog");
      this.renderControls();
      return;
    }
    this.catalogTimer = window.setTimeout(() => {
      if (this.catalogRequest !== id) return;
      this.catalogRequest = null;
      this.showError("The provider list did not arrive. Try again.", "catalog");
      this.renderControls();
    }, 25000);
  }

  loadModels() {
    const provider = this.el("provider-select").value;
    if (!this.connected || this.switching || !this.providers?.some((item) => item.id === provider && item.configured)) return;
    clearTimeout(this.modelsTimer);
    this.clearError();
    this.models = [];
    this.modelProvider = null;
    this.selectedModel = "";
    this.el("provider-model-search").value = "";
    const id = this.requestId();
    this.modelsRequest = { id, provider };
    this.renderModels();
    if (!this.send({ type: "provider.models", request_id: id, provider })) {
      this.modelsRequest = null;
      this.showError("Unable to load models. Try again when connected.", "models");
      this.renderControls();
      return;
    }
    this.modelsTimer = window.setTimeout(() => {
      if (this.modelsRequest?.id !== id) return;
      this.modelsRequest = null;
      this.showError("The model list did not arrive. Your current model is unchanged. Try again.", "models");
      this.renderControls();
    }, 25000);
  }

  canApply() {
    const provider = this.el("provider-select").value;
    return this.connected && !this.blockedReason && !this.switching && !this.saving && !this.catalogRequest && !this.modelsRequest
      && this.modelProvider === provider && this.models.includes(this.selectedModel)
      && !(this.providerReady && this.active?.provider === provider && this.active?.model === this.selectedModel
        && !this.settings.reconfigured.has(provider));
  }

  apply() {
    if (!this.canApply()) return;
    this.clearError();
    const id = this.requestId();
    const provider = this.el("provider-select").value;
    this.pendingSwitch = { id, provider, model: this.selectedModel };
    this.resultText = "";
    this.onPendingChange();
    if (!this.send({ type: "provider.switch", request_id: id, provider, model: this.selectedModel })) {
      this.pendingSwitch = null;
      this.showError("Unable to send the switch request. Your current model is unchanged.", "switch");
      this.onPendingChange();
      return;
    }
    this.switchTimer = window.setTimeout(() => {
      if (this.pendingSwitch?.id !== id) return;
      // The server may still be validating. Keep writes gated until its response or disconnect.
      this.setText("provider-status", "Still checking this model. Waiting for confirmation…");
    }, 25000);
  }

  handleEvent(type, payload) {
    if (this.settings.handleEvent(type, payload)) return;
    if (type === "provider.catalog") {
      if (!this.catalogRequest || payload.request_id !== this.catalogRequest) return;
      clearTimeout(this.catalogTimer);
      this.catalogRequest = null;
      this.providers = Array.isArray(payload.providers) ? payload.providers : [];
      this.active = payload.active || null;
      this.clearError();
      const selected = this.providers.find((item) => item.id === this.active?.provider && item.configured)
        || this.providers.find((item) => item.configured);
      this.el("provider-select").replaceChildren();
      if (!selected) {
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "No configured providers";
        this.el("provider-select").append(placeholder);
      }
      for (const provider of this.providers) {
        const option = document.createElement("option");
        option.value = provider.id;
        option.textContent = `${provider.label}${provider.configured ? "" : " — setup required"}`;
        option.disabled = !provider.configured;
        this.el("provider-select").append(option);
      }
      this.el("provider-select").value = selected?.id || "";
      this.renderControls();
      if (this.el("provider-dialog").open && selected) this.loadModels();
      return;
    }
    if (type === "provider.models") {
      if (!this.modelsRequest || payload.request_id !== this.modelsRequest.id
        || payload.provider !== this.modelsRequest.provider || payload.provider !== this.el("provider-select").value) return;
      clearTimeout(this.modelsTimer);
      this.modelsRequest = null;
      this.modelProvider = payload.provider;
      this.models = [...new Set((payload.models || []).filter((model) => typeof model === "string" && model))];
      const initial = this.active?.provider === payload.provider ? this.active.model : payload.selected_model;
      this.selectedModel = this.models.includes(initial) ? initial : this.models[0] || "";
      this.clearError();
      if (!this.models.length) this.showError("No models were returned for this provider. Your current model is unchanged. Try again.", "models");
      this.renderModels();
      return;
    }
    if (type === "provider.changed") {
      if (!this.pendingSwitch || payload.request_id !== this.pendingSwitch.id) return;
      clearTimeout(this.switchTimer);
      this.pendingSwitch = null;
      this.active = payload.active;
      this.settings.reconfigured.delete(this.active.provider);
      this.providerReady = !!payload.provider_ready;
      this.selectedModel = this.active.model;
      this.resultText = "Model updated";
      this.clearError();
      this.onChanged(payload);
      this.onPendingChange();
      this.renderModels();
      this.el("provider-dialog").close();
      return;
    }
    if (type === "provider.selection_error") {
      const operation = payload.operation;
      const expected = operation === "catalog" ? this.catalogRequest
        : operation === "models" ? this.modelsRequest?.id : operation === "switch" ? this.pendingSwitch?.id : null;
      if (!expected || payload.request_id !== expected) return;
      if (operation === "catalog") { clearTimeout(this.catalogTimer); this.catalogRequest = null; }
      if (operation === "models") { clearTimeout(this.modelsTimer); this.modelsRequest = null; }
      if (operation === "switch") {
        clearTimeout(this.switchTimer);
        this.pendingSwitch = null;
        this.resultText = "Switch failed · choose a model to retry";
      }
      this.showError(payload.detail || "The request failed. Try again.", operation);
      this.onPendingChange();
    }
  }

  renderModels() {
    const list = this.el("provider-model-list");
    list.replaceChildren();
    const query = this.el("provider-model-search").value.trim().toLowerCase();
    const models = this.models.filter((model) => model.toLowerCase().includes(query));
    for (const model of models) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "provider-model-option";
      button.dataset.model = model;
      button.setAttribute("aria-pressed", String(model === this.selectedModel));
      const name = document.createElement("span");
      name.textContent = model;
      button.append(name);
      if (this.providerReady && this.active?.provider === this.modelProvider && this.active?.model === model) {
        const active = document.createElement("span");
        active.className = "provider-model-active";
        active.textContent = "Active";
        button.append(active);
      }
      button.addEventListener("click", () => {
        if (!this.connected || this.blockedReason || this.switching || this.saving) return;
        this.selectedModel = model;
        if (this.errorOperation === "switch") this.clearError();
        for (const row of list.children) row.setAttribute("aria-pressed", String(row.dataset.model === model));
        this.renderControls();
      });
      list.append(button);
    }
    this.setText("provider-model-count", this.models.length ? `${models.length} of ${this.models.length}` : "");
    this.renderControls();
  }

  renderControls() {
    const locked = !this.connected || !!this.blockedReason || this.switching || this.saving;
    const activeLabel = this.active ? `${this.label(this.active.provider)} / ${this.active.model}` : "Choose provider & model";
    this.setText("provider-picker-label", activeLabel);
    this.el("provider-picker").disabled = locked;
    this.el("provider-picker").dataset.ready = String(this.providerReady && !!this.active);
    this.el("provider-picker").title = this.blockedReason || (this.switching ? "Switching model…" : `Choose provider and model · ${activeLabel}`);
    this.el("provider-picker").setAttribute("aria-label", `Choose provider and model${this.active ? `, current: ${activeLabel}` : ""}`);
    this.setText("provider-picker-status", this.switching ? "Switching…" : this.connected
      ? this.blockedReason || this.resultText || (this.active && !this.providerReady ? "Connection needed" : "") : this.active ? "Offline" : "");
    this.el("provider-select").disabled = locked || !!this.catalogRequest || !this.providers?.some((item) => item.configured);
    this.el("provider-model-search").disabled = locked || !!this.modelsRequest || !this.models.length;
    this.el("provider-model-refresh").disabled = locked || !!this.catalogRequest || !!this.modelsRequest
      || !this.providers?.some((item) => item.id === this.el("provider-select").value && item.configured);
    for (const button of this.el("provider-model-list").children) button.disabled = locked;
    this.el("provider-retry").hidden = !this.errorOperation;
    this.el("provider-retry").disabled = locked || !!this.catalogRequest || !!this.modelsRequest;
    this.el("provider-apply").disabled = !this.canApply();
    this.setText("provider-apply", this.switching ? "Switching…" : "Use model");
    this.setText("provider-selection", this.selectedModel ? `${this.label(this.el("provider-select").value)} / ${this.selectedModel}` : "Select a model to continue.");
    this.setText("provider-status", this.switching ? "Checking this model before switching…" : this.blockedReason
      || (this.catalogRequest ? "Loading providers…" : this.modelsRequest ? "Loading models…"
        : this.providers && !this.providers.some((item) => item.configured) ? "Set up a provider with Manage providers to get started."
          : this.models.length && !this.el("provider-model-list").children.length ? "No models match your search."
            : this.models.length ? "Choose a model from this provider." : ""));
    this.settings.render();
  }
}
