// Built-in dashboard for HDHR Local. Draws Home Assistant's own cards (gauge, entities, conditional,
// history-graph, markdown, tile) from the layout the integration reports over the websocket ("hdhr/layout"),
// so it adapts to however many tuners and DVR engines are on the network. People can still build their own
// dashboards from the same entities.
const SIGNAL = { green: 80, yellow: 60, red: 0 }; // signal: high is good
const DISK = { green: 0, yellow: 70, red: 90 }; // disk: high is bad

function tunerBlocks(dev) {
  const e = dev.entities;
  const count = dev.count || 0;
  const idleRows = [];
  const blocks = [];
  for (let n = 0; n < count; n++) {
    const k = (s) => e[`tuner_${n}_${s}`];
    if (!k("channel")) continue;
    // idle: one compact row in the device block
    idleRows.push({
      type: "conditional",
      conditions: [{ condition: "state", entity: k("channel"), state: "idle" }],
      row: { entity: k("channel"), name: `Tuner ${n}`, icon: "mdi:antenna" },
    });
    // in use: a full block of its own (the conditional card hides the whole block while idle)
    blocks.push({
      type: "conditional",
      conditions: [{ condition: "state", entity: k("channel"), state_not: "idle" }],
      card: { type: "vertical-stack", cards: [
        { type: "heading", heading: `Tuner ${n}`, heading_style: "subtitle", icon: "mdi:antenna" },
        { type: "entities", entities: [{ entity: k("channel"), name: "Channel" }] },
        // its own line, so a long list of viewers wraps instead of squeezing the label
        { type: "markdown", content: "**Watching:** {{ states('" + k("viewer") + "') }}" +
          (k("program") ? "  \n**On now:** {{ states('" + k("program") + "') }}" +
            "{% set ep = state_attr('" + k("program") + "', 'episode') %}{% if ep %} - {{ ep }}{% endif %}" : "") },
        { type: "grid", columns: 2, square: false, cards: [
          { type: "gauge", entity: k("signal_strength"), name: "Signal strength", min: 0, max: 100, unit: "%", severity: SIGNAL },
          { type: "gauge", entity: k("signal_quality"), name: "Signal quality", min: 0, max: 100, unit: "%", severity: SIGNAL },
        ] },
        { type: "entities", entities: [
          { entity: k("errors"), name: "Stream errors" },
          k("error_total") && { entity: k("error_total"), name: "Error total" },
          { entity: k("symbol_quality"), name: "Symbol quality" },
          { entity: k("bitrate"), name: "Bitrate" },
        ].filter(Boolean) },
      ] },
    });
  }
  const head = [
    { type: "heading", heading: dev.name, heading_style: "title", icon: "mdi:satellite-uplink" },
    {
      type: "gauge", entity: e.tuners_in_use, name: "Tuners in use", min: 0, max: count || 4,
      severity: { green: 0, yellow: Math.max(1, (count || 4) - 1), red: count || 4 },
    },
  ];
  const tally = [
    e.error_total && { entity: e.error_total, name: "Error total (all tuners)" },
    // a "button" row (not the entity row) so the label reads Reset and a stray tap asks first
    e.reset_errors && {
      type: "button", name: "Reset error totals", icon: "mdi:counter", action_name: "Reset",
      tap_action: {
        action: "perform-action", perform_action: "button.press", target: { entity_id: e.reset_errors },
        confirmation: { text: "Reset the error totals for all tuners to 0?" },
      },
    },
  ].filter(Boolean);
  if (tally.length) head.push({ type: "entities", entities: tally });
  if (idleRows.length) {
    head.push({
      type: "conditional",
      conditions: [{ condition: "numeric_state", entity: e.tuners_in_use, below: count }],
      card: { type: "entities", title: "Idle tuners", entities: idleRows },
    });
  }
  return [{ type: "vertical-stack", cards: head }, ...blocks];
}

function dvrBlocks(dev) {
  const e = dev.entities;
  const cards = [
    { type: "heading", heading: dev.name, heading_style: "title", icon: "mdi:archive" },
    { type: "grid", columns: 2, square: false, cards: [
      { type: "gauge", entity: e.used, name: "Disk used", min: 0, max: 100, unit: "%", severity: DISK },
      { type: "tile", entity: e.recording, name: "Recording" },
    ] },
    { type: "entities", entities: [
      { entity: e.live_streams, name: "Live streams" },
      { entity: e.free_space, name: "Free space" },
      { entity: e.recordings, name: "Recordings" },
    ] },
  ];
  if (e.last_recording) {
    cards.push({
      type: "markdown",
      content:
        "{% set t = states('" + e.last_recording + "') %}**Last recording:** " +
        "{% if t in ['unknown', 'unavailable', 'None', ''] %}none{% else %}{{ t }}" +
        "{% set c = state_attr('" + e.last_recording + "', 'channel') %}{% if c %} (ch {{ c }}){% endif %}{% endif %}",
    });
  }
  if (e.live_streams) {
    cards.push({
      type: "markdown",
      title: "Live feeds",
      content:
        "{% set s = state_attr('" + e.live_streams + "', 'streams') or [] %}" +
        "{% if s %}{% for x in s %}- **{{ x.split(' -> ')[0] }}** to {{ x.split(' -> ')[1] }}\n" +
        "{% endfor %}{% else %}No live feeds.{% endif %}",
    });
  }
  const blocks = [{ type: "vertical-stack", cards: cards.filter((c) => c.type !== "grid" || c.cards.every((x) => x.entity)) }];
  if (e.recordings_calendar || e.next_recording) blocks.push(recordingsBlock(e));
  return blocks;
}

// Upcoming recordings (cloud schedule): next one, counts, tuner-conflict warning with the clashing shows, calendar.
function recordingsBlock(e) {
  const cards = [{ type: "heading", heading: "Recordings", heading_style: "title", icon: "mdi:calendar-clock" }];
  if (e.next_recording) {
    const n = e.next_recording;
    cards.push({
      type: "markdown",
      content:
        "{% set n = '" + n + "' %}**Next recording:** " +
        "{% if states(n) in ['unknown', 'unavailable'] %}none scheduled{% else %}" +
        "{{ state_attr(n, 'title') }}{% if state_attr(n, 'episode') %} - {{ state_attr(n, 'episode') }}{% endif %}" +
        " (ch {{ state_attr(n, 'channel') }}), {{ as_timestamp(states(n)) | timestamp_custom('%a %b %-d, %-I:%M %p') }}{% endif %}",
    });
  }
  const rows = [
    e.upcoming_recordings && { entity: e.upcoming_recordings, name: "Upcoming (14 days)" },
    e.peak_tuners && { entity: e.peak_tuners, name: "Most tuners needed at once" },
    e.tuner_conflict && { entity: e.tuner_conflict, name: "Tuner conflict" },
  ].filter(Boolean);
  if (rows.length) cards.push({ type: "entities", entities: rows });
  if (e.tuner_conflict) {
    cards.push({
      type: "conditional",
      conditions: [{ condition: "state", entity: e.tuner_conflict, state: "on" }],
      card: {
        type: "markdown",
        content:
          "{% set c = state_attr('" + e.tuner_conflict + "', 'conflicts') or [] %}" +
          "**Not enough tuners ({{ state_attr('" + e.tuner_conflict + "', 'tuners') }}):**\n" +
          "{% for x in c %}- {{ as_timestamp(x.start) | timestamp_custom('%a %-I:%M %p') }}-" +
          "{{ as_timestamp(x.end) | timestamp_custom('%-I:%M %p') }}: needs {{ x.needed }} - {{ x.shows | join(', ') }}\n{% endfor %}",
      },
    });
  }
  if (e.recordings_calendar) {
    cards.push({ type: "calendar", entities: [e.recordings_calendar], initial_view: "listWeek" });
  }
  return { type: "vertical-stack", cards };
}

// TVs / players: what each is playing, and (where a freeze detector reports) frozen + freezes today + last freeze.
function playerBlocks(layout) {
  return (layout.players || [])
    .map((p) => {
      const e = p.entities;
      const rows = [
        e.now_playing && { entity: e.now_playing, name: "Now playing" },
        e.playing && { entity: e.playing, name: "Playing" },
        e.channel && { entity: e.channel, name: "Channel" },
        e.frozen && { entity: e.frozen, name: "Frozen" },
        e.freezes_today && { entity: e.freezes_today, name: "Freezes today" },
        e.last_freeze && { entity: e.last_freeze, name: "Last freeze" },
      ].filter(Boolean);
      return rows.length
        ? { type: "vertical-stack", cards: [
            { type: "heading", heading: p.name, heading_style: "title", icon: "mdi:television" },
            { type: "entities", entities: rows },
          ] }
        : null;
    })
    .filter(Boolean);
}

// Signal history on the main page. One graph per block so the masonry can pack them into the free columns,
// most useful first. Titles carry the device name only when there is more than one device of that kind.
function historyBlocks(layout) {
  const blocks = [];
  const tuners = layout.devices.filter((d) => d.kind === "tuner");
  const dvrs = layout.devices.filter((d) => d.kind === "dvr");
  const graph = (title, entities, hours = 24) =>
    entities.length ? { type: "history-graph", title, hours_to_show: hours, entities } : null;
  for (const dev of tuners) {
    const e = dev.entities;
    const pre = tuners.length > 1 ? `${dev.name}: ` : "";
    const per = (s) => {
      const out = [];
      for (let n = 0; n < (dev.count || 0); n++) if (e[`tuner_${n}_${s}`]) out.push({ entity: e[`tuner_${n}_${s}`], name: `Tuner ${n}` });
      return out;
    };
    blocks.push(
      graph(`${pre}Signal quality (24 h)`, per("signal_quality")),
      graph(`${pre}Errors per minute (24 h)`, per("errors")),
      graph(`${pre}Error totals (24 h)`, per("error_total")),
      graph(`${pre}Channels (24 h)`, per("channel")),
      graph(`${pre}Signal strength (24 h)`, per("signal_strength")),
      graph(`${pre}Bitrate (24 h)`, per("bitrate")),
    );
  }
  for (const dev of dvrs) {
    const e = dev.entities;
    const pre = dvrs.length > 1 ? `${dev.name}: ` : "";
    if (e.live_streams) blocks.push(graph(`${pre}Live streams (24 h)`, [{ entity: e.live_streams, name: "Live streams" }]));
    if (e.used) blocks.push(graph(`${pre}Disk used (7 days)`, [{ entity: e.used, name: "Disk used" }], 168));
  }
  return blocks.filter(Boolean);
}

const MIN_COL = 330; // px: narrowest column
const GAP = 8; // px between blocks and columns
const GUIDE_HOURS = 6; // the Guide grid's window; the arrows move it by 6 hours or a day

class HdhrPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._cards = [];
    this._wrappers = [];
    this._tab = "overview";
    // Masonry: blocks never split, keep their order, each goes into the shortest column. Re-packs when the page
    // width changes or a block grows/shrinks (a tuner starts or stops).
    this._ro = new ResizeObserver(() => this._scheduleLayout());
  }

  disconnectedCallback() {
    this._ro.disconnect();
  }

  _scheduleLayout() {
    clearTimeout(this._layoutTimer);
    this._layoutTimer = setTimeout(() => this._layoutColumns(), 120);
  }

  _layoutColumns() {
    const grid = this._grid;
    if (!grid || !this._wrappers.length) return;
    const width = grid.clientWidth - 2 * GAP;
    const n = Math.max(1, Math.min(this._wrappers.length, Math.floor((width + GAP) / (MIN_COL + GAP))));
    const cols = Array.from({ length: n }, () => ({ h: 0, items: [] }));
    this._wrappers.forEach((w, i) => {
      // a hidden block (an idle tuner's conditional card) takes no space and no gap
      const card = w.firstElementChild;
      const shown = !this._measured || (card && card.offsetHeight > 0);
      w.classList.toggle("empty", !shown);
      const h = !this._measured ? 120 : shown ? w.offsetHeight : 0;
      let best = 0;
      for (let c = 1; c < n; c++) if (cols[c].h < cols[best].h - 1) best = c;
      cols[best].items.push(i);
      cols[best].h += h ? h + GAP : 0;
    });
    this._measured = true;
    const key = cols.map((c) => c.items.join(",")).join("|");
    if (key === this._layoutKey) return; // same placement: don't move anything
    this._layoutKey = key;
    grid.replaceChildren(
      ...cols.map((c) => {
        const col = document.createElement("div");
        col.className = "col";
        for (const i of c.items) col.appendChild(this._wrappers[i]);
        return col;
      })
    );
  }

  set hass(hass) {
    this._hass = hass;
    for (const c of this._cards) c.hass = hass;
    if (this._menu) this._menu.hass = hass;
    if (!this._started) {
      this._started = true;
      this._load();
    }
  }

  set narrow(v) {
    this._narrow = v;
    if (this._menu) this._menu.narrow = v;
  }

  set panel(p) {
    this._panel = p;
  }

  async _load() {
    try {
      this._layout = await this._hass.callWS({ type: "hdhr/layout" });
    } catch (err) {
      this._layout = { title: "HDHomeRun", devices: [], error: String((err && err.message) || err) };
    }
    this._helpers = await window.loadCardHelpers();
    this._render();
  }

  _blocks() {
    // Tabs: Overview = live status, TVs, archives + recordings; Guide = searchable guide (rendered separately);
    // Signal history = the graphs, on their own tab.
    const L = this._layout;
    if (this._tab === "history") return historyBlocks(L);
    const out = [];
    for (const dev of L.devices.filter((d) => d.kind === "tuner")) out.push(...tunerBlocks(dev));
    out.push(...playerBlocks(L));
    for (const dev of L.devices.filter((d) => d.kind === "dvr")) out.push(...dvrBlocks(dev));
    return out;
  }

  _render() {
    const root = this.shadowRoot;
    root.innerHTML = `
      <style>
        :host { display: block; min-height: 100vh; background: var(--primary-background-color); }
        .toolbar { display: flex; align-items: center; height: var(--header-height, 56px); padding: 0 8px;
          background: var(--app-header-background-color, var(--primary-color));
          color: var(--app-header-text-color, var(--text-primary-color, #fff)); }
        .title { flex: 1; margin-left: 8px; font-size: 20px; font-weight: 400; white-space: nowrap;
          overflow: hidden; text-overflow: ellipsis; }
        .tabs button { background: none; border: none; color: inherit; font: inherit; font-size: 14px;
          padding: 16px 12px 14px; cursor: pointer; opacity: 0.7; border-bottom: 2px solid transparent; }
        .tabs button.active { opacity: 1; border-bottom-color: currentColor; }
        .grid { display: flex; gap: ${GAP}px; padding: ${GAP}px; align-items: flex-start; }
        .col { flex: 1 1 0; min-width: 0; display: flex; flex-direction: column; }
        .block { display: block; break-inside: avoid; padding-bottom: ${GAP}px; }
        .block.empty { padding-bottom: 0; }
        .msg { padding: 24px; color: var(--primary-text-color); }
        .guide { padding: 8px; max-width: 1600px; margin: 0 auto; }
        .gbar { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 6px; }
        .gsearch { flex: 1 1 300px; font-size: 16px; padding: 10px 12px; border-radius: 8px;
          border: 1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color); }
        .gplayer { color: var(--primary-text-color); font-size: 14px; }
        .gplayer select { margin-left: 6px; font: inherit; padding: 6px; border-radius: 6px;
          background: var(--card-background-color); color: var(--primary-text-color); border: 1px solid var(--divider-color); }
        .gmsg { min-height: 20px; margin: 2px 2px 8px; font-size: 14px; color: var(--secondary-text-color); }
        .gmsg.ok { color: var(--success-color, #43a047); }
        .gmsg.bad { color: var(--error-color, #db4437); }
        .glist { display: flex; flex-direction: column; gap: 6px; }
        .grow { display: flex; gap: 12px; align-items: center; background: var(--card-background-color);
          border-radius: 10px; padding: 10px 12px; box-shadow: var(--ha-card-box-shadow, none); }
        .gch { width: 96px; flex: none; display: flex; flex-direction: column; color: var(--primary-text-color); }
        .gch span { font-size: 12px; color: var(--secondary-text-color); overflow: hidden; text-overflow: ellipsis;
          white-space: nowrap; }
        .gprog { flex: 1; min-width: 0; }
        .gtitle { font-size: 15px; color: var(--primary-text-color); }
        .gsub, .gnext { font-size: 12px; color: var(--secondary-text-color); }
        .gstat { font-size: 12px; color: var(--secondary-text-color); margin: 0 2px 2px; }
        .gday { margin: 10px 2px 2px; font-size: 14px; font-weight: 500; color: var(--primary-text-color); }
        .gact { display: flex; gap: 6px; flex: none; }
        .gact button { background: transparent; color: var(--primary-color); border: 1px solid var(--primary-color);
          border-radius: 6px; padding: 6px 10px; cursor: pointer; font: inherit; font-size: 13px; }
        .gact button.watch { background: var(--primary-color); color: var(--text-primary-color, #fff); }
        .gsyn { font-size: 12px; color: var(--secondary-text-color); margin-top: 4px; }
        .gnav { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; margin: 2px 0 6px; }
        .gnav button { background: var(--card-background-color); color: var(--primary-text-color); font: inherit;
          font-size: 14px; border: 1px solid var(--divider-color); border-radius: 6px; padding: 6px 12px; cursor: pointer; }
        .gnav button:disabled { opacity: 0.4; cursor: default; }
        .gwhen { margin-left: 8px; font-size: 15px; font-weight: 500; color: var(--primary-text-color); }
        .gscroll { overflow: auto; max-height: calc(100vh - var(--header-height, 56px) - 180px); border-radius: 10px;
          border: 1px solid var(--divider-color); background: var(--card-background-color); }
        .ggrid { min-width: 860px; }
        .gline { display: flex; border-bottom: 1px solid var(--divider-color); }
        .ghead { position: sticky; top: 0; z-index: 3; background: var(--card-background-color); }
        .gchc { width: 120px; flex: none; position: sticky; left: 0; z-index: 2; box-sizing: border-box; padding: 4px 8px;
          display: flex; flex-direction: column; justify-content: center; font-size: 14px; color: var(--primary-text-color);
          background: var(--card-background-color); border-right: 1px solid var(--divider-color); }
        .gchc span, .gchc i { font-size: 11px; font-style: normal; color: var(--secondary-text-color); white-space: nowrap;
          overflow: hidden; text-overflow: ellipsis; }
        .gline.watched .gchc i { color: var(--primary-color); }
        .gtrack { position: relative; flex: 1; height: 50px; }
        .gaxis { height: 28px; }
        .gaxis span { position: absolute; top: 6px; padding-left: 4px; font-size: 12px; white-space: nowrap;
          color: var(--secondary-text-color); border-left: 1px solid var(--divider-color); }
        .gblk { position: absolute; top: 3px; bottom: 3px; margin-left: 1px; box-sizing: border-box; overflow: hidden;
          padding: 4px 6px; border-radius: 6px; cursor: pointer; background: var(--secondary-background-color);
          border: 1px solid var(--divider-color); }
        .gblk:hover { border-color: var(--primary-color); }
        .gblk.live { border-left: 3px solid var(--primary-color); }
        .gblk.sel { outline: 2px solid var(--primary-color); outline-offset: -2px; }
        .gblk .bt { font-size: 13px; color: var(--primary-text-color); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .gblk .bs { font-size: 11px; color: var(--secondary-text-color); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .gnow { position: absolute; top: 0; bottom: 0; width: 2px; z-index: 1; pointer-events: none;
          background: var(--error-color, #db4437); }
        .gnone { position: absolute; left: 8px; top: 16px; font-size: 12px; color: var(--secondary-text-color); }
        .gdetail { position: sticky; left: 0; width: min(100%, 92vw); box-sizing: border-box; padding: 6px;
          border-bottom: 1px solid var(--divider-color); }
      </style>
      <div class="toolbar">
        <ha-menu-button></ha-menu-button>
        <div class="title"></div>
        <div class="tabs">
          <button data-tab="overview">Overview</button>
          <button data-tab="guide">Guide</button>
          <button data-tab="history">Signal history</button>
        </div>
      </div>
      <div class="grid"></div>`;
    this._menu = root.querySelector("ha-menu-button");
    this._menu.hass = this._hass;
    this._menu.narrow = this._narrow;
    root.querySelector(".title").textContent = this._layout.title || "HDHomeRun";
    for (const b of root.querySelectorAll(".tabs button")) {
      b.classList.toggle("active", b.dataset.tab === this._tab);
      b.addEventListener("click", () => {
        if (this._tab === b.dataset.tab) return;
        this._tab = b.dataset.tab;
        this._render();
      });
    }
    const grid = root.querySelector(".grid");
    this._grid = grid;
    this._cards = [];
    this._wrappers = [];
    this._layoutKey = null;
    this._measured = false;
    this._ro.disconnect();
    clearInterval(this._guideTimer);
    if (this._tab === "guide") {
      grid.className = "guide";
      this._guideContainer = grid;
      this._renderGuide(grid);
      return;
    }
    if (this._layout.error || !this._layout.devices.length) {
      const m = document.createElement("div");
      m.className = "msg";
      m.textContent = this._layout.error ? `Could not load the layout: ${this._layout.error}` : "No HDHomeRun devices found yet.";
      grid.appendChild(m);
      return;
    }
    for (const config of this._blocks()) {
      const el = this._helpers.createCardElement(config);
      el.hass = this._hass;
      const wrap = document.createElement("div");
      wrap.className = "block";
      wrap.appendChild(el);
      this._cards.push(el);
      this._wrappers.push(wrap);
      this._ro.observe(wrap);
    }
    this._ro.observe(grid);
    this._layoutColumns(); // first pass on estimates; real heights re-pack it as the cards render
  }

  // --- Guide tab: search the whole guide (~2 weeks) or scroll it by day / 6-hour block. Empty box = a time grid, GUIDE_HOURS at
  // a time (the integration sends only that window); typing searches all ~2 weeks instead. Watch / Record / Series
  // call hdhr.tune and hdhr.record and show the same sentence a voice assistant would say.
  _nowSlot() {
    return Math.floor(Date.now() / 1000 / 1800) * 1800; // the current half hour
  }

  async _loadGuide() {
    if (this._gStart != null && this._gStart <= this._nowSlot()) this._gStart = null; // time caught up: back to "now"
    try {
      this._guide = await this._hass.callWS({
        type: "hdhr/guide", start: this._gStart ?? this._nowSlot(), hours: GUIDE_HOURS,
      });
    } catch (err) {
      this._guide = { channels: [], error: String((err && err.message) || err) };
    }
  }

  async _renderGuide(box) {
    box.innerHTML = `
      <div class="gbar">
        <input class="gsearch" type="search" placeholder="Search the next 2 weeks: shows, episodes, channels" autocomplete="off">
        <label class="gplayer">Watch on <select></select></label>
      </div>
      <div class="gnav">
        <button data-move="-86400">&laquo; Day</button>
        <button data-move="-21600">&lsaquo; 6 h</button>
        <button data-move="0">Now</button>
        <button data-move="21600">6 h &rsaquo;</button>
        <button data-move="86400">Day &raquo;</button>
        <span class="gwhen"></span>
      </div>
      <div class="gstat"></div>
      <div class="gmsg"></div>
      <div class="glist"><div class="msg">Loading the guide...</div></div>`;
    const sel = box.querySelector("select");
    const tunable = (this._layout.players || []).filter((p) => p.tunable);
    for (const p of tunable) {
      const o = document.createElement("option");
      o.value = o.textContent = p.name;
      sel.appendChild(o);
    }
    if (this._guidePlayer) sel.value = this._guidePlayer;
    sel.addEventListener("change", () => (this._guidePlayer = sel.value));
    box.querySelector(".gplayer").style.display = tunable.length ? "" : "none";
    const input = box.querySelector(".gsearch");
    input.value = this._guideQuery || "";
    let t;
    input.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(() => {
        this._guideQuery = input.value;
        this._fillGuide(box);
      }, 300);
    });
    for (const b of box.querySelectorAll(".gnav button")) {
      b.addEventListener("click", async () => {
        const step = Number(b.dataset.move);
        let next = step ? (this._gStart ?? this._nowSlot()) + step : null;
        if (next != null && next <= this._nowSlot()) next = null; // the guide has nothing before now
        this._gStart = next;
        this._gDetail = null;
        await this._loadGuide();
        this._fillGuide(box);
      });
    }
    await this._loadGuide();
    this._fillGuide(box);
    this._guideTimer = setInterval(async () => {
      if (this._tab !== "guide") return;
      await this._loadGuide();
      this._fillGuide(box);
    }, 300000);
  }

  async _fillGuide(box) {
    const list = box.querySelector(".glist");
    const G = this._guide || { channels: [] };
    const say = (text) => {
      const m = document.createElement("div");
      m.className = "msg";
      m.textContent = text;
      list.replaceChildren(m);
    };
    const q = (this._guideQuery || "").trim();
    box.querySelector(".gnav").style.display = q ? "none" : "";
    const stat = box.querySelector(".gstat");
    if (stat) stat.textContent = G.channels.length ? `${G.channels.length} channels · guide covers the next ${G.days} days` : "";
    if (G.error) return say(`Could not load the guide: ${G.error}`);
    if (!G.channels.length) return say("The guide is still loading from SiliconDust (about 15 seconds after a restart).");
    if (!q) return this._fillGrid(box, list, G);
    // a search runs in the integration over the whole two weeks; only the matches come back
    const now = Date.now() / 1000;
    const asked = q;
    let res;
    try {
      res = await this._hass.callWS({ type: "hdhr/guide_search", query: q, limit: 300 });
    } catch (err) {
      return say(`Search failed: ${(err && err.message) || err}`);
    }
    if ((this._guideQuery || "").trim() !== asked) return; // a newer search is on its way
    if (!res.results.length) return say(`Nothing in the next ${G.days} days matches "${q}".`);
    const out = [];
    let lastDay = "";
    for (const r of res.results) {
      const d = new Date(r.s * 1000);
      const dayLabel = d.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
      if (dayLabel !== lastDay) {
        const h = document.createElement("div");
        h.className = "gday";
        h.textContent = dayLabel;
        out.push(h);
        lastDay = dayLabel;
      }
      const ch = { number: r.number, name: r.name, watching: res.watching[r.number] || [] };
      out.push(this._guideRow({ ch, p: r }, now));
    }
    if (res.results.length >= 300) {
      const m = document.createElement("div");
      m.className = "msg";
      m.textContent = "Showing the first 300 matches - add a word to narrow it down.";
      out.push(m);
    }
    list.replaceChildren(...out);
  }

  // The browse grid: channels down, GUIDE_HOURS across. Tapping a show opens its details (with the buttons) under
  // that channel. The scroll position is kept when moving through time, so the same channels stay in view.
  _fillGrid(box, list, G) {
    const W = GUIDE_HOURS * 3600;
    const t0 = this._gStart ?? this._nowSlot();
    const t1 = t0 + W;
    const now = Date.now() / 1000;
    const clock = (s) => new Date(s * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const dayOf = (s) => new Date(s * 1000).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
    const sameDay = (a, b) => new Date(a * 1000).toDateString() === new Date(b * 1000).toDateString();
    box.querySelector(".gwhen").textContent =
      `${dayOf(t0)} · ${clock(t0)} – ${sameDay(t0, t1 - 1) ? "" : dayOf(t1) + " "}${clock(t1)}`;
    for (const b of box.querySelectorAll(".gnav button")) {
      const step = Number(b.dataset.move);
      b.disabled = step < 0 ? t0 <= this._nowSlot()
        : step > 0 ? !!G.last && t0 + step >= G.last
        : this._gStart == null;
    }
    const pct = (x) => ((x - t0) / W) * 100;
    const old = list.querySelector(".gscroll");
    const keep = old ? [old.scrollTop, old.scrollLeft] : [0, 0];
    const scroll = document.createElement("div");
    scroll.className = "gscroll";
    const grid = document.createElement("div");
    grid.className = "ggrid";
    scroll.appendChild(grid);
    const head = document.createElement("div");
    head.className = "gline ghead";
    head.innerHTML = `<div class="gchc"></div><div class="gtrack gaxis"></div>`;
    for (let t = t0; t < t1; t += 1800) {
      const tick = document.createElement("span");
      tick.style.left = `${pct(t)}%`;
      tick.textContent = clock(t);
      head.querySelector(".gaxis").appendChild(tick);
    }
    grid.appendChild(head);
    const watching = G.watching || {};
    for (const ch of G.channels) {
      const who = watching[ch.number] || [];
      const line = document.createElement("div");
      line.className = "gline" + (who.length ? " watched" : "");
      line.innerHTML = `<div class="gchc"><b></b><span></span><i></i></div><div class="gtrack"></div>`;
      line.querySelector(".gchc b").textContent = ch.number;
      line.querySelector(".gchc span").textContent = ch.name;
      if (who.length) line.querySelector(".gchc i").textContent = "▶ " + who.join(", ");
      const track = line.querySelector(".gtrack");
      if (now >= t0 && now < t1) {
        const n = document.createElement("div");
        n.className = "gnow";
        n.style.left = `${pct(now)}%`;
        track.appendChild(n);
      }
      if (!ch.programs.length) {
        const e = document.createElement("div");
        e.className = "gnone";
        e.textContent = "No listing";
        track.appendChild(e);
      }
      let open = null;
      for (const p of ch.programs) {
        const a = Math.max(p.s, t0);
        const z = Math.min(p.e, t1);
        const blk = document.createElement("div");
        blk.className = "gblk" + (p.s <= now && now < p.e ? " live" : "");
        blk.style.left = `${pct(a)}%`;
        blk.style.width = `calc(${pct(z) - pct(a)}% - 2px)`;
        blk.innerHTML = `<div class="bt"></div><div class="bs"></div>`;
        blk.querySelector(".bt").textContent = p.t;
        blk.querySelector(".bs").textContent =
          (p.s < t0 ? "‹ " : "") + `${clock(p.s)}-${clock(p.e)}` + (p.ep ? ` · ${p.ep}` : "");
        blk.title = `${p.t}${p.ep ? " - " + p.ep : ""} (${clock(p.s)}-${clock(p.e)})`;
        const key = `${ch.number}|${p.s}`;
        blk.addEventListener("click", () => {
          for (const d of grid.querySelectorAll(".gdetail")) d.remove();
          for (const s of grid.querySelectorAll(".gblk.sel")) s.classList.remove("sel");
          if (this._gDetail === key) {
            this._gDetail = null;
            return;
          }
          this._gDetail = key;
          blk.classList.add("sel");
          line.after(this._guideDetail(ch, p, who));
        });
        if (this._gDetail === key) {
          blk.classList.add("sel");
          open = p;
        }
        track.appendChild(blk);
      }
      grid.appendChild(line);
      if (open) grid.appendChild(this._guideDetail(ch, open, who));
    }
    list.replaceChildren(scroll);
    [scroll.scrollTop, scroll.scrollLeft] = keep;
  }

  _guideDetail(ch, p, who) {
    const d = document.createElement("div");
    d.className = "gdetail";
    d.appendChild(this._guideRow({ ch: { number: ch.number, name: ch.name, watching: who }, p }, Date.now() / 1000, true));
    return d;
  }

  _guideRow({ ch, p, next }, now, full = false) {
    const time = (s) => new Date(s * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const day = (s) => {
      const d = new Date(s * 1000);
      return d.toDateString() === new Date().toDateString() ? "Today" : d.toLocaleDateString([], { weekday: "short" });
    };
    const live = p.s <= now && now < p.e;
    const row = document.createElement("div");
    row.className = "grow";
    row.innerHTML = `<div class="gch"><b></b><span></span></div>
      <div class="gprog"><div class="gtitle"></div><div class="gsub"></div><div class="gnext"></div><div class="gsyn"></div></div>
      <div class="gact"></div>`;
    row.querySelector(".gch b").textContent = ch.number;
    row.querySelector(".gch span").textContent = ch.name;
    row.querySelector(".gtitle").textContent = p.t;
    const watching = live && ch.watching && ch.watching.length ? ` · watching: ${ch.watching.join(", ")}` : "";
    row.querySelector(".gsub").textContent =
      `${live ? "On now" : day(p.s)} ${time(p.s)}-${time(p.e)}` + (p.ep ? ` · ${p.ep}` : "") + watching;
    if (next) row.querySelector(".gnext").textContent = `Next at ${time(next.s)}: ${next.t}`;
    if (full && p.syn) row.querySelector(".gsyn").textContent = p.syn;
    else row.title = p.syn || "";
    const act = row.querySelector(".gact");
    const button = (label, cls, fn) => {
      const b = document.createElement("button");
      b.textContent = label;
      if (cls) b.className = cls;
      b.addEventListener("click", fn);
      act.appendChild(b);
    };
    const select = this._guideContainer && this._guideContainer.querySelector(".gplayer select");
    if (live && select && select.options.length) {
      button("Watch", "watch", () => this._service("tune", { player: select.value, channel: ch.number }));
    }
    if (p.sid) {
      button("Record", "", () => {
        if (window.confirm(`Record ${p.t} at ${time(p.s)} on ${ch.number}?`)) {
          this._service("record", { series_id: p.sid, mode: "next", start: p.s, channel: ch.number, show: p.t });
        }
      });
      button("Series", "", () => {
        if (window.confirm(`Record every new airing of ${p.t}?`)) {
          this._service("record", { series_id: p.sid, mode: "series", show: p.t });
        }
      });
    }
    return row;
  }

  async _service(service, data) {
    // only the newest press owns the message line (a cancelled older change finishes last)
    const seq = (this._svcSeq = (this._svcSeq || 0) + 1);
    const msg = this._guideContainer.querySelector(".gmsg");
    msg.className = "gmsg";
    msg.textContent = service === "tune" ? `Changing ${data.player} to ${data.channel}...` : `Scheduling ${data.show}...`;
    try {
      const r = await this._hass.callWS({
        type: "call_service", domain: "hdhr", service, service_data: data, return_response: true,
      });
      const res = (r && r.response) || {};
      if (seq !== this._svcSeq || res.superseded) return;
      msg.className = "gmsg " + (res.ok ? "ok" : "bad");
      msg.textContent = res.message || (res.ok ? "Done." : "That didn't work.");
    } catch (err) {
      if (seq !== this._svcSeq) return;
      msg.className = "gmsg bad";
      msg.textContent = String((err && err.message) || err);
    }
  }
}

customElements.define("hdhr-panel", HdhrPanel);
