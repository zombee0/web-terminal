// Hosted-only authentication. The shared console uses optional capabilities.
window.HANGAR_CAPABILITIES = {
  defaultRuntime: "cloud", disableLocal: true, disableDirectAccess: true,
  authenticatedAccess: true,
  hint: "Per account: 2 CPU / 4 GiB total. Per Box: up to 10 GB disk.\nStopped Boxes keep their allocation until deleted."
};

// Scoped to the hosted account area, leaving the standalone console untouched.
const hostedAccountStyles = `
  .hangar-hosted header { grid-template-columns: minmax(0, 1fr) minmax(280px, 340px); align-items: center; gap: 24px; }
  .hosted-header-tools { display: flex; flex-direction: column; align-items: stretch; gap: 10px; min-width: 0; }
  .hangar-hosted .sys { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 5px 14px; font-size: 10px; letter-spacing: .04em; }
  .hangar-hosted .sys > div { white-space: nowrap; }
  .hangar-hosted .sys .hosted-unavailable { color: var(--muted); font-weight: 400; }
  .hosted-account { display: flex; align-items: center; gap: 10px; min-width: 0; min-height: 62px; padding: 11px 12px; border: 1px solid var(--line); background: rgba(6, 16, 18, .72); }
  .hosted-account-avatar { display: grid; place-items: center; flex: 0 0 32px; height: 32px; border: 1px solid #24523c; background: #0b2118; color: var(--green); font-size: 13px; font-weight: 500; }
  .hosted-account-identity { flex: 1; min-width: 0; text-align: left; }
  .hosted-account-name, .hosted-account-email { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .hosted-account-name { color: var(--text); font-size: 12px; font-weight: 500; line-height: 1.5; }
  .hosted-account-email { color: var(--muted); font-size: 10px; line-height: 1.6; }
  .hosted-account-action { display: inline-flex; align-items: center; justify-content: center; gap: 7px; flex-shrink: 0; padding: 8px; border: 1px solid transparent; background: transparent; color: var(--muted); font: inherit; font-size: 10px; letter-spacing: .06em; white-space: nowrap; cursor: pointer; transition: color .15s, border-color .15s, background .15s; }
  .hosted-account-action svg { width: 15px; height: 15px; flex-shrink: 0; }
  .hosted-account-action:hover { color: var(--text); border-color: var(--line); background: #0b1d16; }
  .hosted-account-action:focus-visible { outline: 2px solid var(--green); outline-offset: 3px; }
  .hosted-account-action:disabled { cursor: wait; opacity: .5; }
  .hosted-account.is-guest { padding: 0; }
  .hosted-account-signin { width: 100%; min-height: 60px; padding: 12px 16px; color: var(--green); font-size: 11px; gap: 10px; }
  .hosted-account-signin:hover { color: var(--green); border-color: var(--green-dim); }
  .hosted-account-loading { color: var(--muted); font-size: 10px; letter-spacing: .08em; }
  .hosted-admin-link { align-self: flex-end; color: var(--cyan); font-size: 10px; letter-spacing: .06em; text-decoration: none; }
  .hosted-admin-link:hover { text-decoration: underline; }
  .hosted-admin-link[hidden] { display: none; }
  .hangar-hosted .row-actions { flex-wrap: wrap; }
  .hangar-hosted .row-actions button { flex: 0 0 auto; white-space: nowrap; }
  .hangar-hosted td.col-ops { overflow: visible; }
  .hangar-hosted #fleet { overflow-x: auto; }
  .hangar-hosted #fleet table { min-width: 820px; }
  .hosted-quota { color: var(--cyan); font-size: 11px; line-height: 1.6; margin-top: 12px; }
  .hosted-option { display: flex; align-items: center; gap: 8px; margin-top: 14px; color: var(--text); font-size: 11px; letter-spacing: .06em; cursor: pointer; }
  .hosted-option input { width: 16px; height: 16px; margin: 0; accent-color: var(--green); }
  .hosted-option-note { color: var(--muted); font-size: 10px; line-height: 1.6; margin: 4px 0 0 24px; }
  .hosted-details { width: min(560px, calc(100vw - 32px)); max-height: calc(100vh - 32px); padding: 22px; border: 1px solid var(--green-dim); background: var(--panel-solid); color: var(--text); box-shadow: var(--shadow); }
  .hosted-details::backdrop { background: rgba(0, 0, 0, .7); }
  .hosted-details h2 { margin: 0 0 14px; color: var(--green); font-size: 13px; letter-spacing: .12em; }
  .hosted-details h3 { margin: 20px 0 6px; color: var(--cyan); font-size: 11px; font-weight: 500; letter-spacing: .1em; }
  .hosted-details dl { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 6px 14px; margin: 0; font-size: 11px; }
  .hosted-details dt { color: var(--muted); letter-spacing: .06em; }
  .hosted-details dd { margin: 0; overflow-wrap: anywhere; }
  .hosted-details-note { color: var(--amber); font-size: 10px; line-height: 1.6; margin: 0 0 10px; }
  .hosted-port-form { display: flex; gap: 8px; }
  .hosted-port-form input { flex: 1; min-width: 0; }
  .hosted-port-list { list-style: none; margin: 12px 0 0; padding: 0; display: grid; gap: 8px; }
  .hosted-port-list li { display: grid; grid-template-columns: 56px minmax(0, 1fr) auto; align-items: center; gap: 10px; padding: 8px 10px; border: 1px solid var(--line); font-size: 11px; }
  .hosted-port-list b { color: var(--green); font-weight: 500; }
  .hosted-port-list a { color: var(--cyan); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .hosted-port-list span { gap: 6px; }
  .hosted-port-list button { padding: 6px 8px; font-size: 10px; }
  .hosted-port-list li.hosted-port-empty { display: block; padding: 18px 14px; border-style: dashed; color: var(--muted); font-size: 11px; line-height: 1.6; text-align: center; }
  .hosted-details-error { min-height: 1.6em; color: var(--red); font-size: 11px; margin: 8px 0 0; }
  .hosted-details-close { display: flex; justify-content: flex-end; margin-top: 16px; }
  @media (max-width: 520px) {
    .hosted-port-list li { grid-template-columns: minmax(0, 1fr) auto; }
    .hosted-port-list a { grid-column: 1 / -1; grid-row: 2; }
  }
  @media (max-width: 960px) {
    .hangar-hosted header { grid-template-columns: minmax(0, 1fr); gap: 18px; }
    .hosted-header-tools { width: 100%; }
    .hangar-hosted .sys { justify-content: space-between; }
  }
  @media (prefers-reduced-motion: reduce) {
    .hosted-account-action { transition: none; }
  }
`;

function hostedAccountIcon(action) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.5");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(svg.namespaceURI, "path");
  path.setAttribute("d", action === "out"
    ? "M9 5H5v14h4M13 8l4 4-4 4M9 12h12"
    : "M15 5h4v14h-4M10 8l4 4-4 4M3 12h11");
  svg.append(path);
  return svg;
}

window.HANGAR_HOSTED = {
  user: null, csrf: null, used: { cpus: 0, memory_mib: 0 }, loaded: false,
  async initialize() {
    const style = document.createElement("style");
    style.textContent = hostedAccountStyles;
    document.head.append(style);
    document.body.classList.add("hangar-hosted");
    const tools = document.createElement("div");
    tools.className = "hosted-header-tools";
    const panel = document.createElement("div");
    panel.id = "hosted-auth";
    panel.className = "hosted-account";
    panel.setAttribute("role", "group");
    panel.setAttribute("aria-label", "Account");
    const loading = document.createElement("span");
    loading.className = "hosted-account-loading";
    loading.textContent = "CHECKING SIGN-IN…";
    panel.append(loading);
    const status = document.querySelector("header .sys");
    status.before(tools);
    const admin = document.createElement("a");
    admin.id = "hosted-admin-link";
    admin.className = "hosted-admin-link";
    admin.href = "/admin/";
    admin.textContent = "ADMIN OVERVIEW →";
    admin.hidden = true;
    tools.append(panel, admin, status);
    const localStatus = status.children[1].querySelector("b");
    localStatus.textContent = "DISABLED";
    localStatus.className = "hosted-unavailable";
    const local = document.getElementById("btn-local");
    local.disabled = true;
    local.title = "Local is unavailable in this deployment";
    local.before(document.getElementById("btn-cloud"));
    const fields = { "res-cpus": [1, 2, 1, 1], "res-memory": [512, 4096, 512, 2048], "res-disk": [1, 10, 1, 10] };
    for (const [id, [min, max, step, value]] of Object.entries(fields)) {
      const input = document.getElementById(id);
      Object.assign(input, { min, max, step, value, disabled: false });
      input.addEventListener("input", () => this.quota());
    }
    const quota = document.createElement("div"); quota.id = "hosted-quota"; quota.className = "hosted-quota";
    quota.setAttribute("role", "status");
    document.getElementById("target-hint").after(quota);
    // Google's consent must go to its own CLI client, so the Box only installs
    // agy; the user signs in with Google in the first terminal they open.
    const gemini = document.createElement("label");
    gemini.className = "hosted-option";
    const geminiInput = document.createElement("input");
    geminiInput.type = "checkbox";
    geminiInput.id = "hosted-gemini";
    geminiInput.setAttribute("aria-describedby", "hosted-gemini-note");
    gemini.append(geminiInput, document.createTextNode("TRY GEMINI FREE TIER"));
    const geminiNote = document.createElement("div");
    geminiNote.id = "hosted-gemini-note";
    geminiNote.className = "hosted-option-note";
    geminiNote.textContent = "Installs Google's Antigravity CLI (agy). When you first open the terminal, tap the Google link, approve, and paste the code shown. Google sets the free usage limits.";
    quota.after(gemini, geminiNote);
    const fleet = document.getElementById("fleet");
    this.details = this.buildDetails();
    new MutationObserver(() => {
      const labels = { open: "Open your terminal after sign-in and ownership checks", copy: "Copy a link that requires the Box owner's sign-in",
        stop: "Stop the Box; keep its data and resource allocation", resume: "Start the stopped Box", delete: "Permanently delete the Box and its data" };
      document.querySelectorAll("#fleet button[data-act]").forEach(button => { button.title = labels[button.dataset.act] || ""; });
      fleet.querySelectorAll(".row-actions").forEach(actions => {
        const id = actions.querySelector("button[data-id]")?.dataset.id;
        if (!id || actions.querySelector("[data-hosted-details]")) return;
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.hostedDetails = id;
        button.textContent = "DETAILS";
        button.title = "Box details and public ports";
        actions.prepend(button);
      });
      if (this.details.open) this.showBox(fleet._boxes?.[this.details.dataset.box]);
    }).observe(fleet, { childList: true });
    fleet.addEventListener("click", event => {
      const button = event.target.closest("button[data-hosted-details]");
      if (button) this.openDetails(fleet._boxes?.[button.dataset.hostedDetails]);
    });
    await this.refresh();
    setInterval(() => this.refresh().catch(() => this.render(null)), 30000);
  },
  async refresh() {
    const response = await fetch("/api/session", { cache: "no-store" });
    if (!response.ok) throw new Error("Authentication is unavailable");
    const data = await response.json();
    this.user = data.user;
    this.csrf = data.csrf;
    this.render(data.user);
  },
  render(user) {
    const panel = document.getElementById("hosted-auth");
    const accountKey = JSON.stringify(user ? [user.name, user.email, user.is_admin] : null);
    this.user = user;
    document.getElementById("hosted-admin-link").hidden = !user?.is_admin;
    this.quota();
    // Polling should not destroy the focused sign-out button every 30 seconds.
    if (panel.dataset.accountKey === accountKey) return;
    panel.dataset.accountKey = accountKey;
    panel.replaceChildren();
    panel.classList.toggle("is-guest", !user);
    if (user) {
      const displayName = user.name?.trim() || user.email.split("@")[0];
      const avatar = document.createElement("span");
      avatar.className = "hosted-account-avatar";
      avatar.setAttribute("aria-hidden", "true");
      avatar.textContent = Array.from(displayName)[0]?.toLocaleUpperCase() || "U";
      const identity = document.createElement("div");
      identity.className = "hosted-account-identity";
      const name = document.createElement("span");
      name.className = "hosted-account-name";
      name.textContent = displayName;
      name.title = displayName;
      const email = document.createElement("span");
      email.className = "hosted-account-email";
      email.textContent = user.email;
      email.title = user.email;
      identity.append(name, email);
      const logout = document.createElement("button");
      logout.type = "button";
      logout.className = "hosted-account-action";
      logout.append(hostedAccountIcon("out"), document.createTextNode("SIGN OUT"));
      logout.onclick = async () => {
        const response = await fetch("/auth/logout", { method: "POST", headers: this.headers("POST") });
        if (response.ok) location.assign("/");
        else { await this.refresh(); }
      };
      panel.append(avatar, identity, logout);
    } else {
      const login = document.createElement("button");
      login.type = "button";
      login.className = "hosted-account-action hosted-account-signin";
      login.append(hostedAccountIcon("in"), document.createTextNode("SIGN IN WITH GOOGLE"));
      login.onclick = () => location.assign("/auth/login");
      panel.append(login);
    }
  },
  boxesChanged(boxes) {
    this.loaded = true;
    this.used = boxes.reduce((used, box) => ({ cpus: used.cpus + box.cpus, memory_mib: used.memory_mib + box.memory_mib }), { cpus: 0, memory_mib: 0 });
    this.quota();
  },
  quota() {
    const fields = ["res-cpus", "res-memory", "res-disk"].map(id => document.getElementById(id));
    const fits = this.used.cpus + Number(fields[0].value) <= 2 && this.used.memory_mib + Number(fields[1].value) <= 4096;
    document.getElementById("btn-create").disabled = !this.user || !this.loaded || !fits || fields.some(input => !input.value || !input.checkValidity());
    const label = document.getElementById("hosted-quota");
    if (label) label.textContent = !this.user ? "Sign in to view your available resources." : !this.loaded ? "Loading your allocation…"
      : `AVAILABLE: ${Math.max(0, 2-this.used.cpus)} CPU / ${Math.max(0, 4096-this.used.memory_mib)} MiB · 10 GB disk max per Box`;
  },
  buildDetails() {
    const element = (tag, props = {}, ...children) => {
      const node = Object.assign(document.createElement(tag), props);
      node.append(...children);
      return node;
    };
    const dialog = element("dialog", { className: "hosted-details", id: "hosted-details" });
    dialog.setAttribute("aria-labelledby", "hosted-details-title");
    const facts = element("dl");
    const input = element("input", { type: "number", min: 1, max: 65535, step: 1, placeholder: "Guest port, e.g. 8000", id: "hosted-port-input" });
    input.setAttribute("aria-label", "Guest port to expose");
    const expose = element("button", { type: "submit", className: "primary" }, "EXPOSE");
    const form = element("form", { className: "hosted-port-form actions" }, input, expose);
    const list = element("ul", { className: "hosted-port-list", id: "hosted-port-list" });
    const error = element("p", { className: "hosted-details-error", id: "hosted-port-error" });
    error.setAttribute("role", "alert");
    const close = element("button", { type: "button" }, "CLOSE");
    dialog.append(
      element("h2", { id: "hosted-details-title" }, "BOX DETAILS"), facts,
      element("h3", {}, "PUBLIC PORTS"),
      element("p", { className: "hosted-details-note" }, "Anyone with a port's link can reach it, without signing in. The terminal (port 3001) always requires your sign-in."),
      form, list, error, element("div", { className: "hosted-details-close actions" }, close));
    document.body.append(dialog);
    close.onclick = () => dialog.close();
    form.onsubmit = async event => {
      event.preventDefault();
      const port = Number(input.value);
      if (!Number.isInteger(port) || port < 1 || port > 65535) { error.textContent = "Enter a port from 1 to 65535."; return; }
      expose.disabled = true;
      error.textContent = "";
      try {
        await this.portsRequest("POST", `/api/boxes/${dialog.dataset.box}/ports`, { port });
        input.value = "";
        await this.loadPorts();
      } catch (failure) { error.textContent = failure.message; }
      finally { expose.disabled = this.details.dataset.status !== "running"; }
    };
    list.onclick = async event => {
      const button = event.target.closest("button[data-port]");
      if (!button) return;
      const port = this.ports.find(item => item.port === Number(button.dataset.port));
      if (!port) return;
      error.textContent = "";
      try {
        if (button.dataset.action === "copy") {
          await navigator.clipboard.writeText(port.url);
          toast(`PORT ${port.port} LINK COPIED`);
          return;
        }
        button.disabled = true;
        await this.portsRequest("DELETE", `/api/boxes/${dialog.dataset.box}/ports/${port.port}`);
        await this.loadPorts();
      } catch (failure) { button.disabled = false; error.textContent = failure.message; }
    };
    Object.assign(dialog, { facts, input, expose, list, error });
    return dialog;
  },
  showBox(box) {
    const dialog = this.details;
    if (!box) { if (dialog.open) dialog.close(); return; }
    dialog.dataset.box = box.id;
    dialog.dataset.status = box.status;
    const rows = [["NAME", box.name], ["ID", box.id], ["STATE", (box.status || "unknown").toUpperCase()],
      ["RESOURCES", `${box.cpus} CPU · ${box.memory_mib} MiB · ${box.disk_size_gb} GB disk`]];
    dialog.facts.replaceChildren(...rows.flatMap(([term, value]) => {
      const dt = document.createElement("dt"); dt.textContent = term;
      const dd = document.createElement("dd"); dd.textContent = value;
      return [dt, dd];
    }));
    const running = box.status === "running";
    dialog.expose.disabled = !running;
    dialog.input.disabled = !running;
    dialog.expose.title = running ? "Open a public link to this guest port" : "Start this Box before exposing a port";
  },
  async openDetails(box) {
    if (!box) return;
    this.ports = [];
    this.details.error.textContent = "";
    this.details.list.replaceChildren();
    this.showBox(box);
    this.details.showModal();
    try { await this.loadPorts(); }
    catch (failure) { this.details.error.textContent = failure.message; }
  },
  async loadPorts() {
    const id = this.details.dataset.box;
    const data = await this.portsRequest("GET", `/api/boxes/${id}/ports`);
    if (this.details.dataset.box !== id) return;
    this.ports = data.ports || [];
    const list = this.details.list;
    if (!this.ports.length) {
      const empty = document.createElement("li");
      empty.className = "hosted-port-empty";
      empty.textContent = "No ports exposed yet. Start a server in the Box, then expose its port here.";
      list.replaceChildren(empty);
      return;
    }
    list.replaceChildren(...this.ports.map(port => {
      const item = document.createElement("li");
      const label = document.createElement("b");
      label.textContent = String(port.port);
      const link = document.createElement("a");
      link.href = port.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = port.url;
      const actions = document.createElement("span");
      actions.className = "actions";
      for (const [action, text] of [["copy", "COPY"], ["close", "REMOVE"]]) {
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.port = String(port.port);
        button.dataset.action = action;
        button.textContent = text;
        if (action === "close") { button.className = "danger"; button.title = `Stop exposing port ${port.port}`; }
        actions.append(button);
      }
      item.append(label, link, actions);
      return item;
    }));
  },
  async portsRequest(method, path, body) {
    const headers = this.headers(method);
    if (body) headers["Content-Type"] = "application/json";
    const response = await fetch(path, { method, headers, cache: "no-store", body: body ? JSON.stringify(body) : undefined });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
  },
  createOptions() {
    return { gemini: document.getElementById("hosted-gemini").checked };
  },
  headers(method) {
    return method && method !== "GET" ? { "X-CSRF-Token": this.csrf || "", "Idempotency-Key": crypto.randomUUID() } : {};
  }
};
