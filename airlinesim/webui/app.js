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
  btnAdvanceH: document.getElementById("btnAdvanceH"),
  hour: document.getElementById("hour"),
  tickHours: document.getElementById("tickHours"),
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
  tailSelect: document.getElementById("tailSelect"),
  specSelect: document.getElementById("specSelect"),
  airportsDL: document.getElementById("airportsDL"),
  hubs: document.getElementById("hubs"),
  formOpenRoute: document.getElementById("formOpenRoute"),
  formAcquire: document.getElementById("formAcquire"),
  formHire: document.getElementById("formHire"),
  formHub: document.getElementById("formHub"),
  alliance: document.getElementById("alliance"),
  mergers: document.getElementById("mergers"),
  formAlliance: document.getElementById("formAlliance"),
  alliancePartner: document.getElementById("alliancePartner"),
  acqPreset: document.getElementById("acqPreset"),
  acqCabin: document.getElementById("acqCabin"),
  btnMapAbout: document.getElementById("btnMapAbout"),
  mapAboutDlg: document.getElementById("mapAboutDlg"),
  newGameDlg: document.getElementById("newGameDlg"),
  ngCash: document.getElementById("ngCash"),
  ngAiCash: document.getElementById("ngAiCash"),
  recabinDlg: document.getElementById("recabinDlg"),
  formRecabin: document.getElementById("formRecabin"),
  recabinTail: document.getElementById("recabinTail"),
  recabinCost: document.getElementById("recabinCost"),
  recabinPreset: document.getElementById("recabinPreset"),
  recabinCabin: document.getElementById("recabinCabin"),
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
  // Everything a fleet decision turns on, in one line per type.
  populateSelect(els.specSelect, catalog.aircraft, "spec_id",
    (a) => `${a.display_name} — ${a.max_seats}st, ` +
           `${(a.max_range_km / 1000).toFixed(1)}kkm, ` +
           `rwy ${a.takeoff_runway_m.toFixed(0)}m, ${a.type_rating || a.manufacturer}, ` +
           `${money(a.list_price)}`);
  // One shared datalist backs every airport input (route endpoints, bases,
  // hubs): type-ahead over the whole corpus instead of a 300-row dropdown.
  els.airportsDL.innerHTML = catalog.airports.map((ap) =>
    `<option value="${esc(ap.iata)}" label="${esc(ap.display_name)}` +
    `${ap.has_mx ? " · MX" : ""}"></option>`).join("");
}

// -- alliances and M&A -----------------------------------------------------
// Both live behind GET /api/mergers, which is read-only and returns a fully
// costed case per rival — rationale, price, synergies, payback, and the reason
// a bid would be refused. Rejected candidates come back WITH their reason
// rather than filtered out, because "why can't I buy them?" is the question
// this panel exists to answer.
let mergerData = null;

async function refreshMergers() {
  mergerData = await fetch("/api/mergers").then((r) => r.json()).catch(() => null);
  renderAlliance();
}

const RATIONALE_HINT = {
  HORIZONTAL: "overlapping networks — duplicate legs consolidate",
  COMPLEMENTARY: "networks barely overlap — each becomes the other's feed",
  SURVIVAL: "neither carrier can compete alone",
  NONE: "no overlap and no new stations",
};

function renderAlliance() {
  if (!mergerData) return;
  const al = mergerData.alliance;
  const partners = (al?.partners || []).map((id) => {
    const p = latest?.players.find((x) => x.player_id === id);
    return p ? p.name : id;
  });
  els.alliance.innerHTML = al
    ? `<div class="metric"><span class="tag">${esc(al.kind)}</span>
         <b>${esc(al.name)}</b> with ${partners.map(esc).join(", ") || "nobody"}
         &middot; partner feed x${al.feed_efficiency}
         &middot; dues ${money(al.dues_per_day)}/day</div>
       <div class="metric">coordinated hubs:
         ${al.no_compete_hubs.length ? al.no_compete_hubs.map(esc).join(", ") : "none"}
         <input id="ncHub" placeholder="IATA" size="4" autocomplete="off">
         <button class="btn small" data-act="addhub">Coordinate</button>
         <button class="btn small warn" data-act="leave">Leave alliance</button></div>`
    : `<div class="metric">Not in an alliance. Only your own onward flights
         feed your connecting traffic.</div>`;

  // The partner picker only offers carriers not already in an alliance.
  const taken = new Set((mergerData.alliances || []).flatMap((a) => a.members));
  const free = (mergerData.candidates || []).filter((c) => !taken.has(c.player_id));
  els.alliancePartner.innerHTML = free.length
    ? free.map((c) => `<option value="${esc(c.player_id)}">${esc(c.name)}</option>`).join("")
    : `<option value="">no unallied carrier</option>`;

  const pos = mergerData.cannot_compete_alone
    ? `<div class="warn">You hold ${(mergerData.my_share * 100).toFixed(0)}% of
         departures against a leader on ${(mergerData.leader_share * 100).toFixed(0)}% —
         by the survival test you cannot compete alone.</div>`
    : "";

  const rows = (mergerData.candidates || []).map((c) => {
    const pay = c.payback_years == null ? "never" : `${c.payback_years}y`;
    const act = c.approved
      ? `<button class="btn small" data-act="acquire" data-target="${esc(c.player_id)}">Acquire</button>`
      : `<button class="btn small warn" data-act="force" data-target="${esc(c.player_id)}"
                 title="the valuation says no — buy anyway">Override</button>`;
    return `<tr>
      <td>${esc(c.name)}</td>
      <td>${c.fleet}/${c.routes}</td>
      <td>${money(c.enterprise_value)}</td>
      <td>${money(c.total_outlay)}</td>
      <td>${money(c.annual_synergy)}/yr</td>
      <td>${pay}</td>
      <td><span class="tag" title="${esc(RATIONALE_HINT[c.rationale] || "")}">${esc(c.rationale)}</span></td>
      <td class="${c.approved ? "good" : "warn"}">${esc(c.reason)}</td>
      <td>${act}</td>
    </tr>`;
  }).join("");

  els.mergers.innerHTML = `${pos}
    <table><thead><tr>
      <th>Carrier</th><th>Fleet/Routes</th><th>Value</th><th>Cost</th>
      <th>Synergy</th><th>Payback</th><th>Rationale</th><th>Verdict</th><th></th>
    </tr></thead><tbody>${rows || emptyRow(9)}</tbody></table>
    <div class="metric">Cost is the price plus integration. You hold
      ${money(mergerData.cash)}. A merger transfers fleet, routes, crews, hubs
      <b>and debt</b>, and consolidates duplicated legs.</div>`;
}

els.alliance.addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  if (b.dataset.act === "leave") {
    if (confirm("Leave the alliance? Your partners' onward flights stop feeding your routes.")) {
      await sendCommand("leave_alliance", {});
      refreshMergers();
    }
  } else if (b.dataset.act === "addhub") {
    const iata = String(document.getElementById("ncHub").value || "").trim().toUpperCase();
    if (!iata) return;
    await sendCommand("set_no_compete_hub", { iata, enabled: true });
    refreshMergers();
  }
});

els.mergers.addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  const c = (mergerData?.candidates || []).find((x) => x.player_id === b.dataset.target);
  if (!c) return;
  const force = b.dataset.act === "force";
  const warn = force
    ? `\n\nThe valuation REJECTS this deal: ${c.reason}.\nBuy anyway?`
    : "";
  if (!confirm(`Acquire ${c.name} for ${money(c.total_outlay)}?\n\n` +
               `${c.rationale} — ${c.reason}\n` +
               `Synergy ${money(c.annual_synergy)}/yr, payback ` +
               `${c.payback_years == null ? "never" : c.payback_years + "y"}.\n` +
               `You take on ${money(c.debt)} of their debt.${warn}`)) return;
  await sendCommand("acquire_carrier", { target_id: c.player_id, force });
  refreshMergers();
});

els.formAlliance.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const partner = String(f.get("partner") || "");
  await sendCommand("form_alliance", {
    name: f.get("name"), kind: f.get("kind"),
    partners: partner ? [partner] : [],
  });
  e.target.reset();
  refreshMergers();
});

// -- cabin planner ---------------------------------------------------------
// Every seat-count field on the page is driven from here, and every number it
// shows comes from GET /api/cabin — i.e. from the same fitter the acquire and
// recabin commands run. The browser deliberately owns NO geometry of its own:
// a preview that disagreed with the installed cabin would be worse than no
// preview at all.
const CABIN_FIELDS = ["first", "business", "premium", "economy"];
const CABIN_SHORT = { FIRST: "F", BUSINESS: "J", PREMIUM: "W", ECONOMY: "Y" };

function seatsFromInputs(inputs) {
  const seats = {};
  for (const [cls, el] of Object.entries(inputs)) {
    const v = String(el.value).trim();
    if (v !== "" && Number(v) > 0) seats[cls] = parseInt(v, 10);
  }
  return seats;
}

async function fetchCabinFit(specId, seats) {
  const qs = new URLSearchParams({ spec_id: specId });
  for (const [k, v] of Object.entries(seats)) qs.set(k, v);
  return fetch(`/api/cabin?${qs}`).then((r) => r.json()).catch(() => null);
}

function cabinPlanHtml(fit) {
  if (!fit) return "";
  if (fit.error) return `<span class="bad">${esc(fit.error)}</span>`;
  const plan = Object.entries(fit.seats)
    .map(([c, n]) => `<b>${n}</b>${CABIN_SHORT[c] || c[0]}`).join(" + ");
  const g = fit.geometry;
  const pct = Math.min(100, (fit.length_used_m / fit.cabin_length_m) * 100);
  const notes = (fit.notes || []).length
    ? `<div class="warn">${esc(fit.notes.join("; "))}</div>` : "";
  // How much bigger each cabin could be alongside this plan. Shown rather
  // than enforced as an input max: a `max` attribute makes the browser refuse
  // to submit an over-large number, which would replace the fitter's "here is
  // what fits and why" with a bare tooltip.
  const room = ["FIRST", "BUSINESS", "PREMIUM", "ECONOMY"]
    .map((c) => `${CABIN_SHORT[c]} up to ${fit.max_with_plan[c]}`).join(" &middot; ");
  // What each premium seat costs in economy seats is the whole trade-off, so
  // it's on screen rather than left to be discovered through lost revenue.
  const cost = Object.entries(g.classes)
    .filter(([c]) => c !== "ECONOMY")
    .map(([c, d]) => `${CABIN_SHORT[c]} ${d.footprint}Y (${d.abreast}-abreast, ${d.pitch_in}")`)
    .join(" &middot; ");
  return `
    <div class="cabinLine"><b>${fit.total_seats}</b> seats: ${plan || "—"}</div>
    <div class="cabinBar"><span style="width:${pct.toFixed(1)}%"></span></div>
    <div class="metric">${fit.length_used_m}m of ${fit.cabin_length_m}m cabin used
      &middot; ${g.abreast_economy}-abreast economy${
        g.abreast_source === "estimated" ? " (estimated)" : ""}</div>
    <div class="metric">room for: ${room}</div>
    <div class="metric">seat cost: ${cost}</div>${notes}`;
}

// Wires a set of seat inputs + a preset picker + a preview panel together.
// Returns a refresh() the caller can trigger when the aircraft changes.
function bindCabinPlanner({ inputs, preset, panel, specId }) {
  let current = specId;
  let pending = null;

  async function refresh() {
    if (!current) { panel.innerHTML = ""; return; }
    const fit = await fetchCabinFit(current, seatsFromInputs(inputs));
    panel.innerHTML = cabinPlanHtml(fit);
  }

  function schedule() {
    clearTimeout(pending);
    pending = setTimeout(refresh, 150);
  }

  for (const el of Object.values(inputs)) el.addEventListener("input", schedule);

  if (preset) {
    preset.addEventListener("change", () => {
      const spec = (catalog?.aircraft || []).find((s) => s.spec_id === current);
      const plan = spec?.cabin_presets?.[preset.value];
      if (!plan) return;
      for (const [cls, el] of Object.entries(inputs)) {
        el.value = plan[cls.toUpperCase()] || "";
      }
      refresh();
    });
  }

  return {
    refresh,
    setSpec(id) {
      current = id;
      if (preset) {
        const spec = (catalog?.aircraft || []).find((s) => s.spec_id === id);
        preset.innerHTML = `<option value="">cabin plan…</option>` +
          Object.keys(spec?.cabin_presets || {})
            .map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
      }
      refresh();
    },
    seats: () => seatsFromInputs(inputs),
    clear() {
      for (const el of Object.values(inputs)) el.value = "";
      refresh();
    },
  };
}

function cabinInputs(scope) {
  const out = {};
  for (const c of CABIN_FIELDS) out[c] = scope.querySelector(`[data-cabin="${c}"]`);
  return out;
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
  if (snap.hour != null) {
    els.hour.textContent = String(snap.hour).padStart(2, "0") + ":00";
  }
  // The controls reflect the SERVER's clock, not whatever the sliders were
  // left at: a loaded save or a second browser tab has to show the truth.
  if (snap.speed != null && document.activeElement !== els.speedRange) {
    els.speedRange.value = snap.speed;
    els.speedVal.textContent = String(Math.round(snap.speed));
  }
  if (snap.tick_hours != null && document.activeElement !== els.tickHours) {
    els.tickHours.value = String(snap.tick_hours);
  }
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

  // The clock detected the machine being asleep and paused rather than
  // fast-forwarding through it. Say so — the alternative is a player finding
  // the game silently stopped and assuming it crashed. Cleared by Resume,
  // server-side, so it survives a page reload.
  const notice = document.getElementById("clockNotice");
  if (notice) {
    notice.textContent = snap.clock_notice || "";
    notice.classList.toggle("hidden", !snap.clock_notice);
  }

  renderStart(snap);
  renderPlayers(snap);
  renderIfIdle(els.routes, routesHtml(snap));
  renderIfIdle(els.fleet, fleetHtml(snap));
  els.hubs.innerHTML = hubsHtml(snap);
  els.crew.innerHTML = crewHtml(snap);
  els.airports.innerHTML = airportsHtml(snap);
  els.log.innerHTML = logHtml(snap);

  if (typeof drawLive === "function" && MAP.svg) {
    drawLive(snap);
    applySelection();
  }

  const human = snap.players.find((p) => p.player_id === snap.human_player_id);
  if (human) {
    populateSelect(els.tailSelect, human.fleet.filter((a) => !a.retired), "tail_number",
      (a) => `${a.tail_number} (${a.display_name})`);
  }
}

// A player now starts with cash and nothing else, so the first screen has to
// say what to do. Disappears as soon as they have a fleet and a route.
function renderStart(snap) {
  const el = document.getElementById("startHint");
  if (!el) return;
  const me = snap.players.find((p) => p.player_id === snap.human_player_id);
  if (!me) return;
  const steps = [];
  if (!me.fleet.length) {
    steps.push("<b>Lease an aircraft</b> — open <i>Fleet &rarr; Buy / finance / lease</i>. " +
               "Leasing costs no capital up front, so it's the usual way to start.");
  }
  if (!me.hubs.length) {
    steps.push("<b>Open a hub</b> — it's the only place your aircraft can be " +
               "maintained, and it buys you preferential gates there.");
  }
  if (me.fleet.length && !me.route_ops.length) {
    steps.push("<b>Open a route</b> — any pair of the " +
               ((catalog && catalog.airports.length) || "300") +
               " airports is legal, as long as your aircraft has the range and " +
               "the runways are long enough.");
  }
  if (!steps.length) {
    el.classList.add("hidden");
    return;
  }
  el.classList.remove("hidden");
  el.innerHTML = `<h2>Getting started</h2><ol>${
    steps.map((s) => `<li>${s}</li>`).join("")}</ol>
    <div class="metric">Your rivals each start from a single route and build
      from there. The clock is paused until you press Resume.</div>`;
}

function renderPlayers(snap) {
  els.players.innerHTML = snap.players.map((p) => {
    const isHuman = p.player_id === snap.human_player_id;
    const pax = p.route_ops.reduce((s, o) => s + o.pax_per_day, 0);
    const ai = p.ai_profile;
    // Show a rival's strategy and its latest moves: an opponent whose style
    // you can read is one you can actually plan against.
    const STAGE = {
      healthy: ["good", "healthy"], freeze: ["warn", "expansion frozen"],
      cut: ["warn", "cutting routes"], shed: ["bad", "returning aircraft"],
    };
    const st = ai && STAGE[ai.stage];
    const style = ai
      ? `<div class="metric"><span class="tag">${esc(ai.archetype)}</span>
           ${esc(ai.blurb)}</div>
         <div class="metric">cash flow
           <span class="${ai.cash_flow_per_day >= 0 ? "good" : "bad"}">${
             money(ai.cash_flow_per_day)}/day</span>${
           st ? ` &middot; <span class="${st[0]}">${st[1]}</span>` : ""}</div>`
      : "";
    const moves = ai && ai.recent && ai.recent.length
      ? `<details><summary>recent moves</summary>${
          ai.recent.slice().reverse()
            .map((m) => `<div class="logLine">${esc(m)}</div>`).join("")
        }</details>`
      : "";
    // the swatch is the map's colour for this carrier, so the two views are
    // reading the same key
    const swatch = typeof carrierColor === "function"
      ? `<i class="swatch" style="background:${carrierColor(snap, p.player_id)}"></i>` : "";
    return `
      <div class="playerBlock">
        <div class="playerHead">
          ${swatch}<span class="name">${esc(p.name)}</span>
          <span class="tag">${isHuman ? "YOU" : "AI"}</span>
        </div>
        <div class="metric">cash ${money(p.cash)} &middot; debt ${money(p.debt)} &middot;
          net worth <span class="${p.net_worth >= 0 ? "good" : "bad"}">${money(p.net_worth)}</span>
          &middot; ${p.fleet.length} aircraft &middot; ${p.route_ops.length} routes
          &middot; ${pax.toFixed(0)} px/day</div>
        ${disruptionLine(p)}
        ${style}${moves}
      </div>`;
  }).join("");
}

// What the weather has cost this carrier, cumulatively. Everything here is a
// real ledger entry the engine charged — hotels, meals, compensation and crew
// hotels are separate lines in DisruptionCosts, not one lumped estimate.
function disruptionLine(p) {
  const d = p.disruption;
  if (!d || (!d.cancelled_flights && !d.stranded_pax && !d.total_cost)) return "";
  const parts = [];
  if (d.cancelled_flights) parts.push(`${d.cancelled_flights.toFixed(0)} flights cancelled`);
  if (d.delay_hours) parts.push(`${d.delay_hours.toFixed(0)}h delay`);
  if (d.stranded_pax) {
    parts.push(`${d.stranded_pax} stranded (${d.rebooked_pax} rebooked, ` +
               `${d.refunded_pax} refunded)`);
  }
  if (d.total_cost) parts.push(`<span class="bad">${money(d.total_cost)}</span> disruption cost`);
  return `<div class="metric">weather: ${parts.join(" &middot; ")}</div>`;
}

const TIER_LABEL = { 1: "Basic", 2: "Standard", 3: "Premium" };

function cabinStr(cabin) {
  if (!cabin) return "all-econ";
  const short = { ECONOMY: "Y", PREMIUM: "W", BUSINESS: "J", FIRST: "F" };
  return Object.entries(cabin).map(([c, n]) => `${n}${short[c] || c[0]}`).join(" ");
}

function routesHtml(snap) {
  const rows = [];
  for (const p of snap.players) {
    const isHuman = p.player_id === snap.human_player_id;
    for (const o of p.route_ops) {
      const warn = !o.suitable
        ? `<span class="reasons">${esc(o.suitability_reasons.join("; "))}</span>`
        : (o.crew_block ? `<span class="warn">${esc(o.crew_block)}</span>` : "");
      const tierCell = isHuman
        ? `<select data-op="${o.route_op_id}" data-field="tier">` +
          [1, 2, 3].map((t) =>
            `<option value="${t}" ${t === o.service_tier ? "selected" : ""}>` +
            `${TIER_LABEL[t]}</option>`).join("") + `</select>`
        : TIER_LABEL[o.service_tier] || o.service_tier;
      rows.push(`<tr data-rowop="${o.route_op_id}" data-rowtail="${esc(o.tail_number)}">
        <td>${esc(p.name)}</td>
        <td>${o.origin}→${o.dest}${o.data_tier && o.data_tier !== "exact"
          ? ` <span class="metric" title="demand is a ${o.data_tier} estimate, not measured">~</span>` : ""}</td>
        <td>${esc(o.tail_number)}</td>
        <td>${isHuman
          ? `<input type="number" min="1" step="1" value="${o.ticket_price}" data-op="${o.route_op_id}" data-field="price">`
          : `$${o.ticket_price.toFixed(0)}`}</td>
        <td>${isHuman
          ? `<input type="number" min="0" step="1" value="${o.daily_frequency}" data-op="${o.route_op_id}" data-field="freq">`
          : o.daily_frequency}</td>
        <td>${tierCell}</td>
        <td>${(o.load_factor * 100).toFixed(0)}%</td>
        <td>${o.pax_per_day.toFixed(0)}</td>
        <td class="${o.profit_per_day >= 0 ? "good" : "bad"}">${money(o.profit_per_day)}</td>
        <td>${weatherCell(o)}</td>
        <td>${isHuman
          ? `<button class="btn small warn" data-op="${o.route_op_id}" data-act="close">Close</button>`
          : ""}</td>
        <td>${warn}</td>
      </tr>`);
      const cabins = cabinFareRow(o, isHuman);
      if (cabins) rows.push(cabins);
    }
  }
  return `<table><thead><tr>
    <th>Carrier</th><th>Route</th><th>Tail</th><th>Price</th><th>Freq</th><th>Service</th>
    <th>LF</th>
    <th title="passengers a day — the same number whatever the detail setting">Pax/day</th>
    <th title="contribution margin a day: revenue less this flight's fuel, crew and fees. Excludes lease rent, loan service, payroll and hub overhead">Profit/day</th>
    <th title="what the weather is doing to this route right now">Wx</th>
    <th></th><th></th>
  </tr></thead><tbody>${rows.join("") || emptyRow(12)}</tbody></table>`;
}

// The map draws weather; this says what it COST. Capacity lost, delay added
// and frequencies cancelled are all on the op — without them the map is
// scenery and a route quietly under-performing has no visible cause.
function weatherCell(o) {
  const cap = o.weather_capacity == null ? 1 : o.weather_capacity;
  const delay = o.weather_delay_h || 0;
  const cancelled = o.weather_cancelled || 0;
  if (!o.weather && cap >= 0.999 && delay <= 0.005 && cancelled <= 0.005) return "";
  const bits = [];
  if (cap < 0.999) bits.push(`cap ${(cap * 100).toFixed(0)}%`);
  if (delay > 0.005) bits.push(`+${delay.toFixed(1)}h`);
  if (cancelled > 0.005) bits.push(`${cancelled.toFixed(1)} cx`);
  const bad = cap < 0.75 || cancelled > 0.005;
  return `<span class="${bad ? "bad" : "warn"}" title="${esc(o.weather || "")}">` +
         `${esc(o.weather || "weather")}</span>` +
         (bits.length ? ` <span class="metric">${bits.join(" · ")}</span>` : "");
}

// Per-cabin pricing, for the cabins the ASSIGNED aircraft actually has. A
// cabin left unpriced follows the base fare times its class multiplier, and
// says so — so the row shows what every seat on the aeroplane is selling for,
// not one number standing in for four different products.
function cabinFareRow(o, isHuman) {
  const cabins = o.cabins || [];
  if (cabins.length < 2) return "";
  const cells = cabins.map((c) => {
    const lf = (c.load_factor * 100).toFixed(0);
    const fare = isHuman
      ? `<input type="number" min="0" step="1" value="${c.priced ? Math.round(c.fare) : ""}"
                placeholder="${Math.round(c.default_fare)}"
                data-op="${o.route_op_id}" data-cabin-price="${c.cabin}"
                title="blank follows the base fare (${money(c.default_fare)})">`
      : `$${c.fare.toFixed(0)}`;
    return `<span class="cabinFare ${c.priced ? "priced" : ""}">
       <b>${CABIN_SHORT[c.cabin] || c.cabin[0]}</b> ${fare}
       <span class="metric">${c.seats}st &middot; ${lf}% &middot; ${money(c.revenue_per_day)}/d</span>
     </span>`;
  }).join("");
  // spans every column but the carrier name — keep in step with routesHtml's
  // header when a column is added
  return `<tr class="cabinRow"><td></td><td colspan="11">${cells}</td></tr>`;
}

function fleetHtml(snap) {
  const rows = [];
  for (const p of snap.players) {
    const isHuman = p.player_id === snap.human_player_id;
    for (const a of p.fleet) {
      const recabining = a.reconfiguring_until > snap.sim_time_hours;
      const status = a.retired ? "retired"
        : recabining ? "recabin"
        : (a.in_service ? "in service" : "grounded");
      const actions = isHuman && !a.retired
        ? `<button class="btn small" data-tail="${esc(a.tail_number)}" data-act="${a.owned ? "sell" : "return"}">
             ${a.owned ? "Sell" : "Return"}</button>
           <button class="btn small" data-tail="${esc(a.tail_number)}" data-act="recabin">Recabin</button>`
        : "";
      rows.push(`<tr data-rowtail="${esc(a.tail_number)}">
        <td>${esc(p.name)}</td>
        <td>${esc(a.tail_number)}</td>
        <td>${esc(a.display_name)}</td>
        <td>${a.owned ? "owned" : "leased"}</td>
        <td>${cabinStr(a.cabin)}</td>
        <td>${a.location_iata}</td>
        <td class="${a.retired ? "bad" : (a.in_service && !recabining ? "good" : "warn")}">${status}</td>
        <td>${a.airframe_hours.toFixed(0)}h</td>
        <td>${money(a.value)}</td>
        <td>${actions}</td>
      </tr>`);
    }
  }
  return `<table><thead><tr>
    <th>Carrier</th><th>Tail</th><th>Type</th><th>Own</th><th>Cabin</th><th>Loc</th><th>Status</th><th>Hours</th><th>Value</th><th></th>
  </tr></thead><tbody>${rows.join("") || emptyRow(10)}</tbody></table>`;
}

// Crew states, in the order they stack in the bar. Ordered by operational
// salience rather than alphabetically: what a player needs to see first is
// how much of the payroll is working, and how much is stuck somewhere useless.
const CREW_STATES = [
  ["flying", "flying", "crew rostered to a departure this tick"],
  ["ready", "ready", "at base, rested, in hours, unassigned — available now"],
  ["resting", "resting", "mid-rest: illegal to assign until the rest is banked"],
  ["capped", "out of hours", "at work but out of duty hours — the daily, 7-day "
    + "or 28-day cap. Hiring more crew is the only fix; rest won't clear it today"],
  ["away", "away", "at another airport with nothing to do — positioning is "
    + "direct-to-base only, so these are idle until they deadhead home"],
];

// The panel exists because a crew shortage is nearly always a DISTRIBUTION
// problem, not a headcount one: an airline can hold fifty idle crew at its
// hub and still cancel a departure at a station where it based nobody. The
// old panel printed one line of "ORD:12 DFW:8" per crew type, which showed
// the headcount and hid the shortage. This shows both, and puts the bases
// that cannot crew their own flying at the top.
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// Cockpit and cabin are rostered independently, so a base can be flush with
// one and short of the other — and a departure needs both. That split doesn't
// deserve its own bar, but it is exactly what you want when you hover the one
// base that is failing.
function crewTypeLines(b) {
  return Object.entries(b.by_type || {}).map(([t, v]) => {
    const parts = CREW_STATES.filter(([k]) => v[k])
      .map(([k, label]) => `${v[k]} ${label}`).join(", ");
    return `${t}: ${v.headcount} — ${parts}`;
  }).join("\n");
}

function crewBaseRow(b) {
  const hc = b.headcount;
  const seg = CREW_STATES.map(([k, label, tip]) => {
    if (!b[k]) return "";
    const pct = (100 * b[k]) / hc;
    return `<span class="crewSeg ${k}" style="width:${pct.toFixed(2)}%"
                  title="${b[k]} ${label} — ${esc(tip)}"></span>`;
  }).join("");
  const counts = CREW_STATES.filter(([k]) => b[k])
    .map(([k, label]) => `<span class="${k}">${b[k]} ${label}</span>`).join(" · ");
  // A blocked departure is the failure this panel is for, and the diagnosis
  // differs: crew sitting idle on the field means the legality gate refused
  // them, no crew present means the station was never staffed or positioned.
  let flag = "";
  if (b.blocked) {
    flag = b.present
      ? `<span class="bad" title="crew are on this field but none could be legally rostered — check rest and duty">
           ${b.blocked}/${b.demand} departures uncrewed, ${b.present} on the field</span>`
      : `<span class="bad" title="nobody is at this airport to fly them">
           ${b.blocked}/${b.demand} departures uncrewed, nobody here</span>`;
  }
  const rest = b.resting
    ? `<span class="metric" title="mean progress through the mandatory rest">
         rest ${Math.round(100 * b.rest_frac)}%</span>` : "";
  // "based" and "here" are the two halves of the distribution question, so
  // they sit side by side — and only differ when crew are out of position.
  // Unbased crew (maintenance) are at no airport by definition, so showing
  // them "0 here" would read as a positioning failure they can't have.
  const unbased = b.iata === "(unbased)";
  const here = (!unbased && b.present !== hc)
    ? `<span class="metric" title="crew physically at this airport right now, whoever they are based with">
         · ${b.present} here</span>` : "";
  return `<div class="crewBase">
    <div class="crewBaseHead">
      <span class="iata">${esc(b.iata)}</span>
      ${b.is_hub ? '<span class="tag">hub</span>' : ""}
      <span class="metric">${hc} ${unbased ? "crew" : "based"}</span>${here}
      ${b.demand ? `<span class="metric">· ${b.demand} dep/day</span>` : ""}
      ${rest}${flag}
    </div>
    <div class="crewBar" title="${hc} crew based at ${esc(b.iata)}
${crewTypeLines(b)}">${seg}</div>
    <div class="crewCounts">${counts}</div>
  </div>`;
}

// Stations the airline FLIES from but bases nobody at. Normal for an
// out-and-back — the crew arrives, turns and goes home — so these are one
// compact line rather than a card each, and only the ones that actually
// failed to crew a departure are called out.
function crewStationsHtml(stations) {
  if (!stations.length) return "";
  const bits = stations.map((b) => {
    const cls = b.blocked ? "bad" : "metric";
    const mark = b.blocked ? `${b.blocked} uncrewed` : `${b.present} here`;
    return `<span class="${cls}"
      title="${esc(b.iata)}: ${plural(b.demand, "departure")} a day, no crew based here — ${mark}"
      >${esc(b.iata)} <span class="metric">${mark}</span></span>`;
  }).join(" · ");
  return `<div class="crewStations metric" title="stations flown from with no crew based there">
    flown, not based: ${bits}</div>`;
}

function crewHtml(snap) {
  return snap.players.map((p) => {
    const all = Object.values(p.crew_bases || {});
    if (!all.length) {
      return `<div class="playerBlock">
        <div class="playerHead"><span class="name">${esc(p.name)}</span></div>
        <div class="metric">No crew employed.</div></div>`;
    }
    const bases = all.filter((b) => b.headcount > 0);
    const stations = all.filter((b) => !b.headcount);
    // Uncrewed departures first, then by headcount, so the row you need to
    // act on is never below the fold.
    bases.sort((a, b) => (b.blocked - a.blocked) || (b.headcount - a.headcount));
    stations.sort((a, b) => (b.blocked - a.blocked) || (b.demand - a.demand));
    const tot = bases.reduce((s, b) => s + b.headcount, 0);
    const idle = bases.reduce((s, b) => s + b.ready + b.away, 0);
    const away = bases.reduce((s, b) => s + b.away, 0);
    const blocked = all.reduce((s, b) => s + b.blocked, 0);
    return `<div class="playerBlock">
      <div class="playerHead">
        <span class="name">${esc(p.name)}</span>
        <span class="metric">${tot} crew across ${plural(bases.length, "base")}
          · ${idle} idle${away ? `, ${away} out of position` : ""}${blocked
            ? ` · <span class="bad">${plural(blocked, "uncrewed departure")}</span>` : ""}</span>
      </div>
      <div class="crewGrid">${bases.map(crewBaseRow).join("")}</div>
      ${crewStationsHtml(stations)}
    </div>`;
  }).join("");
}

// Reliability is the cumulative record — the "this hub costs you every winter"
// number the weather work exists to produce. Blank until an airport has
// actually been disrupted, so a clear-weather world isn't full of 100%s.
function airportsHtml(snap) {
  const anyWx = Object.values(snap.airports).some(
    (a) => (a.weather && a.weather.kind) || (a.reliability && a.reliability.disrupted_hours));
  const rows = Object.entries(snap.airports).map(([iata, a]) => {
    const wx = a.weather || {}, rel = a.reliability || {};
    const now = wx.kind
      ? `<span class="${wx.closed ? "bad" : "warn"}">${esc(wx.text || wx.kind)}</span>`
      : "";
    const rec = rel.reliability != null && rel.disrupted_hours
      ? `${(rel.reliability * 100).toFixed(0)}%<span class="metric"> · ` +
        `${rel.cancelled_flights.toFixed(0)} cx · ${money(rel.cost)}` +
        (rel.worst && rel.worst !== "CLEAR"
          ? ` · worst ${esc(rel.worst.replace(/_/g, " ").toLowerCase())}` : "") +
        `</span>`
      : "";
    return `<tr>
      <td>${esc(iata)}</td>
      <td>${a.gates_used.toFixed(0)}/${a.gates_total}</td>
      <td>${a.fuel_spot != null ? "$" + a.fuel_spot.toFixed(3) + "/L" : "—"}</td>
      ${anyWx ? `<td>${now}</td><td>${rec}</td>` : ""}
    </tr>`;
  }).join("");
  return `<table><thead><tr><th>IATA</th><th>Gates</th><th>Fuel spot</th>
    ${anyWx ? `<th>Sky</th><th title="share of elapsed time this field has been
      operating normally, with what the disruption has cost">Reliability</th>` : ""}
  </tr></thead><tbody>${rows || emptyRow(anyWx ? 5 : 3)}</tbody></table>`;
}

function hubsHtml(snap) {
  const human = snap.players.find((p) => p.player_id === snap.human_player_id);
  if (!human) return "";
  const fee = (iata) => {
    const ap = (catalog?.airports || []).find((x) => x.iata === iata);
    return ap ? ` ${money(ap.hub_fee_per_day)}/day` : "";
  };
  const chips = (human.hubs || []).map((h) =>
    `<span class="hubChip">${esc(h)}<span class="metric">${fee(h)}</span>
       <button class="btn small warn" data-hub="${esc(h)}" data-act="closehub"
               title="close this hub">×</button></span>`).join(" ");
  return chips || `<div class="metric">No hubs yet — without one your aircraft
    can still use any maintenance field, but you get no preferential gates
    anywhere.</div>`;
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
els.btnAdvance.addEventListener("click", () => sendControl("advance", { hours: 24 }));
els.btnAdvanceH.addEventListener("click", () => sendControl("advance", { hours: 6 }));
els.tickHours.addEventListener("change", () => {
  sendControl("resolution", { value: parseFloat(els.tickHours.value) });
});
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
els.btnNew.addEventListener("click", () => els.newGameDlg.showModal());

// Blank stays BLANK on the wire — the server reads "" as "auto-size" and
// drops the kwarg, so the builder's own sizing runs. Sending 0 instead would
// look like a deliberate choice of zero.
els.newGameDlg.addEventListener("close", async () => {
  if (els.newGameDlg.returnValue !== "go") return;
  const res = await postJSON("/api/game/new", {
    cash: els.ngCash.value,
    ai_cash: els.ngAiCash.value,
  });
  if (res.state) render(res.state);
  await loadCatalog();
  toast("new game started");
});

// What the map is and isn't — one click away rather than five lines of prose
// under it. The orientation line inside is written by map.js's northNote().
if (els.btnMapAbout && els.mapAboutDlg) {
  els.btnMapAbout.addEventListener("click", () => els.mapAboutDlg.showModal());
}

els.routes.addEventListener("change", (e) => {
  const t = e.target;
  if (!t.dataset.op) return;
  if (t.dataset.cabinPrice) {
    // blank clears the override and hands the cabin back to the base fare
    sendCommand("set_cabin_price", {
      route_op_id: t.dataset.op, cabin: t.dataset.cabinPrice,
      price: t.value.trim() === "" ? null : parseFloat(t.value),
    });
  } else if (t.dataset.field === "price") {
    sendCommand("set_price", { route_op_id: t.dataset.op, price: parseFloat(t.value) });
  } else if (t.dataset.field === "freq") {
    sendCommand("set_frequency", { route_op_id: t.dataset.op, freq: parseInt(t.value, 10) });
  } else if (t.dataset.field === "tier") {
    sendCommand("set_service_tier", { route_op_id: t.dataset.op, tier: parseInt(t.value, 10) });
  }
});

// Route close + fleet lifecycle, via delegation so re-renders don't detach
// anything. Recabin asks for the new layout in one line — crude but honest
// about what it costs, since the confirm() states price and downtime.
els.routes.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-act='close']");
  if (!b) return;
  if (confirm("Close this route?")) {
    sendCommand("close_route", { route_op_id: b.dataset.op });
  }
});

els.fleet.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  const tail = b.dataset.tail;
  if (b.dataset.act === "sell") {
    if (confirm(`Sell ${tail}? Its routes close and any loan is paid off from the proceeds.`)) {
      sendCommand("sell_aircraft", { tail_number: tail });
    }
  } else if (b.dataset.act === "return") {
    if (confirm(`Return ${tail} to the lessor early? You pay the termination penalty and its routes close.`)) {
      sendCommand("break_lease", { tail_number: tail });
    }
  } else if (b.dataset.act === "recabin") {
    openRecabin(tail);
  }
});

// -- recabin dialog ---------------------------------------------------------
let recabinTail = null;
const recabinPlanner = bindCabinPlanner({
  inputs: cabinInputs(els.formRecabin),
  preset: els.recabinPreset,
  panel: els.recabinCabin,
  specId: null,
});

function openRecabin(tail) {
  const human = latest?.players.find((p) => p.player_id === latest.human_player_id);
  const plane = human?.fleet.find((x) => x.tail_number === tail);
  if (!plane) return;
  const spec = (catalog?.aircraft || []).find((s) => s.spec_id === plane.spec_id);
  recabinTail = tail;
  els.recabinTail.textContent = `${tail} (${plane.display_name})`;
  els.recabinCost.innerHTML = spec
    ? `Costs <b>${money(spec.reconfig_cost)}</b> and grounds the aircraft for
       <b>${spec.reconfig_days} days</b>. Any route flying it keeps running once it's back.`
    : "";
  // Start from the cabin it has now, so a small change is a small edit — but
  // economy goes in the PLACEHOLDER, not the value. Typed in, it pins economy
  // and every premium seat you then add has to be trimmed back out of it;
  // left blank, it refills itself around whatever you choose.
  const current = plane.cabin || { ECONOMY: spec ? spec.max_seats : 0 };
  const inputs = cabinInputs(els.formRecabin);
  for (const [cls, el] of Object.entries(inputs)) {
    const now = current[cls.toUpperCase()] || 0;
    if (cls === "economy") {
      el.value = "";
      el.placeholder = now ? `economy (now ${now})` : "economy";
    } else {
      el.value = now || "";
    }
  }
  recabinPlanner.setSpec(plane.spec_id);
  els.recabinDlg.showModal();
}

els.recabinDlg.addEventListener("close", () => {
  if (els.recabinDlg.returnValue !== "go" || !recabinTail) return;
  sendCommand("reconfigure_aircraft", {
    tail_number: recabinTail, seats: recabinPlanner.seats(),
  });
  recabinTail = null;
});

els.formHub.addEventListener("submit", (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  sendCommand("set_hub", {
    iata: String(f.get("iata") || "").trim().toUpperCase(), enabled: true,
  }).then(() => e.target.reset());
});

els.hubs.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-act='closehub']");
  if (!b) return;
  if (confirm(`Close the ${b.dataset.hub} hub? You lose its preferential gates and maintenance there.`)) {
    sendCommand("set_hub", { iata: b.dataset.hub, enabled: false });
  }
});

els.formOpenRoute.addEventListener("submit", (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const origin = String(f.get("origin") || "").trim().toUpperCase();
  const dest = String(f.get("dest") || "").trim().toUpperCase();
  sendCommand("open_route", {
    route_spec_id: `${origin}-${dest}`,
    tail_number: f.get("tail_number"),
    price: parseFloat(f.get("price")),
    freq: parseInt(f.get("freq") || "1", 10),
    service_tier: parseInt(f.get("service_tier") || "2", 10),
  });
});

// The acquisition cabin planner. `seats` here reaches the engine — it used to
// be assembled correctly, sent correctly, and then dropped by the server's
// command table, which is why typed seat counts appeared to be ignored.
const acquirePlanner = bindCabinPlanner({
  inputs: cabinInputs(els.formAcquire),
  preset: els.acqPreset,
  panel: els.acqCabin,
  specId: null,
});
els.specSelect.addEventListener("change", () => acquirePlanner.setSpec(els.specSelect.value));

els.formAcquire.addEventListener("submit", (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const seats = acquirePlanner.seats();
  sendCommand("acquire_aircraft", {
    spec_id: f.get("spec_id"),
    tail_number: f.get("tail_number"),
    method: f.get("method"),
    base_iata: String(f.get("base_iata") || "").trim().toUpperCase() || null,
    seats: Object.keys(seats).length ? seats : null,
  }).then(() => {
    e.target.reset();
    acquirePlanner.setSpec(els.specSelect.value);
  });
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
  // after the catalog, because the map needs airport coordinates from it
  await initMap();
  // the aircraft list only exists after the catalog loads, so the cabin
  // planner can't be primed until now
  acquirePlanner.setSpec(els.specSelect.value);
  const state = await fetch("/api/state").then((r) => r.json());
  render(state);
  // Valuations move as the sim runs, but not fast enough to justify a fetch
  // per tick — the SSE snapshot stays lean and this polls beside it.
  refreshMergers();
  setInterval(refreshMergers, 15000);
  connect();
})();
