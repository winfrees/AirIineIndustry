// AirlineSim dashboard — plain JS, no framework/build step. Talks to the
// stdlib HTTP+SSE backend in server.py: GET /api/state, GET /api/events
// (live push), POST /api/command / /api/control / /api/game/*.

const els = {
  day: document.getElementById("day"),
  ver: document.getElementById("ver"),
  conn: document.getElementById("conn"),
  gameOver: document.getElementById("gameOver"),
  toast: document.getElementById("toast"),
  btnPause: document.getElementById("btnPause"),
  btnAdvance: document.getElementById("btnAdvance"),
  btnSave: document.getElementById("btnSave"),
  btnLoad: document.getElementById("btnLoad"),
  btnNew: document.getElementById("btnNew"),
  speedRange: document.getElementById("speedRange"),
  speedVal: document.getElementById("speedVal"),
  players: document.getElementById("players"),
  routes: document.getElementById("routes"),
  fleet: document.getElementById("fleet"),
  crew: document.getElementById("crew"),
  airports: document.getElementById("airports"),
  log: document.getElementById("log"),
  routeSelect: document.getElementById("routeSelect"),
  tailSelect: document.getElementById("tailSelect"),
  specSelect: document.getElementById("specSelect"),
  baseSelect: document.getElementById("baseSelect"),
  hireBaseSelect: document.getElementById("hireBaseSelect"),
  formOpenRoute: document.getElementById("formOpenRoute"),
  formAcquire: document.getElementById("formAcquire"),
  formHire: document.getElementById("formHire"),
};

let catalog = null;
let latest = null;
let toastTimer = null;

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function money(n) {
  const sign = n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

function toast(message, isErr) {
  els.toast.textContent = message;
  els.toast.classList.toggle("err", !!isErr);
  els.toast.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => els.toast.classList.add("hidden"), 2600);
}

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return res.json().catch(() => ({ ok: res.ok }));
}

async function sendCommand(type, args) {
  const res = await postJSON("/api/command", { type, ...args });
  if (res.state) render(res.state);
  toast(res.message || (res.ok ? "done" : "failed"), !res.ok);
  return res;
}

async function sendControl(action, extra) {
  const res = await postJSON("/api/control", { action, ...extra });
  if (res.state) render(res.state);
  return res;
}

// -- catalog / static dropdowns --------------------------------------------
function populateSelect(select, items, valueKey, labelFn) {
  const prev = select.value;
  select.innerHTML = items
    .map((it) => `<option value="${esc(it[valueKey])}">${esc(labelFn(it))}</option>`)
    .join("");
  if (items.some((it) => String(it[valueKey]) === prev)) select.value = prev;
}

async function loadCatalog() {
  catalog = await fetch("/api/catalog").then((r) => r.json());
  populateSelect(els.routeSelect, catalog.routes, "spec_id",
    (r) => `${r.origin} → ${r.dest} (${r.display_name})`);
  populateSelect(els.specSelect, catalog.aircraft, "spec_id",
    (a) => `${a.display_name} — ${money(a.list_price)}, ${a.max_seats}st`);
  populateSelect(els.baseSelect, catalog.airports, "iata", (a) => `${a.iata} — ${a.display_name}`);
  populateSelect(els.hireBaseSelect, catalog.airports, "iata", (a) => `${a.iata} — ${a.display_name}`);
}

// -- rendering ---------------------------------------------------------
function renderIfIdle(container, html) {
  // don't clobber an input the user is actively editing mid-tick
  if (container.contains(document.activeElement) &&
      document.activeElement !== container) {
    return;
  }
  container.innerHTML = html;
}

function render(snap) {
  latest = snap;
  els.day.textContent = snap.day;
  // Comes from the server per snapshot, so it names the build actually
  // serving — not whatever build's HTML/JS the browser happens to have cached.
  if (snap.engine_version) els.ver.textContent = "v" + snap.engine_version;
  els.btnPause.textContent = snap.paused ? "Resume" : "Pause";
  els.btnPause.classList.toggle("warn", !snap.paused);

  if (snap.game_over) {
    els.gameOver.textContent = `GAME OVER — ${snap.game_over_reason}`;
    els.gameOver.classList.remove("hidden");
  } else {
    els.gameOver.classList.add("hidden");
  }

  renderPlayers(snap);
  renderIfIdle(els.routes, routesHtml(snap));
  els.fleet.innerHTML = fleetHtml(snap);
  els.crew.innerHTML = crewHtml(snap);
  els.airports.innerHTML = airportsHtml(snap);
  els.log.innerHTML = logHtml(snap);

  const human = snap.players.find((p) => p.player_id === snap.human_player_id);
  if (human) {
    populateSelect(els.tailSelect, human.fleet.filter((a) => !a.retired), "tail_number",
      (a) => `${a.tail_number} (${a.display_name})`);
  }
}

function renderPlayers(snap) {
  els.players.innerHTML = snap.players.map((p) => {
    const isHuman = p.player_id === snap.human_player_id;
    const pax = p.route_ops.reduce((s, o) => s + o.pax, 0);
    const ai = p.ai_profile;
    // Show a rival's strategy and its latest moves: an opponent whose style
    // you can read is one you can actually plan against.
    const style = ai
      ? `<div class="metric"><span class="tag">${esc(ai.archetype)}</span>
           ${esc(ai.blurb)}</div>`
      : "";
    const moves = ai && ai.recent && ai.recent.length
      ? `<details><summary>recent moves</summary>${
          ai.recent.slice().reverse()
            .map((m) => `<div class="logLine">${esc(m)}</div>`).join("")
        }</details>`
      : "";
    return `
      <div class="playerBlock">
        <div class="playerHead">
          <span class="name">${esc(p.name)}</span>
          <span class="tag">${isHuman ? "YOU" : "AI"}</span>
        </div>
        <div class="metric">cash ${money(p.cash)} &middot; debt ${money(p.debt)} &middot;
          net worth <span class="${p.net_worth >= 0 ? "good" : "bad"}">${money(p.net_worth)}</span>
          &middot; ${p.fleet.length} aircraft &middot; ${p.route_ops.length} routes
          &middot; ${pax.toFixed(0)} px/day</div>
        ${style}${moves}
      </div>`;
  }).join("");
}

function routesHtml(snap) {
  const rows = [];
  for (const p of snap.players) {
    const isHuman = p.player_id === snap.human_player_id;
    for (const o of p.route_ops) {
      const warn = !o.suitable
        ? `<span class="reasons">${esc(o.suitability_reasons.join("; "))}</span>`
        : (o.crew_block ? `<span class="warn">${esc(o.crew_block)}</span>` : "");
      rows.push(`<tr>
        <td>${esc(p.name)}</td>
        <td>${o.origin}→${o.dest}</td>
        <td>${esc(o.tail_number)}</td>
        <td>${isHuman
          ? `<input type="number" min="1" step="1" value="${o.ticket_price}" data-op="${o.route_op_id}" data-field="price">`
          : `$${o.ticket_price.toFixed(0)}`}</td>
        <td>${isHuman
          ? `<input type="number" min="0" step="1" value="${o.daily_frequency}" data-op="${o.route_op_id}" data-field="freq">`
          : o.daily_frequency}</td>
        <td>${(o.load_factor * 100).toFixed(0)}%</td>
        <td>${o.pax.toFixed(0)}</td>
        <td class="${o.profit >= 0 ? "good" : "bad"}">${money(o.profit)}</td>
        <td>${warn}</td>
      </tr>`);
    }
  }
  return `<table><thead><tr>
    <th>Carrier</th><th>Route</th><th>Tail</th><th>Price</th><th>Freq</th>
    <th>LF</th><th>Pax</th><th>Profit</th><th></th>
  </tr></thead><tbody>${rows.join("") || emptyRow(9)}</tbody></table>`;
}

function fleetHtml(snap) {
  const rows = [];
  for (const p of snap.players) {
    for (const a of p.fleet) {
      const status = a.retired ? "retired" : (a.in_service ? "in service" : "grounded");
      rows.push(`<tr>
        <td>${esc(p.name)}</td>
        <td>${esc(a.tail_number)}</td>
        <td>${esc(a.display_name)}</td>
        <td>${a.owned ? "owned" : "leased"}</td>
        <td>${a.location_iata}</td>
        <td class="${a.retired ? "bad" : (a.in_service ? "good" : "warn")}">${status}</td>
        <td>${a.airframe_hours.toFixed(0)}h</td>
        <td>${money(a.value)}</td>
      </tr>`);
    }
  }
  return `<table><thead><tr>
    <th>Carrier</th><th>Tail</th><th>Type</th><th>Own</th><th>Loc</th><th>Status</th><th>Hours</th><th>Value</th>
  </tr></thead><tbody>${rows.join("") || emptyRow(8)}</tbody></table>`;
}

function crewGroupHtml(label, units) {
  if (!units.length) return "";
  const total = units.reduce((s, c) => s + c.headcount, 0);
  const resting = units.filter((c) => c.resting).reduce((s, c) => s + c.headcount, 0);
  const locs = {};
  for (const c of units) locs[c.location_iata] = (locs[c.location_iata] || 0) + c.headcount;
  const locStr = Object.entries(locs).map(([k, v]) => `${k}:${v}`).join(" ");
  return `<div class="metric">${label}: ${total} (${resting} resting) — ${locStr}</div>`;
}

function crewHtml(snap) {
  return snap.players.map((p) => `
    <div class="playerBlock">
      <div class="playerHead"><span class="name">${esc(p.name)}</span></div>
      ${crewGroupHtml("Cockpit", p.cockpit_pool)}
      ${crewGroupHtml("Cabin", p.cabin_pool)}
      ${crewGroupHtml("Ground/MX", p.crews)}
    </div>`).join("");
}

function airportsHtml(snap) {
  const rows = Object.entries(snap.airports).map(([iata, a]) => `<tr>
    <td>${esc(iata)}</td>
    <td>${a.gates_used.toFixed(0)}/${a.gates_total}</td>
    <td>${a.fuel_spot != null ? "$" + a.fuel_spot.toFixed(3) + "/L" : "—"}</td>
  </tr>`).join("");
  return `<table><thead><tr><th>IATA</th><th>Gates</th><th>Fuel spot</th></tr></thead>
    <tbody>${rows || emptyRow(3)}</tbody></table>`;
}

function logHtml(snap) {
  const human = snap.players.find((p) => p.player_id === snap.human_player_id);
  if (!human || !human.log.length) return `<div class="metric">no activity yet</div>`;
  return human.log.slice().reverse()
    .map((l) => `<div class="logLine">${esc(l)}</div>`).join("");
}

function emptyRow(cols) {
  return `<tr><td colspan="${cols}" class="metric">none yet</td></tr>`;
}

// -- live updates via SSE ------------------------------------------------
function connect() {
  const es = new EventSource("/api/events");
  es.onopen = () => {
    els.conn.textContent = "live";
    els.conn.classList.remove("offline");
    els.conn.classList.add("online");
  };
  es.onerror = () => {
    els.conn.textContent = "reconnecting…";
    els.conn.classList.remove("online");
    els.conn.classList.add("offline");
  };
  es.onmessage = (evt) => render(JSON.parse(evt.data));
}

// -- controls --------------------------------------------------------------
els.btnPause.addEventListener("click", () => {
  sendControl(latest && !latest.paused ? "pause" : "resume");
});
els.btnAdvance.addEventListener("click", () => sendControl("advance", { days: 1 }));
els.speedRange.addEventListener("input", () => {
  els.speedVal.textContent = els.speedRange.value;
});
els.speedRange.addEventListener("change", () => {
  sendControl("speed", { value: parseFloat(els.speedRange.value) });
});
els.btnSave.addEventListener("click", async () => {
  const res = await postJSON("/api/game/save", {});
  toast(res.ok ? `saved to ${res.path}` : "save failed", !res.ok);
});
els.btnLoad.addEventListener("click", async () => {
  const res = await postJSON("/api/game/load", {});
  if (res.state) render(res.state);
  toast(res.ok ? "loaded" : (res.message || "load failed"), !res.ok);
});
els.btnNew.addEventListener("click", async () => {
  if (!confirm("Start a new game? Current progress will be lost unless saved.")) return;
  const res = await postJSON("/api/game/new", {});
  if (res.state) render(res.state);
  await loadCatalog();
  toast("new game started");
});

els.routes.addEventListener("change", (e) => {
  const t = e.target;
  if (!t.dataset.op) return;
  if (t.dataset.field === "price") {
    sendCommand("set_price", { route_op_id: t.dataset.op, price: parseFloat(t.value) });
  } else if (t.dataset.field === "freq") {
    sendCommand("set_frequency", { route_op_id: t.dataset.op, freq: parseInt(t.value, 10) });
  }
});

els.formOpenRoute.addEventListener("submit", (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  sendCommand("open_route", {
    route_spec_id: f.get("route_spec_id"),
    tail_number: f.get("tail_number"),
    price: parseFloat(f.get("price")),
    freq: parseInt(f.get("freq") || "1", 10),
  });
});

els.formAcquire.addEventListener("submit", (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  sendCommand("acquire_aircraft", {
    spec_id: f.get("spec_id"),
    tail_number: f.get("tail_number"),
    method: f.get("method"),
    base_iata: f.get("base_iata"),
  }).then(() => e.target.reset());
});

els.formHire.addEventListener("submit", (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  sendCommand("hire_crew", {
    crew_type: f.get("crew_type"),
    base_iata: f.get("base_iata"),
    headcount: parseInt(f.get("headcount"), 10),
    cost_per_hour: parseFloat(f.get("cost_per_hour")),
  }).then(() => e.target.reset());
});

// -- boot -------------------------------------------------------------------
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/service-worker.js"));
}

(async function boot() {
  await loadCatalog();
  const state = await fetch("/api/state").then((r) => r.json());
  render(state);
  connect();
})();
