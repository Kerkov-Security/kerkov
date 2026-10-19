/* Read-only view of the engagement workspace. File contents are plain text
   from disk and only ever reach the DOM through textContent. */
const SCRIPTS_KEYWORDS = (words) => new Set(words.split(/\s+/).filter(Boolean));

const SCRIPTS_LANGUAGES = {
  python: {
    line: ["#"],
    strings: [{ q: '"', triple: true }, { q: "'", triple: true }],
    keywords: SCRIPTS_KEYWORDS("def class return if elif else for while import from as with try except finally raise pass break continue lambda yield global nonlocal assert del in is not and or async await match case None True False self"),
  },
  bash: {
    line: ["#"],
    strings: [{ q: '"' }, { q: "'", escape: false }],
    keywords: SCRIPTS_KEYWORDS("if then else elif fi for while until do done case esac in function return local export set unset read echo exit source"),
  },
  powershell: {
    line: ["#"],
    block: [["<#", "#>"]],
    strings: [{ q: '"' }, { q: "'", escape: false }],
    keywords: SCRIPTS_KEYWORDS("if else elseif foreach for while do switch function return param begin process end try catch finally throw break continue filter"),
  },
  javascript: {
    line: ["//"],
    block: [["/*", "*/"]],
    strings: [{ q: '"' }, { q: "'" }, { q: "`", multiline: true }],
    keywords: SCRIPTS_KEYWORDS("var let const function return if else for while do switch case break continue new class extends super this typeof instanceof in of try catch finally throw async await yield import export from default null true false undefined void delete"),
  },
  json: { strings: [{ q: '"' }], keywords: SCRIPTS_KEYWORDS("true false null") },
  yaml: {
    line: ["#"],
    strings: [{ q: '"' }, { q: "'", escape: false }],
    keywords: SCRIPTS_KEYWORDS("true false null yes no on off"),
  },
  toml: {
    line: ["#"],
    strings: [{ q: '"' }, { q: "'", escape: false }],
    keywords: SCRIPTS_KEYWORDS("true false"),
  },
  ini: {
    line: [";", "#"],
    strings: [{ q: '"' }, { q: "'" }],
    keywords: SCRIPTS_KEYWORDS("true false"),
  },
  ruby: {
    line: ["#"],
    block: [["=begin", "=end"]],
    strings: [{ q: '"' }, { q: "'", escape: false }],
    keywords: SCRIPTS_KEYWORDS("def end class module if elsif else unless while until for do return yield begin rescue ensure raise then nil true false and or not self require"),
  },
  go: {
    line: ["//"],
    block: [["/*", "*/"]],
    strings: [{ q: '"' }, { q: "`", escape: false, multiline: true }],
    keywords: SCRIPTS_KEYWORDS("func package import return if else for range var const type struct interface map chan go defer select switch case break continue default nil true false"),
  },
  rust: {
    line: ["//"],
    block: [["/*", "*/"]],
    strings: [{ q: '"' }],
    keywords: SCRIPTS_KEYWORDS("fn let mut const static struct enum impl trait pub use mod match if else for while loop return break continue self Self true false None Some Ok Err where as"),
  },
  php: {
    line: ["//", "#"],
    block: [["/*", "*/"]],
    strings: [{ q: '"' }, { q: "'", escape: false }],
    keywords: SCRIPTS_KEYWORDS("function return if else elseif foreach for while do switch case break continue class new echo print public private protected static null true false use namespace"),
  },
  perl: {
    line: ["#"],
    strings: [{ q: '"' }, { q: "'", escape: false }],
    keywords: SCRIPTS_KEYWORDS("sub my our local return if elsif else unless while until for foreach use require print die"),
  },
  c: {
    line: ["//"],
    block: [["/*", "*/"]],
    strings: [{ q: '"' }, { q: "'" }],
    keywords: SCRIPTS_KEYWORDS("int char float double void long short unsigned signed struct union enum typedef static const return if else for while do switch case break continue sizeof goto"),
  },
  cpp: {
    line: ["//"],
    block: [["/*", "*/"]],
    strings: [{ q: '"' }, { q: "'" }],
    keywords: SCRIPTS_KEYWORDS("int char float double void long short unsigned signed struct union enum typedef static const return if else for while do switch case break continue sizeof class new delete template namespace using public private protected virtual override bool true false nullptr auto"),
  },
  java: {
    line: ["//"],
    block: [["/*", "*/"]],
    strings: [{ q: '"' }, { q: "'" }],
    keywords: SCRIPTS_KEYWORDS("public private protected class interface extends implements void int long double float boolean char return if else for while do switch case break continue new this super static final import package try catch finally throw throws null true false"),
  },
  sql: {
    line: ["--"],
    block: [["/*", "*/"]],
    strings: [{ q: "'", escape: false }],
    ci: true,
    keywords: SCRIPTS_KEYWORDS("SELECT FROM WHERE INSERT INTO VALUES UPDATE SET DELETE CREATE TABLE DROP ALTER ADD JOIN LEFT RIGHT INNER OUTER ON GROUP BY ORDER HAVING LIMIT OFFSET AS AND OR NOT NULL LIKE IN IS DISTINCT UNION PRIMARY KEY FOREIGN REFERENCES INDEX"),
  },
  html: { block: [["<!--", "-->"]], strings: [{ q: '"' }, { q: "'" }], numbers: false },
  css: { block: [["/*", "*/"]], strings: [{ q: '"' }, { q: "'" }] },
  dockerfile: {
    line: ["#"],
    ci: true,
    keywords: SCRIPTS_KEYWORDS("FROM RUN CMD LABEL EXPOSE ENV ADD COPY ENTRYPOINT VOLUME USER WORKDIR ARG ONBUILD STOPSIGNAL HEALTHCHECK SHELL MAINTAINER"),
  },
  makefile: { line: ["#"], numbers: false },
};

const SCRIPTS_PLAIN = { numbers: false };

function scriptsConfig(language) {
  return SCRIPTS_LANGUAGES[language] || SCRIPTS_PLAIN;
}

function scriptsTokenize(src, cfg) {
  const tokens = [];
  const line = cfg.line || [];
  const block = cfg.block || [];
  const strings = cfg.strings || [];
  const keywords = cfg.keywords || new Set();
  const useNumbers = cfg.numbers !== false;
  const n = src.length;
  let i = 0;
  let buf = "";
  const flush = () => { if (buf) { tokens.push([null, buf]); buf = ""; } };
  const isIdentStart = (c) => /[A-Za-z_$]/.test(c);
  const isIdent = (c) => /[A-Za-z0-9_$]/.test(c);

  while (i < n) {
    const c = src[i];

    let lc = line.find((prefix) => src.startsWith(prefix, i));
    if (lc) {
      flush();
      let j = src.indexOf("\n", i);
      if (j === -1) j = n;
      tokens.push(["comment", src.slice(i, j)]);
      i = j;
      continue;
    }

    let bc = block.find((pair) => src.startsWith(pair[0], i));
    if (bc) {
      flush();
      let j = src.indexOf(bc[1], i + bc[0].length);
      j = j === -1 ? n : j + bc[1].length;
      tokens.push(["comment", src.slice(i, j)]);
      i = j;
      continue;
    }

    let matchedString = false;
    for (const spec of strings) {
      const triple = spec.triple ? spec.q.repeat(3) : null;
      if (triple && src.startsWith(triple, i)) {
        flush();
        let j = src.indexOf(triple, i + 3);
        j = j === -1 ? n : j + 3;
        tokens.push(["string", src.slice(i, j)]);
        i = j;
        matchedString = true;
        break;
      }
      if (c === spec.q) {
        flush();
        let j = i + 1;
        while (j < n) {
          if (spec.escape !== false && src[j] === "\\") { j += 2; continue; }
          if (src[j] === spec.q) { j += 1; break; }
          if (src[j] === "\n" && !spec.multiline) break;
          j += 1;
        }
        tokens.push(["string", src.slice(i, Math.min(j, n))]);
        i = Math.min(j, n);
        matchedString = true;
        break;
      }
    }
    if (matchedString) continue;

    if (isIdentStart(c)) {
      flush();
      let j = i + 1;
      while (j < n && isIdent(src[j])) j += 1;
      const word = src.slice(i, j);
      const key = cfg.ci ? word.toUpperCase() : word;
      tokens.push([keywords.has(key) ? "keyword" : null, word]);
      i = j;
      continue;
    }

    if (useNumbers && c >= "0" && c <= "9") {
      flush();
      let j = i + 1;
      while (j < n && /[0-9a-fA-FxXoObB._]/.test(src[j])) j += 1;
      tokens.push(["number", src.slice(i, j)]);
      i = j;
      continue;
    }

    buf += c;
    i += 1;
  }
  flush();
  return tokens;
}

function scriptsHighlight(codeEl, source, language) {
  const tokens = scriptsTokenize(source, scriptsConfig(language));
  const fragment = document.createDocumentFragment();
  let lineNo = 1;
  const newLine = () => {
    const row = document.createElement("div");
    row.className = "scripts-line";
    const gutter = document.createElement("span");
    gutter.className = "scripts-gutter";
    gutter.setAttribute("aria-hidden", "true");
    gutter.textContent = String(lineNo);
    const code = document.createElement("span");
    code.className = "scripts-line-code";
    row.append(gutter, code);
    fragment.appendChild(row);
    return code;
  };
  let code = newLine();
  for (const [cls, text] of tokens) {
    const pieces = text.split("\n");
    pieces.forEach((piece, index) => {
      if (index > 0) { lineNo += 1; code = newLine(); }
      if (!piece) return;
      if (cls) {
        const span = document.createElement("span");
        span.className = `tok-${cls}`;
        span.textContent = piece;
        code.appendChild(span);
      } else {
        code.appendChild(document.createTextNode(piece));
      }
    });
  }
  codeEl.replaceChildren(fragment);
}

const SCRIPTS_LANG_LABELS = {
  python: "Python", bash: "Shell", powershell: "PowerShell", javascript: "JavaScript",
  json: "JSON", yaml: "YAML", toml: "TOML", ini: "INI", ruby: "Ruby", go: "Go",
  rust: "Rust", php: "PHP", perl: "Perl", c: "C", cpp: "C++", java: "Java",
  sql: "SQL", html: "HTML", css: "CSS", markdown: "Markdown",
  dockerfile: "Dockerfile", makefile: "Makefile",
};

function scriptsFormatSize(bytes) {
  if (!Number.isFinite(bytes)) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

class ScriptsPanel {
  constructor({ send, requestId }) {
    this.send = send;
    this.requestId = requestId;
    this.connected = false;
    this.available = false;
    this.contextId = null;
    this.files = [];
    this.filter = "";
    this.selectedPath = null;
    this.listRequest = null;
    this.readRequest = null;
    this.currentContent = "";
    this.elements = {};
    for (const id of [
      "scripts-tab", "scripts-view", "scripts-count", "scripts-status",
      "scripts-refresh", "scripts-search", "scripts-list", "scripts-empty",
      "scripts-truncated", "scripts-selection-empty", "scripts-document",
      "scripts-doc-lang", "scripts-doc-name", "scripts-doc-path", "scripts-doc-size",
      "scripts-copy", "scripts-copy-label", "scripts-doc-notice", "scripts-code",
    ]) {
      this.elements[id] = document.getElementById(id);
    }
    this.el("scripts-refresh").addEventListener("click", () => this.refresh(true));
    this.el("scripts-search").addEventListener("input", (event) => {
      this.filter = event.target.value.trim().toLowerCase();
      this.renderList();
    });
    this.el("scripts-copy").addEventListener("click", () => this.copy());
  }

  el(id) { return this.elements[id]; }

  setStatus(text) {
    const node = this.el("scripts-status");
    if (node.textContent !== text) node.textContent = text;
  }

  setState({ connected }) {
    const wasConnected = this.connected;
    this.connected = connected;
    if (!connected && wasConnected) {
      this.listRequest = null;
      this.readRequest = null;
      this.setStatus("Disconnected. Showing the last received scripts.");
    }
    this.el("scripts-refresh").disabled = !connected;
    this.el("scripts-search").disabled = !connected && this.files.length === 0;
  }

  show() {
    if (this.connected) this.refresh();
  }

  reset() {
    this.contextId = null;
    this.files = [];
    this.filter = "";
    this.selectedPath = null;
    this.currentContent = "";
    this.listRequest = null;
    this.readRequest = null;
    this.el("scripts-search").value = "";
    this.renderList();
    this.clearViewer();
    this.updateCount();
  }

  refresh(force = false) {
    if (!this.connected) return;
    if (this.listRequest && !force) return;
    const id = this.requestId();
    this.listRequest = id;
    if (!this.send({ type: "scripts.list", request_id: id })) {
      this.listRequest = null;
      this.setStatus("Unable to load scripts. Open this tab to try again.");
    }
  }

  handleEvent(type, payload) {
    if (type === "scripts.snapshot") {
      this.receiveSnapshot(payload);
    } else if (type === "scripts.content") {
      this.receiveContent(payload);
    } else if (type === "scripts.error") {
      this.receiveError(payload);
    }
  }

  receiveSnapshot(payload) {
    this.listRequest = null;
    this.contextId = payload.context_id;
    this.available = payload.available === true;
    this.files = Array.isArray(payload.files) ? payload.files : [];
    this.el("scripts-truncated").hidden = payload.truncated !== true;
    this.el("scripts-search").disabled = !this.connected && this.files.length === 0;
    if (this.selectedPath && !this.files.some((file) => file.path === this.selectedPath)) {
      this.selectedPath = null;
      this.clearViewer();
    }
    const detail = payload.detail
      || (this.available ? "" : "Scripts appear here once the engagement starts.");
    this.setStatus(detail || `${this.files.length} ${this.files.length === 1 ? "file" : "files"} in the workspace.`);
    this.renderList();
    this.updateCount();
  }

  updateCount() {
    const badge = this.el("scripts-count");
    if (this.files.length > 0) {
      badge.hidden = false;
      badge.textContent = String(this.files.length);
    } else {
      badge.hidden = true;
    }
  }

  renderList() {
    const list = this.el("scripts-list");
    const visible = this.filter
      ? this.files.filter((file) => file.path.toLowerCase().includes(this.filter))
      : this.files;
    list.replaceChildren();
    for (const file of visible) {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "scripts-item";
      button.setAttribute("aria-pressed", String(file.path === this.selectedPath));
      if (file.path === this.selectedPath) button.classList.add("active");

      const name = document.createElement("span");
      name.className = "scripts-item-name";
      name.textContent = file.name;
      const meta = document.createElement("span");
      meta.className = "scripts-item-meta";
      const dir = file.path.includes("/") ? file.path.slice(0, file.path.lastIndexOf("/")) : "";
      meta.textContent = [dir, scriptsFormatSize(file.size)].filter(Boolean).join(" · ");
      button.append(name, meta);
      button.addEventListener("click", () => this.select(file.path));
      item.appendChild(button);
      list.appendChild(item);
    }
    const empty = this.el("scripts-empty");
    if (this.files.length === 0) {
      empty.hidden = false;
      empty.textContent = this.available
        ? "No scripts have been generated yet."
        : "Scripts appear here once the engagement starts.";
    } else if (visible.length === 0) {
      empty.hidden = false;
      empty.textContent = "No scripts match your filter.";
    } else {
      empty.hidden = true;
    }
  }

  select(path) {
    if (!this.connected || this.contextId === null) return;
    this.selectedPath = path;
    this.renderList();
    const id = this.requestId();
    this.readRequest = id;
    this.el("scripts-selection-empty").hidden = true;
    this.el("scripts-document").hidden = false;
    this.el("scripts-doc-notice").hidden = true;
    this.el("scripts-copy").disabled = true;
    this.el("scripts-doc-name").textContent = path.includes("/") ? path.slice(path.lastIndexOf("/") + 1) : path;
    this.el("scripts-doc-path").textContent = path;
    this.el("scripts-doc-size").textContent = "Loading…";
    this.el("scripts-doc-lang").hidden = true;
    this.el("scripts-code").querySelector("code").replaceChildren();
    if (!this.send({ type: "scripts.read", request_id: id, context_id: this.contextId, path })) {
      this.readRequest = null;
      this.el("scripts-doc-size").textContent = "Unable to open this script.";
    }
  }

  receiveContent(payload) {
    if (payload.context_id !== this.contextId || payload.path !== this.selectedPath) return;
    this.readRequest = null;
    const notice = this.el("scripts-doc-notice");
    const codeEl = this.el("scripts-code").querySelector("code");
    this.el("scripts-doc-size").textContent = scriptsFormatSize(payload.size);
    const langLabel = SCRIPTS_LANG_LABELS[payload.language];
    const langBadge = this.el("scripts-doc-lang");
    langBadge.hidden = !langLabel;
    if (langLabel) langBadge.textContent = langLabel;

    if (payload.binary) {
      this.currentContent = "";
      this.el("scripts-copy").disabled = true;
      codeEl.replaceChildren();
      notice.hidden = false;
      notice.textContent = "This file is binary and cannot be displayed.";
      return;
    }
    this.currentContent = payload.content || "";
    this.el("scripts-copy").disabled = this.currentContent.length === 0;
    scriptsHighlight(codeEl, this.currentContent, payload.language);
    if (payload.truncated) {
      notice.hidden = false;
      notice.textContent = "Showing the first 512 KB of this file.";
    } else {
      notice.hidden = true;
    }
  }

  receiveError(payload) {
    if (payload.context_id && payload.context_id !== this.contextId) return;
    if (payload.operation === "read") {
      this.el("scripts-doc-size").textContent = payload.detail || "Unable to open this script.";
      this.readRequest = null;
      return;
    }
    this.listRequest = null;
    this.setStatus(payload.detail || "Unable to load scripts.");
  }

  clearViewer() {
    this.el("scripts-document").hidden = true;
    this.el("scripts-selection-empty").hidden = false;
    this.el("scripts-code").querySelector("code").replaceChildren();
    this.currentContent = "";
    this.el("scripts-copy").disabled = true;
  }

  async copy() {
    if (!this.currentContent) return;
    const label = this.el("scripts-copy-label");
    try {
      await navigator.clipboard.writeText(this.currentContent);
      label.textContent = "Copied";
    } catch (_error) {
      label.textContent = "Copy failed";
    }
    window.setTimeout(() => { label.textContent = "Copy"; }, 2000);
  }
}
