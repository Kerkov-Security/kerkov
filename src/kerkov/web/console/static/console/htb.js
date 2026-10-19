/* App Tokens travel through dedicated socket messages, never conversation text. */
class HTBSettings {
  constructor({ send, requestId }) {
    this.send = send;
    this.requestId = requestId;
    this.pending = null;
    this.connected = false;
    this.dialog = document.getElementById("htb-dialog");
    this.token = document.getElementById("htb-token");
    this.status = document.getElementById("htb-status");
    this.submit = document.getElementById("htb-connect");
    this.open = document.getElementById("htb-open");
    this.open.addEventListener("click", () => {
      this.dialog.showModal();
      this.request("htb.status");
    });
    document.getElementById("htb-close").addEventListener("click", () => this.dialog.close());
    this.dialog.addEventListener("close", () => { this.token.value = ""; });
    document.getElementById("htb-form").addEventListener("submit", (event) => {
      event.preventDefault();
      this.request("htb.configure", this.token.value);
      this.token.value = "";
    });
  }

  setConnected(connected) {
    this.connected = connected;
    if (!connected) {
      this.token.value = "";
      if (this.pending) this.status.textContent = "Disconnected before confirmation. Reopen HTB to check setup.";
      this.pending = null;
      clearTimeout(this.timer);
    }
    this.render();
  }

  render() {
    this.open.disabled = !this.connected;
    this.submit.disabled = !this.connected || !!this.pending;
    this.token.disabled = !this.connected || !!this.pending;
  }

  request(type, token) {
    if (!this.connected || this.pending) return;
    const request_id = this.requestId();
    this.pending = request_id;
    const payload = { type, request_id };
    if (type === "htb.configure") payload.token = token;
    if (!this.send(payload)) {
      this.pending = null;
      this.status.textContent = "Unable to send HTB request. Try again when connected.";
    } else {
      this.status.textContent = type === "htb.configure" ? "Verifying and saving App Token…" : "Checking saved setup…";
      this.timer = setTimeout(() => {
        if (this.pending !== request_id) return;
        this.pending = null;
        this.status.textContent = "HTB confirmation timed out. Reopen HTB to check setup.";
        this.render();
      }, 60000);
    }
    this.render();
  }

  handleEvent(type, payload) {
    if (payload.request_id !== this.pending) return;
    clearTimeout(this.timer);
    this.pending = null;
    this.status.textContent = payload.detail || (payload.configured
      ? "An HTB App Token is configured. Connect again to verify or replace it."
      : "Add your HTB App Token to connect lab tools.");
    this.render();
  }
}
