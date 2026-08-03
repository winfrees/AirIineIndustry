# A standalone iOS version — paths, costs, and what's in the way

Status: **investigation. Nothing implemented, no iOS code in the repo.**
Written to record what the port surface actually is, measured against the
codebase rather than estimated, so the decision can be made once and revisited
against real numbers.

Every figure in §1 was measured on the tree at the time of writing. Re-measure
before trusting them; the commands are given so that is cheap.

---

## 1. What would actually be ported

The measurements are unusually favourable, and they are the reason this is a
port rather than a rewrite.

| | measured | how |
|---|---|---|
| Platform-free simulation | **~9,300 LOC** | `wc -l airlinesim/*.py` minus `game/server/explorer/cli/gamelog` |
| Total runtime package | 11,841 LOC | `wc -l airlinesim/*.py` |
| Browser front end | ~3,000 LOC | `wc -l airlinesim/webui/*` |
| Committed data | ~520 KB | `du -sh airlinesim/data/*` |
| `snapshot()` payload | 45 KB JSON | 40-day corpus world, 3 AI carriers |
| `catalog()` payload | 73 KB JSON | served once, not per tick |
| Save file | **5.5 MB pickle** | same world — see §4.1 |
| Non-stdlib imports | **none** | AST walk over `airlinesim/`, excluding `btsdata/` |
| Scenario checks | **313** | `[PASS]` lines across the 11 self-checking scenarios |
| Declared Python floor | 3.10 | `requires-python` in `pyproject.toml` — see §1.1 |

Structural facts that matter more than the line counts:

- **Platform coupling is confined to four files**: `cli.py`, `server.py`,
  `explorer.py`, `game.py`. In `game.py` it is only `threading` + `time` for
  the real-time clock and `pickle` for saves. Everything else — the engine,
  the AI, weather, disruption, alliances, mergers, crew, routes, cabins,
  finance, the route-data provider, `geomag` — touches nothing but arithmetic
  and the standard library.
- **No C extensions and no third-party packages.** The stdlib modules in use
  (`math`, `json`, `csv`, `gzip`, `dataclasses`, `pickle`, `enum`, `abc`,
  `threading`, `socket`, `urllib`, `http`, `logging`, `pathlib`…) are all
  present in the iOS CPython build.
- **The engine is deterministic** — there is not one `random` call in
  `engine.py`, and the weather model's randomness lives in a seeded
  `random.Random` that pickles with the world. This is what makes a Swift port
  verifiable (§2.3) and it is worth protecting on any path.
- **An RPC seam already exists and is proven.** `actions.py` exposes 34
  functions over `(world, player, ...) -> (ok, message)`; `server.COMMANDS` is
  a dispatch table over them; `snapshot()` and `catalog()` are the read side.
  That is already the model layer an app needs — it was not built for this,
  but it fits.
- **PWA scaffolding is already committed**: `webui/manifest.json`,
  `service-worker.js`, `apple-touch-icon`, `apple-mobile-web-app-capable`.

### 1.1 The Apple-side enabler

CPython gained **official iOS support (Tier 3) in 3.13 via PEP 730** — a real
`iOS` platform target, an XCFramework build, and a testbed project. Before
that, embedding Python on iOS meant an unofficial toolchain.

Two constraints that are often misremembered and are *not* blockers here:

- **App Store Guideline 2.5.2** requires an app be self-contained and forbids
  downloading executable code. Bundling an interpreter that runs *your own
  bundled scripts* is established practice — Pythonista, BeeWare/Briefcase and
  Kivy apps all ship. What is forbidden is fetching new code after install.
- **No JIT for third-party apps** (no writable-executable memory). CPython is
  a plain interpreter, so it is unaffected. PyPy is out.

The package targets Python 3.10+; the official iOS path needs **3.13+**. Check
for 3.10-era syntax assumptions before assuming a free upgrade.

---

## 2. The four paths

| | approach | effort | keeps | main risk |
|---|---|---|---|---|
| **A** | WKWebView + embedded CPython, `server.py` on localhost | days–weeks | everything | reads as a webview app; touch UX unchanged |
| **B** | CPython engine + native SwiftUI front end | weeks–months | the ~9,300-LOC core | UI is a rewrite; `map.js` → Canvas/MapKit |
| **C** | Full Swift port | months | the design only | two engines to maintain forever |
| **D** | Pyodide/WASM in a native shell | weeks | everything, plus a web build | ~10 MB runtime, 1–3 s cold start |

### 2.1 Path A — WKWebView + embedded CPython

Bundle `python.xcframework`, ship the `airlinesim` package as app resources,
start the existing server in-process on `127.0.0.1`, point a `WKWebView` at
it. The entire `webui/` works unchanged.

- `routedata.DATA_DIR` and `server.WEBUI_DIR` are `__file__`-relative. CLAUDE.md
  already flags this for the Windows bundle ("keep it that way or rewrite both
  to `importlib.resources` first"), and it holds on iOS for the same reason:
  the app bundle is read-only but the files are real files on disk. **Do not
  zip the package.**
- The localhost socket is avoidable. A `WKScriptMessageHandler` bridge maps
  onto `server.COMMANDS` almost line for line and removes a moving part —
  worth doing even in the spike.
- Honest downside: this ships the desktop UI on a phone. It is the fastest way
  to get the real engine onto a real device, which is its actual value.

### 2.2 Path B — CPython engine, native SwiftUI UI

Keep the simulation core as Python. Drop `server.py` and `webui/`. Swift drives
`GameSession`/`actions.py` and renders `snapshot()`.

- Embed CPython via the C API with a small Swift/ObjC shim, following the
  PEP 730 testbed layout. **PythonKit is really a macOS/Linux tool** — do not
  plan around it on iOS without proving it first.
- The scenario suite stays the regression net for the engine, which is the
  main argument for this path over C.
- The map is the substantial new work: `map.js` is 567 lines of SVG drawing
  that becomes SwiftUI `Canvas` or MapKit overlays. The Albers projection and
  `geomag.py` are pure arithmetic and port in an afternoon.

### 2.3 Path C — full Swift port

Rewrite ~9,300 LOC of arithmetic and dataclasses. No metaprogramming, no C
extensions, no third-party deps to replace.

**This codebase makes the port unusually safe**: the engine is deterministic,
so each module can be ported and then diffed against the Python reference for
byte-identical output over a fixed run. The 313 scenario checks give a ready
oracle.

The cost is not the writing, it is the *owning*: every future rule change
lands twice, and the scenario suite stops covering the shipped engine. Take
this path only if on-device profiling shows a real problem — for a tick loop
this size it will not.

### 2.4 Path D — Pyodide/WASM in a native shell

Compile the Python engine to WASM and run it client-side in a `WKWebView`, no
server and no Xcode Python toolchain, wrapped as a native app for the store.

- One codebase serves the web build and iOS.
- Costs: ~10 MB runtime, 1–3 s cold start, and 5.5 MB saves living in
  IndexedDB (§4.1 gets worse here, not better).
- A reasonable hedge *if* an offline browser version is also wanted.

### 2.5 The near-free option: PWA

`manifest.json`, `service-worker.js` and the Apple meta tags are already in the
repo, so "Add to Home Screen" gives a full-screen app today. But the engine
runs server-side, so it is neither standalone nor offline, and it is not an
App Store product. Its real use is **validating the touch UX before committing
to a port** — cheap, and it answers §4.3 without writing any Swift.

---

## 3. Recommendation

1. **Spike Path A** (~2 weeks) — not as the product. The goal is the real
   engine on a real device, to find out what touch UX, battery and startup
   actually feel like.
2. **Build Path B** for the shipping app, reusing what the spike taught about
   the bridge.
3. **Fix the save format first** (§4.1). It is the one item that gets strictly
   harder the more players exist.

Path C stays on the shelf pending a measured performance problem.

---

## 4. What is genuinely in the way

These are codebase-specific and apply to **every** path.

### 4.1 Saves are 5.5 MB pickles

`GameSession.save/load` pickles the whole session. Two problems on mobile:

- **Size.** 5.5 MB per save on a 40-day corpus world, and it grows with the
  network. With autosave and multiple slots this is not free, and it is worse
  under Path D (IndexedDB).
- **Fragility, which is the real one.** Pickle is bound to the Python version
  and the class layout. A save written by one build **cannot be trusted to
  load after an app update that changes a dataclass** — on desktop the user
  can stay on an old version; on the App Store they cannot. That turns a
  routine engine change into orphaned save games.

A versioned, explicit save format (JSON or SQLite) with a migration path is
real work and should be done **before** shipping, not after. Note the same
mechanism backs the outcome explorer's forking (`explorer.py` pickles nodes at
~11–60 KB each), so any change has to keep that working or give it its own
path.

### 4.2 The real-time clock meets app suspension

`GameSession._loop` runs a background thread converting real seconds into sim
hours. iOS suspends the app, so that thread stops.

The good news: **this project already solved the hard version of the problem.**
A gap over `SUSPEND_GAP_S` is discarded, the session pauses itself, and
`clock_notice` reports how much was skipped — built because three hours with a
laptop lid shut replayed ~5,400 sim-days in one locked burst. On iOS that
stops being an edge case and becomes the normal path, and the existing
behaviour is the correct one.

What remains is a **product decision, not a port problem**: should an airline
sim advance while the app is closed? Most mobile sims do. Answering "yes"
means an offline-progress model (compute forward on resume, bounded), which
the discard-the-gap logic deliberately does *not* do. Decide this before
designing the UI around it.

### 4.3 The UI is desktop-shaped

This is the biggest **product** cost, and no choice of path reduces it:

- a 12-column Routes table with per-cabin fare sub-rows
- a 300-airport datalist for route opening
- a network map sized to 78vh of a desktop viewport
- dense metric lines on the carrier cards

`styles.css` has a 720px breakpoint that stacks every block full-width, so it
*reflows* — but reflowing narrow is not the same as being designed for touch.
Budget for a genuine mobile information architecture, and use the PWA (§2.5)
to learn what it needs.

### 4.4 Filesystem assumptions

Small but they fail silently in a sandbox:

- `server.DEFAULT_SAVE_PATH` → `~/.airlinesim_save.pkl`
- `gamelog.py` → `~/.airlinesim/logs/airlinesim.log`, a rotating handler at
  4 MB × 6 files

Both must move to the app's Application Support / Documents directories, and
the log cap should be reconsidered against a phone's storage and the fact that
`gamelog` is deliberately event-driven (~8.2 KB per 1,000 sim-days).

### 4.5 What does *not* need solving

Worth recording so it is not re-litigated:

- **Dependencies.** There are none. No wheels to find iOS builds for.
- **Data.** ~520 KB, all committed, all read-only, all loaded through
  `SpecRepository` / `routedata`. It ships as bundle resources unchanged.
- **Determinism.** Already guaranteed and already asserted by
  `airlinesim run explorer`. It survives every path.
- **The dev-time ingest.** `btsdata/` is never imported at runtime and does
  not ship. Neither does `tools/`.

---

## 5. Open questions to settle before starting

1. Does the game advance while the app is backgrounded? (§4.2 — drives the
   whole session model.)
2. Is the outcome explorer part of the iOS product, or desktop-only? It is a
   second front end and a second pickle consumer; excluding it removes work.
3. One save slot or several, and does iCloud sync matter? (§4.1 — decide
   before choosing the format.)
4. Is a browser build also wanted? If yes, Path D stops being a hedge and
   starts being the efficient answer.
5. Does the 3.10 → 3.13 floor break anything? Cheap to check, and the official
   iOS path requires it.
