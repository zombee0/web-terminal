(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = { section: "boxes", page: 1, search: "", status: "", sequence: 0, denied: false };
  const views = {
    boxes: { name: "Boxes", headers: ["BOX", "OWNER", "STATUS", "RESOURCES", "CREATED / UPDATED"], statuses: ["creating", "starting", "running", "stopping", "stopped", "deleting", "error", "deleted"] }
  };
  let timer, controller;
  let owner = false, ownerEpoch = 0, historyPage = 1, roleBusy = false, membershipSequence = 0, lookupSequence = 0;
  const roleNames = { user: "User", admin: "Administrator", owner: "Site owner" };
  function text(tag, value, className) {
    const node = document.createElement(tag);
    node.textContent = value == null || value === "" ? "—" : String(value);
    if (className) node.className = className;
    return node;
  }
  function date(value) { return value ? new Date(value).toLocaleString() : "—"; }
  function cell(primary, secondary) {
    const node = text("td", primary);
    if (secondary) node.append(text("small", secondary));
    return node;
  }
  function badge(value) {
    const node = document.createElement("td");
    const tone = ["running", "succeeded"].includes(value) ? "good" : ["error", "failed"].includes(value) ? "bad"
      : ["creating", "starting", "stopping", "deleting", "queued"].includes(value) ? "warn" : "muted";
    node.append(text("span", value?.toUpperCase(), "badge " + tone));
    return node;
  }
  function message(value) { $("message").textContent = value; $("message").hidden = !value; }
  function empty(value) {
    const row = document.createElement("tr"), column = text("td", value, "empty");
    column.colSpan = views[state.section].headers.length;
    row.append(column);
    $("records").tBodies[0].replaceChildren(row);
  }
  function deny(status) {
    ownerAccess(false);
    state.denied = true;
    state.sequence++;
    for (const id of ["boxes", "deleted", "running", "stopped", "allocation", "errors", "page-label", "viewer", "updated"]) $(id).textContent = "—";
    for (const input of document.querySelectorAll("button,input,select")) input.disabled = true;
    $("records").removeAttribute("aria-busy");
    empty("Access ended");
    message(status === 401 ? "Your session has ended. Sign in to continue." : "Administrator access is required. Return to My Fleet to view your own Box.");
    $("reauth").hidden = status !== 401;
  }
  async function api(path, signal) {
    const response = await fetch(path, { cache: "no-store", credentials: "same-origin", signal });
    if (response.status === 401 || response.status === 403) deny(response.status);
    if (!response.ok) throw new Error("Could not refresh records. Please try again.");
    const data = await response.json();
    if (state.denied) throw new Error("Access ended");
    return data;
  }
  async function overview() {
    try {
      const data = await api("/api/admin/overview");
      const boxes = data.boxes;
      $("viewer").textContent = data.viewer.email;
      ownerAccess(data.viewer.role === "owner");
      $("boxes").textContent = Object.entries(boxes).reduce((total, [status, count]) => total + (status === "deleted" ? 0 : count), 0);
      $("deleted").textContent = boxes.deleted || 0;
      $("running").textContent = boxes.running || 0;
      $("stopped").textContent = boxes.stopped || 0;
      $("allocation").textContent = `${data.allocated.cpus} CPU / ${data.allocated.memory_mib / 1024} GiB / ${data.allocated.disk_size_gb} GB`;
      $("errors").textContent = boxes.error || 0;
      $("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
    } catch (error) {
      if (!state.denied) { $("updated").textContent = "Refresh failed"; message(error.message); }
    }
  }
  function ownerAccess(enabled) {
    if (owner !== enabled) ownerEpoch++;
    owner = enabled;
    $("membership").hidden = !enabled;
    if (!enabled) {
      $("candidate").replaceChildren(); $("candidate").hidden = true;
      $("member-email").value = ""; $("role-message").textContent = "";
      $("member-records").tBodies[0].replaceChildren();
      $("role-history").tBodies[0].replaceChildren();
      $("history-page").textContent = "—";
    }
  }
  async function ownerApi(path, options = {}) {
    const epoch = ownerEpoch;
    if (!owner || state.denied) throw new Error("Site owner access required.");
    if (options.method) {
      const session = await api("/api/session");
      options.headers = { "Content-Type": "application/json", "X-CSRF-Token": session.csrf };
    }
    const response = await fetch(path, { cache: "no-store", credentials: "same-origin", ...options });
    if (response.status === 401) deny(401);
    if (response.status === 403) { ownerAccess(false); overview(); }
    if (!response.ok) {
      let error = "Could not update administrator access. Refresh and try again.";
      if (response.status === 404) error = "No matching verified Google user. Ask them to sign in first.";
      if (response.status === 409) error = (await response.json()).error;
      throw new Error(error);
    }
    const data = await response.json();
    if (!owner || epoch !== ownerEpoch || state.denied) throw new Error("Access changed. Refresh to continue.");
    return data;
  }
  function roleControls(item) {
    const group = document.createElement("div"); group.className = "role-actions";
    const select = document.createElement("select");
    select.setAttribute("aria-label", `Role for ${item.email}`);
    for (const [value, label] of Object.entries(roleNames)) select.add(new Option(label, value));
    select.value = item.role;
    const apply = text("button", "APPLY"); apply.type = "button"; apply.disabled = true;
    select.addEventListener("change", () => { apply.disabled = select.value === item.role || roleBusy; });
    apply.addEventListener("click", async () => {
      if (roleBusy || !owner) return;
      const next = select.value;
      if (!window.confirm(`Change ${item.email} from ${roleNames[item.role]} to ${roleNames[next]}?`)) return;
      roleBusy = true; select.disabled = apply.disabled = true;
      membershipSequence++;
      $("role-message").textContent = "Saving…";
      try {
        await ownerApi(`/api/admin/members/${encodeURIComponent(item.user_id)}`, {
          method: "PUT", body: JSON.stringify({ role: next, expected_role: item.role })
        });
        $("candidate").replaceChildren(); $("candidate").hidden = true;
        $("role-message").textContent = "Access updated.";
      } catch (error) {
        if (owner) $("role-message").textContent = error.message;
      } finally {
        roleBusy = false;
        await overview();
        await membership();
        select.disabled = false; apply.disabled = select.value === item.role;
      }
    });
    group.append(select, apply); return group;
  }
  async function membership() {
    if (!owner || roleBusy) return;
    const sequence = ++membershipSequence;
    try {
      const [members, history] = await Promise.all([
        ownerApi("/api/admin/members"), ownerApi(`/api/admin/role-events?page=${historyPage}`)
      ]);
      if (sequence !== membershipSequence || !owner) return;
      $("member-records").tBodies[0].replaceChildren(...members.items.map(item => {
        const row = document.createElement("tr"), actions = document.createElement("td");
        actions.append(roleControls(item));
        row.append(cell(item.email, item.display_name), cell(roleNames[item.role], item.verified ? "" : "Google verification unavailable"), cell(date(item.updated_at)), actions);
        return row;
      }));
      $("role-history").tBodies[0].replaceChildren(...history.items.map(item => {
        const row = document.createElement("tr");
        row.append(cell(date(item.occurred_at)), cell(item.actor_email || item.actor_id), cell(item.target_email || item.target_id),
          cell(`${roleNames[item.previous_role]} → ${roleNames[item.new_role]}`, item.source === "bootstrap" ? "Initial setup" : ""));
        return row;
      }));
      const pages = Math.max(1, Math.ceil(history.total / history.page_size));
      $("history-page").textContent = `${history.total} changes · Page ${history.page} of ${pages}`;
      $("history-previous").disabled = history.page <= 1;
      $("history-next").disabled = history.page >= pages;
    } catch (error) {
      if (owner) {
        $("member-records").tBodies[0].replaceChildren(); $("role-history").tBodies[0].replaceChildren();
        $("role-message").textContent = error.message;
      }
    }
  }
  function row(item) {
    const element = document.createElement("tr");
    const cells = [cell(item.name, item.id), cell(item.owner_email, item.owner_name), badge(item.status),
      cell(`${item.cpus} CPU / ${item.memory_mib / 1024} GiB / ${item.disk_size_gb} GB`, item.failure_code),
      cell(date(item.created_at), date(item.updated_at))];
    element.append(...cells);
    return element;
  }
  async function listing() {
    if (state.denied) return;
    controller?.abort();
    controller = new AbortController();
    const sequence = ++state.sequence;
    $("previous").disabled = $("next").disabled = true;
    $("records").setAttribute("aria-busy", "true");
    const query = new URLSearchParams({ page: state.page, search: state.search, status: state.status });
    try {
      const data = await api(`/api/admin/${state.section}?${query}`, controller.signal);
      if (sequence !== state.sequence) return;
      const body = $("records").tBodies[0];
      body.replaceChildren(...data.items.map(row));
      if (!data.items.length) empty("No matching records");
      const pages = Math.max(1, Math.ceil(data.total / data.page_size));
      $("page-label").textContent = `${data.total} records · Page ${data.page} of ${pages}`;
      $("previous").disabled = data.page <= 1;
      $("next").disabled = data.page >= pages;
    } catch (error) {
      if (sequence !== state.sequence || state.denied || error.name === "AbortError") return;
      empty("Records are unavailable. Use Refresh to try again.");
      $("page-label").textContent = "—";
      message(error.message);
    } finally {
      if (sequence === state.sequence) $("records").removeAttribute("aria-busy");
    }
  }
  function select(section) {
    state.section = section; state.page = 1; state.status = "";
    const view = views[section];
    $("records").setAttribute("aria-label", view.name);
    const header = document.createElement("tr");
    header.append(...view.headers.map(label => { const node = text("th", label); node.scope = "col"; return node; }));
    $("records").tHead.replaceChildren(header);
    $("status").replaceChildren(new Option("All statuses", ""), ...(view.statuses || []).map(value => new Option(value.toUpperCase(), value)));
    $("status").parentElement.hidden = !view.statuses;
    empty("Loading…");
    listing();
  }
  function refresh() {
    if (state.denied) return;
    message("");
    overview().then(membership); listing();
  }
  $("refresh").addEventListener("click", refresh);
  $("search").addEventListener("input", () => {
    clearTimeout(timer);
    state.sequence++; controller?.abort();
    state.search = $("search").value; state.page = 1;
    timer = setTimeout(listing, 300);
  });
  $("status").addEventListener("change", () => { state.status = $("status").value; state.page = 1; listing(); });
  $("previous").addEventListener("click", () => { state.page--; listing(); });
  $("next").addEventListener("click", () => { state.page++; listing(); });
  $("member-search").addEventListener("submit", async event => {
    event.preventDefault();
    const sequence = ++lookupSequence;
    $("candidate").replaceChildren(); $("candidate").hidden = true;
    $("role-message").textContent = "Finding user…";
    try {
      const item = await ownerApi("/api/admin/members/lookup", { method: "POST", body: JSON.stringify({ email: $("member-email").value.trim() }) });
      if (sequence !== lookupSequence) return;
      $("candidate").append(text("p", `${item.email} · ${item.display_name || "Google account"} · ${roleNames[item.role]}`), roleControls(item));
      $("candidate").hidden = false; $("role-message").textContent = "Verify the account before changing access.";
    } catch (error) { if (owner && sequence === lookupSequence) $("role-message").textContent = error.message; }
  });
  $("history-previous").addEventListener("click", () => { historyPage--; membership(); });
  $("history-next").addEventListener("click", () => { historyPage++; membership(); });
  select("boxes"); overview().then(membership);
  setInterval(() => { if (!document.hidden) refresh(); }, 30000);
})();
