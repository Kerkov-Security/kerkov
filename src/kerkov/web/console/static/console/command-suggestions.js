/* Slash-command autocomplete for the composer. Server-sent command list only,
   so the menu never offers something the runtime won't handle. */
class CommandSuggestions {
  constructor({ input, anchor, onClientCommand }) {
    this.input = input;
    this.onClientCommand = onClientCommand;
    this.commands = [];
    this.matches = [];
    this.activeIndex = -1;
    this.open = false;

    this.listbox = document.createElement("ul");
    this.listbox.className = "command-suggestions";
    this.listbox.id = "command-suggestions";
    this.listbox.setAttribute("role", "listbox");
    this.listbox.setAttribute("aria-label", "Command suggestions");
    this.listbox.hidden = true;
    anchor.append(this.listbox);

    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", this.listbox.id);

    input.addEventListener("input", () => this.refresh());
    // mousedown over the textarea's selection so a click keeps the caret there.
    this.listbox.addEventListener("mousedown", (event) => event.preventDefault());
    this.listbox.addEventListener("click", (event) => {
      const option = event.target.closest("[data-index]");
      if (!option) return;
      this.activeIndex = Number(option.dataset.index);
      this.applyActive();
      this.input.focus();
    });
  }

  setCommands(commands) {
    this.commands = Array.isArray(commands) ? commands : [];
    this.refresh();
  }

  // Parse the composer into the command being typed and any argument after it.
  parse() {
    const value = this.input.value;
    if (!value.startsWith("/") || value.includes("\n")) return null;
    const spaceIndex = value.indexOf(" ");
    if (spaceIndex === -1) return { name: value, arg: null };
    return { name: value.slice(0, spaceIndex), arg: value.slice(spaceIndex + 1) };
  }

  computeMatches() {
    const parsed = this.parse();
    if (!parsed) return [];
    if (parsed.arg === null) {
      const token = parsed.name.toLowerCase();
      return this.commands
        .filter((command) => command.name.toLowerCase().startsWith(token))
        .map((command) => ({ kind: "command", command }));
    }
    const command = this.commands.find(
      (entry) => entry.name.toLowerCase() === parsed.name.toLowerCase(),
    );
    if (!command || !command.args.length) return [];
    const token = parsed.arg.trimStart().toLowerCase();
    return command.args
      .filter((arg) => arg.value.toLowerCase().startsWith(token))
      .map((arg) => ({ kind: "arg", command, arg }));
  }

  // What applying the highlighted match would make the composer read.
  completionFor(match) {
    if (!match) return null;
    if (match.kind === "arg") return `${match.command.name} ${match.arg.value}`;
    return match.command.args.length ? `${match.command.name} ` : match.command.name;
  }

  refresh() {
    this.matches = this.computeMatches();
    if (!this.matches.length) {
      this.close();
      return;
    }
    if (this.activeIndex < 0 || this.activeIndex >= this.matches.length) {
      this.activeIndex = 0;
    }
    this.open = true;
    this.render();
  }

  render() {
    this.listbox.replaceChildren();
    this.matches.forEach((match, index) => {
      const option = document.createElement("li");
      option.className = "command-suggestion";
      option.id = `command-suggestion-${index}`;
      option.dataset.index = String(index);
      option.setAttribute("role", "option");
      const active = index === this.activeIndex;
      option.setAttribute("aria-selected", String(active));
      option.classList.toggle("active", active);

      const label = document.createElement("span");
      label.className = "command-suggestion-label";
      label.textContent =
        match.kind === "arg" ? match.arg.value : match.command.name;
      const summary = document.createElement("span");
      summary.className = "command-suggestion-summary";
      summary.textContent =
        match.kind === "arg" ? match.arg.summary : match.command.summary;
      option.append(label, summary);
      this.listbox.append(option);
    });
    this.listbox.hidden = false;
    this.input.setAttribute("aria-expanded", "true");
    const activeOption = this.listbox.children[this.activeIndex];
    this.input.setAttribute(
      "aria-activedescendant",
      activeOption ? activeOption.id : "",
    );
    if (activeOption) activeOption.scrollIntoView({ block: "nearest" });
  }

  close() {
    this.open = false;
    this.activeIndex = -1;
    this.matches = [];
    this.listbox.hidden = true;
    this.listbox.replaceChildren();
    this.input.setAttribute("aria-expanded", "false");
    this.input.removeAttribute("aria-activedescendant");
  }

  move(delta) {
    if (!this.matches.length) return;
    this.activeIndex =
      (this.activeIndex + delta + this.matches.length) % this.matches.length;
    this.render();
  }

  setValue(value) {
    this.input.value = value;
    this.input.dispatchEvent(new Event("input", { bubbles: true }));
    window.requestAnimationFrame(() => {
      this.input.selectionStart = this.input.selectionEnd = this.input.value.length;
    });
  }

  // Fill in the highlighted match. Returns true when it changed the composer.
  applyActive() {
    const match = this.matches[this.activeIndex];
    const completion = this.completionFor(match);
    if (completion === null || completion === this.input.value) return false;
    this.setValue(completion);
    return true;
  }

  // Intercept the composer's keys. Returns true when the menu consumed one and
  // the composer must not act on it (no submit, no caret move, no fullscreen).
  handleKeydown(event) {
    if (!this.open || event.isComposing || event.keyCode === 229) return false;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      this.move(1);
      return true;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      this.move(-1);
      return true;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      this.close();
      return true;
    }
    if (event.key === "Tab" && !event.shiftKey) {
      if (this.applyActive()) {
        event.preventDefault();
        return true;
      }
      return false;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      // Completing a partial command takes one Enter; running it takes another,
      // so arrowing onto /clear or /mode never fires on the first press.
      if (this.applyActive()) {
        event.preventDefault();
        return true;
      }
      const match = this.matches[this.activeIndex];
      this.close();
      if (match && match.kind === "command" && match.command.scope === "client") {
        event.preventDefault();
        this.setValue("");
        this.onClientCommand(match.command.name);
        return true;
      }
      return false;
    }
    return false;
  }
}
