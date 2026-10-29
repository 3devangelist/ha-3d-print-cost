/*
 * FDM Printer Manager — "FDM Printers" sidebar panel.
 *
 * Plain web component, no build step. It reads the printers from Home
 * Assistant's states and drives the integration's own config / options flows
 * through the REST API, so everything it creates is a normal config entry.
 */

const DOMAIN = "fdm_printer_manager";

const STYLE = `
:host{
  --bg:#12161b; --surface:#1a2027; --surface-2:#222a33; --line:#2d3743; --fg:#e7ecf1; --muted:#8c99a8;
  --accent:#f0a33a; --accent-ink:#1b1206; --heat:#ff7a45; --ok:#57c27a; --bad:#ef6a5a; --idle:#7d8a99; --run:#f0a33a;
  --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  display:block; min-height:100vh; background:var(--bg); color:var(--fg);
  font-family:"Barlow","Segoe UI",Roboto,ui-sans-serif,system-ui,sans-serif; font-size:15px; line-height:1.45;
  color-scheme:dark;
}
*{box-sizing:border-box}
.top{display:flex;align-items:center;gap:12px;padding:12px 20px;background:var(--surface);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:2}
.menu{all:unset;cursor:pointer;color:var(--muted);font-size:1.4rem;padding:2px 6px;display:none}
.menu.show{display:block}
.mark{width:30px;height:30px;border-radius:7px;background:var(--accent);color:var(--accent-ink);display:grid;place-items:center;font:700 .72rem var(--mono)}
.title{font-weight:700;font-size:1.15rem;letter-spacing:.01em}
.ver{font:500 .68rem var(--mono);color:var(--muted)}
.spacer{flex:1}
main{padding:20px;max-width:1300px}
h1{font-size:1.7rem;margin:0;font-weight:700}
.sub{color:var(--muted);font-size:.9rem}
.bar{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}
.btn{all:unset;box-sizing:border-box;cursor:pointer;font-weight:600;padding:9px 14px;border-radius:7px;border:1px solid var(--line);background:var(--surface-2);color:var(--fg);display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.btn:hover{border-color:var(--muted)}
.btn:focus-visible{outline:2px solid var(--accent)}
.btn.pri{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.btn.ghost{background:transparent}
.btn.danger{color:var(--bad)}
.btn[disabled]{opacity:.45;pointer-events:none}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px}
.pc{background:var(--surface);border:1px solid var(--line);border-radius:11px;overflow:hidden;display:flex;flex-direction:column}
.stripe{height:3px;background:var(--idle)}
.pc.run .stripe{background:var(--run)} .pc.ok .stripe{background:var(--ok)} .pc.off .stripe{background:var(--bad)}
.hd{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;padding:12px 14px 6px}
.nm{font-size:1.3rem;font-weight:700;line-height:1.1}
.ty{font:500 .68rem var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.state{font:600 .72rem var(--mono);text-transform:uppercase;letter-spacing:.06em;padding:3px 8px;border-radius:5px;background:var(--surface-2);color:var(--idle);white-space:nowrap}
.state.run{color:var(--run);background:rgba(240,163,58,.15)} .state.ok{color:var(--ok);background:rgba(87,194,122,.15)} .state.off{color:var(--bad);background:rgba(239,106,90,.15)}
.job{padding:0 14px;font-family:var(--mono);font-size:.78rem;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.prog{margin:8px 14px 0;height:6px;border-radius:3px;background:var(--surface-2);overflow:hidden}
.prog i{display:block;height:100%;background:var(--run)}
.meta{display:flex;justify-content:space-between;gap:8px;padding:4px 14px 0;font:500 .74rem var(--mono);color:var(--muted)}
.temps{padding:8px 14px 0;font-family:var(--mono);font-size:.85rem;color:var(--heat);min-height:1.4em}
.ft{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;margin-top:12px;background:var(--line);border-top:1px solid var(--line)}
.ft div{background:var(--surface);padding:8px 10px;min-width:0}
.k{font:500 .62rem var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.v{font-family:var(--mono);font-size:.88rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.act{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:9px 12px;border-top:1px solid var(--line);flex-wrap:wrap}
.sw{display:inline-flex;align-items:center;gap:8px;font-size:.85rem;color:var(--muted);cursor:pointer;background:none;border:0;padding:0;font-family:inherit}
.sw[disabled]{cursor:not-allowed;opacity:.6}
.tg{width:34px;height:18px;border-radius:9px;background:var(--line);position:relative;flex:none}
.tg::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:var(--fg);transition:left .2s}
.tg.on{background:var(--ok)} .tg.on::after{left:18px}
.links{display:flex;gap:12px;align-items:center}
a.link,.linkbtn{color:var(--accent);font-weight:600;font-size:.85rem;text-decoration:none;background:none;border:0;cursor:pointer;font-family:inherit;padding:0}
.addcard{all:unset;box-sizing:border-box;border:1.5px dashed var(--line);border-radius:11px;display:grid;place-items:center;min-height:240px;color:var(--muted);cursor:pointer;text-align:center}
.addcard:hover{border-color:var(--accent);color:var(--accent)}
.addcard b{display:block;font-size:1.2rem}
.empty{color:var(--muted);padding:30px 0}
.wiz{max-width:640px}
.steps{display:flex;gap:6px;margin-bottom:16px;flex-wrap:wrap}
.steps span{font:500 .72rem var(--mono);padding:4px 10px;border-radius:14px;border:1px solid var(--line);color:var(--muted)}
.steps span.on{border-color:var(--accent);color:var(--accent)}
.steps span.done{background:var(--surface-2);color:var(--fg)}
.types{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:16px}
@media (max-width:600px){.types{grid-template-columns:minmax(0,1fr)}}
.type{all:unset;box-sizing:border-box;cursor:pointer;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px;display:flex;flex-direction:column;gap:4px}
.type:hover{border-color:var(--muted)} .type:focus-visible{outline:2px solid var(--accent)}
.type.on{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.type[disabled]{opacity:.45;cursor:not-allowed}
.type b{font-size:1.2rem}
.type small{color:var(--muted)}
.soon{font:500 .62rem var(--mono);color:var(--muted);border:1px dashed var(--line);padding:1px 6px;border-radius:4px;text-transform:uppercase;align-self:flex-start}
.f{display:flex;flex-direction:column;gap:4px;margin-bottom:12px}
.f label{font-size:.82rem;color:var(--muted)}
.f input,.f select{background:var(--bg);border:1px solid var(--line);color:var(--fg);border-radius:7px;padding:10px 11px;font:400 .95rem var(--mono);min-width:0;width:100%}
.f input:focus,.f select:focus{outline:2px solid var(--accent);border-color:transparent}
.f .h{font-size:.76rem;color:var(--muted)}
.f .e{font-size:.8rem;color:var(--bad)}
.f.big input{font-size:1.2rem;padding:12px 14px}
.banner{border-radius:7px;padding:9px 11px;margin-bottom:12px;font-size:.9rem}
.banner.err{background:rgba(239,106,90,.14);color:var(--bad)}
.banner.ok{background:rgba(87,194,122,.14);color:var(--ok)}
.banner.info{background:var(--surface-2);color:var(--fg)}
.wbtns{display:flex;justify-content:space-between;gap:8px;margin-top:8px;flex-wrap:wrap}
details{margin:4px 0 12px;color:var(--muted);font-size:.88rem}
details summary{cursor:pointer}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:11px;padding:16px}
.confirm{background:rgba(239,106,90,.1);border:1px solid rgba(239,106,90,.4);border-radius:9px;padding:12px;margin-top:12px}
.busy{opacity:.6;pointer-events:none}
`;

const TYPE_INFO = {
  klipper: { label: "Klipper", hint: "Moonraker · just the IP address" },
  bambu_device: { label: "Bambu Lab", hint: "Through the Bambu Lab integration" },
};

const FIELD_LABELS = {
  host: "IP address",
  api_key: "API key",
  printer_name: "Name",
  port: "Moonraker port",
  web_url: "Web interface",
  printer_device_id: "Printer",
  print_status_entity: "Print status",
  job_name_entity: "Job / file name",
  progress_entity: "Print progress",
  remaining_time_entity: "Remaining time",
  cover_image_entity: "Cover image",
  switch_entity: "Smart plug",
  power_entity: "Power sensor",
  energy_entity: "Energy meter",
  price_entity: "Electricity price",
  integration_method: "Power integration method",
  update_interval: "Sampling interval (s)",
  currency: "Currency",
};

const ERRORS = {
  cannot_connect: "Cannot reach the printer. Check the IP address and that the printer is on.",
  invalid_auth: "The printer refused this API key.",
  price_required: "Select the electricity price: the plug measures energy.",
  unsupported_power_unit: "Unsupported unit (expected W or kW).",
  unsupported_energy_unit: "Unsupported unit (expected Wh, kWh or MWh).",
  unsupported_price_unit: "Unsupported unit (expected e.g. EUR/kWh).",
  already_configured: "This printer is already added.",
};

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const ACTIVE = ["printing", "paused", "running", "pause", "prepare", "slicing"];
const DONE = ["complete", "finish", "finished"];
const BAD = ["error", "failed", "cancelled", "cancel", "unavailable", "offline"];

function stateClass(s) {
  if (!s) return "";
  if (ACTIVE.includes(s)) return "run";
  if (DONE.includes(s)) return "ok";
  if (BAD.includes(s)) return "off";
  return "";
}

function num(state) {
  if (!state) return null;
  const n = parseFloat(state.state);
  return Number.isFinite(n) ? n : null;
}

function fmtMin(min) {
  if (min == null) return "—";
  const m = Math.round(min);
  return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}`;
}

class FdmPrinterPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._view = "list";
    this._registry = null; // entity registry entries of this integration
    this._entries = []; // config entries of this integration
    this._renderTimer = null;
    this._flow = null; // { kind: "config"|"options", id, step, result, entryId }
    this._data = {}; // form values of the current step
    this._busy = false;
    this._confirm = null;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._loadMeta();
    if (this._view === "list") this._scheduleRender();
  }

  set narrow(narrow) {
    this._narrow = narrow;
    if (this._view === "list") this._scheduleRender();
  }

  set panel(panel) {
    this._panel = panel;
  }

  connectedCallback() {
    this._render();
  }

  disconnectedCallback() {
    this._abortFlow();
  }

  // ------------------------------------------------------------------ data
  async _loadMeta() {
    try {
      const [registry, entries] = await Promise.all([
        this._hass.callWS({ type: "config/entity_registry/list" }),
        this._hass.callWS({ type: "config_entries/get", domain: DOMAIN }),
      ]);
      this._registry = registry.filter((e) => e.platform === DOMAIN);
      this._entries = entries;
    } catch (err) {
      console.error("FDM Printers: cannot load registry", err);
      this._registry = [];
    }
    if (this._view === "list") this._render();
  }

  _printers() {
    const printers = [];
    for (const entry of this._entries) {
      if (entry.domain !== DOMAIN) continue;
      const ents = {};
      for (const e of this._registry || []) {
        if (e.config_entry_id !== entry.entry_id) continue;
        const key = (e.unique_id || "").slice(entry.entry_id.length + 1);
        ents[key] = e.entity_id;
      }
      if (!ents.printer_status && !ents.current_print_cost) continue; // panel-only entry
      printers.push({ entry, ents });
    }
    return printers;
  }

  _st(id) {
    return id ? this._hass.states[id] : undefined;
  }

  // ------------------------------------------------------------------ render
  _scheduleRender() {
    if (this._renderTimer) return;
    this._renderTimer = setTimeout(() => {
      this._renderTimer = null;
      this._render();
    }, 500);
  }

  _render() {
    if (!this.shadowRoot) return;
    const top = `
      <div class="top">
        <button class="menu ${this._narrow ? "show" : ""}" id="menu" aria-label="Menu">☰</button>
        <span class="mark">FDM</span>
        <div><div class="title">FDM Printers</div><div class="ver">FDM Printer Manager · alpha</div></div>
        <span class="spacer"></span>
      </div>`;
    let body = "";
    if (!this._hass) body = `<main><p class="empty">Loading…</p></main>`;
    else if (this._view === "list") body = this._renderList();
    else body = this._renderFlow();
    this.shadowRoot.innerHTML = `<style>${STYLE}</style>${top}${body}`;
    this._bind();
  }

  _renderList() {
    if (this._registry === null) return `<main><p class="empty">Loading printers…</p></main>`;
    const printers = this._printers();
    const printing = printers.filter((p) => ACTIVE.includes(this._st(p.ents.printer_status)?.state)).length;
    const cards = printers.map((p) => this._card(p)).join("");
    return `
      <main>
        <div class="bar">
          <div><h1>Printers</h1><div class="sub">${printers.length} printer${printers.length === 1 ? "" : "s"} · ${printing} printing</div></div>
          <button class="btn pri" data-act="add">＋ Add printer</button>
        </div>
        ${printers.length ? "" : `<p class="empty">No printers yet. Add the first one.</p>`}
        <div class="grid">
          ${cards}
          <button class="addcard" data-act="add"><span><b>＋ Add printer</b>Klipper · Bambu Lab</span></button>
        </div>
      </main>`;
  }

  _card(p) {
    const s = (k) => this._st(p.ents[k]);
    const status = s("printer_status");
    const attrs = status?.attributes || {};
    const raw = status?.state;
    const shown = raw && !["unknown", "unavailable"].includes(raw) ? raw : attrs.reachable === false ? "offline" : raw || "—";
    const cls = stateClass(shown);
    const active = ACTIVE.includes(raw);
    const progress = num(s("print_progress"));
    const file = s("print_file")?.state;
    const left = num(s("print_time_left"));
    const nozzle = num(s("nozzle_temperature"));
    const curEnergy = num(s("current_print_energy"));
    const curCost = s("current_print_cost");
    const lastResult = s("last_print_result")?.state;
    const lastCost = s("last_print_cost");
    const duration = num(s("current_print_duration"));
    const unit = curCost?.attributes?.unit_of_measurement || lastCost?.attributes?.unit_of_measurement || "";
    const plug = this._st(attrs.switch_entity);
    const plugOn = plug?.state === "on";
    const type = attrs.printer_type === "klipper" ? "Klipper" : "Bambu Lab";
    const where = attrs.host ? ` · ${esc(attrs.host)}` : "";
    const fileShown = file && !["unknown", "unavailable"].includes(file) ? file : "—";
    const lastLine =
      lastResult && !["unknown", "unavailable"].includes(lastResult)
        ? `Last: ${lastResult === "success" ? "✅" : "❌"} ${esc(lastResult)}${num(lastCost) != null ? ` · ${num(lastCost).toFixed(2)} ${esc(unit)}` : ""}`
        : "";
    const cost = num(curCost);
    return `
      <article class="pc ${cls}">
        <div class="stripe"></div>
        <div class="hd"><div><div class="nm">${esc(p.entry.title)}</div><div class="ty">${type}${where}</div></div><span class="state ${cls}">${esc(shown)}</span></div>
        <div class="job">${esc(active ? fileShown : lastLine || fileShown)}</div>
        <div class="prog"><i style="width:${active && progress != null ? Math.min(100, progress) : 0}%"></i></div>
        <div class="meta"><span>${active && progress != null ? `${Math.round(progress)}%` : ""}</span><span>${active && left != null ? `${fmtMin(left)} left` : ""}</span></div>
        <div class="temps">${nozzle != null ? `▲ ${nozzle.toFixed(1)}°` : ""}</div>
        <div class="ft">
          <div><div class="k">Energy</div><div class="v">${active && curEnergy != null ? `${curEnergy.toFixed(2)} kWh` : "—"}</div></div>
          <div><div class="k">Cost</div><div class="v">${active && cost != null ? `${cost.toFixed(2)} ${esc(unit)}` : "—"}</div></div>
          <div><div class="k">Time</div><div class="v">${active && duration != null ? fmtMin(duration * 60) : "—"}</div></div>
        </div>
        <div class="act">
          ${
            attrs.switch_entity
              ? `<button class="sw" data-act="plug" data-entity="${esc(attrs.switch_entity)}" data-active="${active ? 1 : 0}" ${active ? "disabled title=\"Not while printing\"" : ""}><span class="tg ${plugOn ? "on" : ""}"></span>Plug ${plugOn ? "on" : plug ? "off" : "?"}</button>`
              : `<span class="sub">No plug</span>`
          }
          <span class="links">
            ${attrs.web_url ? `<a class="link" href="${esc(attrs.web_url)}" target="_blank" rel="noopener">Open web UI ↗</a>` : ""}
            <button class="linkbtn" data-act="edit" data-entry="${esc(p.entry.entry_id)}">Edit</button>
          </span>
        </div>
      </article>`;
  }

  // ------------------------------------------------------------------ flows
  _flowBase() {
    return this._flow?.kind === "options" ? "config/config_entries/options/flow" : "config/config_entries/flow";
  }

  async _startAdd() {
    this._view = "flow";
    this._flow = { kind: "config", title: "Add printer" };
    this._data = {};
    this._busy = true;
    this._render();
    try {
      const result = await this._hass.callApi("POST", "config/config_entries/flow", {
        handler: DOMAIN,
        show_advanced_options: false,
      });
      this._setResult(result);
    } catch (err) {
      this._flow.error = String(err?.body?.message || err?.message || err);
    }
    this._busy = false;
    this._render();
  }

  async _startEdit(entryId) {
    const entry = this._entries.find((e) => e.entry_id === entryId);
    this._view = "flow";
    this._flow = { kind: "options", title: `Edit · ${entry ? entry.title : ""}`, entryId };
    this._data = {};
    this._busy = true;
    this._confirm = null;
    this._render();
    try {
      const result = await this._hass.callApi("POST", "config/config_entries/options/flow", { handler: entryId });
      this._setResult(result);
    } catch (err) {
      this._flow.error = String(err?.body?.message || err?.message || err);
    }
    this._busy = false;
    this._render();
  }

  _setResult(result) {
    this._flow.id = result.flow_id || this._flow.id;
    this._flow.result = result;
    this._data = {};
    if (result.type === "form") {
      for (const field of result.data_schema || []) {
        const suggested = field.description?.suggested_value;
        if (suggested !== undefined && suggested !== null) this._data[field.name] = suggested;
        else if (field.default !== undefined) this._data[field.name] = field.default;
      }
      if (result.step_id === "electricity") this._prefillPlug();
    }
  }

  async _submit(payload) {
    if (!this._flow?.id) return;
    this._busy = true;
    this._render();
    try {
      const result = await this._hass.callApi("POST", `${this._flowBase()}/${this._flow.id}`, payload);
      this._setResult(result);
      if (result.type === "create_entry" || result.type === "abort") {
        this._flow.id = null;
        await this._loadMeta();
      }
    } catch (err) {
      this._flow.error = String(err?.body?.message || err?.message || err);
    }
    this._busy = false;
    this._render();
  }

  async _abortFlow() {
    const flow = this._flow;
    if (!flow?.id || !this._hass) return;
    const id = flow.id;
    flow.id = null;
    try {
      await this._hass.callApi("DELETE", `${flow.kind === "options" ? "config/config_entries/options/flow" : "config/config_entries/flow"}/${id}`);
    } catch (_err) {
      /* flow already gone */
    }
  }

  _backToList() {
    const flow = this._flow;
    if (flow?.id) {
      const base = flow.kind === "options" ? "config/config_entries/options/flow" : "config/config_entries/flow";
      this._hass.callApi("DELETE", `${base}/${flow.id}`).catch(() => {});
    }
    this._flow = null;
    this._confirm = null;
    this._view = "list";
    this._loadMeta();
    this._render();
  }

  _stepLabel() {
    const step = this._flow?.result?.step_id;
    if (this._flow?.kind === "options") return null;
    const map = { user: 1, klipper: 1, klipper_auth: 1, bambu_device: 1, bambu: 1, electricity: 2 };
    return map[step] || (this._flow?.result?.type === "create_entry" ? 3 : 1);
  }

  _renderFlow() {
    const f = this._flow || {};
    const r = f.result;
    let inner = "";
    if (f.error) inner = `<div class="banner err">${esc(f.error)}</div><div class="wbtns"><button class="btn" data-act="back">← Back to printers</button></div>`;
    else if (!r) inner = `<p class="empty">Loading…</p>`;
    else if (r.type === "menu") inner = this._renderMenu(r);
    else if (r.type === "form") inner = this._renderForm(r);
    else if (r.type === "create_entry") {
      const done = f.kind === "options" ? "Saved. The printer keeps its history." : `Added <b>${esc(r.title)}</b>.`;
      inner = `<div class="banner ok">${done}</div><div class="wbtns"><span></span><button class="btn pri" data-act="back">Back to printers</button></div>`;
    } else if (r.type === "abort") {
      inner = `<div class="banner err">${esc(ERRORS[r.reason] || r.reason)}</div><div class="wbtns"><button class="btn" data-act="back">← Back to printers</button></div>`;
    } else inner = `<div class="banner info">Unexpected step: ${esc(r.type)}</div>`;

    const n = this._stepLabel();
    const steps =
      n == null
        ? ""
        : `<div class="steps">${["Printer", "Plug", "Done"]
            .map((t, i) => `<span class="${i + 1 === n ? "on" : i + 1 < n ? "done" : ""}">${i + 1} · ${t}</span>`)
            .join("")}</div>`;
    const remove =
      f.kind === "options"
        ? `<div style="margin-top:22px">${
            this._confirm === "remove"
              ? `<div class="confirm">Remove this printer from Home Assistant? Its sensors and print history go too. The smart plug is not touched.<div class="wbtns"><button class="btn" data-act="noremove">Keep it</button><button class="btn danger" data-act="doremove">Remove printer</button></div></div>`
              : `<button class="btn danger" data-act="askremove">Remove printer</button>`
          }</div>`
        : "";
    return `
      <main>
        <div class="bar"><div><h1>${esc(f.title || "")}</h1><div class="sub">${f.kind === "options" ? "Changes keep the printer's history." : "Everything in one place."}</div></div>
          <button class="btn ghost" data-act="back">✕ Cancel</button></div>
        <div class="wiz ${this._busy ? "busy" : ""}">${steps}${inner}${remove}</div>
      </main>`;
  }

  _renderMenu(r) {
    const options = (r.menu_options || []).filter((o) => o !== "panel");
    const cards = options
      .map((o) => {
        const info = TYPE_INFO[o] || { label: o, hint: "" };
        return `<button class="type" data-act="menu" data-step="${esc(o)}"><b>${esc(info.label)}</b><small>${esc(info.hint)}</small></button>`;
      })
      .join("");
    return `<div class="types">${cards}<button class="type" disabled><b>OctoPrint</b><small>Older machines</small><span class="soon">planned</span></button></div>`;
  }

  _renderForm(r) {
    const errors = r.errors || {};
    const base = errors.base ? `<div class="banner err">${esc(ERRORS[errors.base] || errors.base)}</div>` : "";
    const hint = {
      klipper: `<div class="banner info">Type the printer's IP address. Name, port and web interface are filled in automatically.</div>`,
      klipper_auth: `<div class="banner info">This printer only accepts connections with a Moonraker API key.</div>`,
      electricity: `<div class="banner info">Pick the plug the printer is connected to. Its power and energy sensors are found automatically.</div>`,
    }[r.step_id] || "";
    const schema = r.data_schema || [];
    const main = [];
    const advanced = [];
    for (const field of schema) {
      const html = this._field(field, errors[field.name]);
      if (r.step_id === "electricity" && field.name === "integration_method") advanced.push(html);
      else main.push(html);
    }
    const adv = advanced.length ? `<details><summary>Advanced</summary>${advanced.join("")}</details>` : "";
    let submitLabel = "Next →";
    if (this._flow.kind === "options" && r.step_id === "behaviour") submitLabel = "Save";
    if (this._flow.kind === "config" && r.step_id === "electricity") submitLabel = "Add printer";
    return `${hint}${base}<form id="form">${main.join("")}${adv}<div class="wbtns"><span></span><button class="btn pri" type="submit">${submitLabel}</button></div></form>`;
  }

  _field(field, error) {
    const name = field.name;
    const label = FIELD_LABELS[name] || name;
    const sel = field.selector || {};
    const value = this._data[name];
    const err = error ? `<span class="e">${esc(ERRORS[error] || error)}</span>` : "";
    const req = field.required ? "" : ` <span class="h">(optional)</span>`;
    const big = name === "host" ? " big" : "";
    if (sel.entity) {
      const ids = this._entityOptions(sel.entity, name);
      const opts = [`<option value="">— none —</option>`]
        .concat(
          ids.map((id) => {
            const st = this._hass.states[id];
            const fn = st?.attributes?.friendly_name;
            const text = fn && fn !== id ? `${fn} · ${id}` : id;
            return `<option value="${esc(id)}" ${id === value ? "selected" : ""}>${esc(text)}</option>`;
          }),
        )
        .join("");
      return `<div class="f"><label for="f-${esc(name)}">${esc(label)}${req}</label><select id="f-${esc(name)}" data-name="${esc(name)}">${opts}</select>${err}</div>`;
    }
    if (sel.device) {
      const devs = this._deviceOptions(sel.device);
      const opts = [`<option value="">— choose —</option>`]
        .concat(devs.map((d) => `<option value="${esc(d.id)}" ${d.id === value ? "selected" : ""}>${esc(d.name)}</option>`))
        .join("");
      return `<div class="f"><label for="f-${esc(name)}">${esc(label)}</label><select id="f-${esc(name)}" data-name="${esc(name)}">${opts}</select>${err}</div>`;
    }
    if (sel.select) {
      const options = (sel.select.options || []).map((o) => (typeof o === "string" ? { value: o, label: o } : o));
      const opts = options.map((o) => `<option value="${esc(o.value)}" ${o.value === value ? "selected" : ""}>${esc(o.label)}</option>`).join("");
      return `<div class="f"><label for="f-${esc(name)}">${esc(label)}</label><select id="f-${esc(name)}" data-name="${esc(name)}">${opts}</select>${err}</div>`;
    }
    if (sel.number) {
      return `<div class="f"><label for="f-${esc(name)}">${esc(label)}${req}</label><input id="f-${esc(name)}" data-name="${esc(name)}" data-num="1" type="number" value="${esc(value ?? "")}">${err}</div>`;
    }
    const type = sel.text?.type === "password" ? "password" : "text";
    const ph = name === "host" ? "192.168.1.89" : "";
    return `<div class="f${big}"><label for="f-${esc(name)}">${esc(label)}${req}</label><input id="f-${esc(name)}" data-name="${esc(name)}" type="${type}" value="${esc(value ?? "")}" placeholder="${ph}" autocomplete="off">${err}</div>`;
  }

  _entityOptions(cfg, fieldName) {
    // Selector config comes either as {domain, device_class} or as {filter:[{domain, device_class}]}.
    const filters = Array.isArray(cfg.filter) && cfg.filter.length ? cfg.filter : [cfg];
    const asList = (v) => (v == null ? null : Array.isArray(v) ? v : [v]);
    const ok = (id) => {
      const st = this._hass.states[id];
      return filters.some((f) => {
        const domains = asList(f.domain);
        const classes = asList(f.device_class);
        if (domains && !domains.includes(id.split(".")[0])) return false;
        if (classes && !classes.includes(st?.attributes?.device_class)) return false;
        return true;
      });
    };
    let ids = Object.keys(this._hass.states).filter(ok);
    if (fieldName === "price_entity") ids = ids.filter((id) => /(€|eur|\/kwh|\/mwh)/i.test(this._hass.states[id]?.attributes?.unit_of_measurement || "") || id.startsWith("input_number."));
    return ids.sort();
  }

  _deviceOptions(cfg) {
    const filters = Array.isArray(cfg.filter) && cfg.filter.length ? cfg.filter : [cfg];
    const integrations = filters.map((f) => f.integration).filter(Boolean);
    const devices = Object.values(this._hass.devices || {});
    return devices
      .filter((d) => !integrations.length || (d.identifiers || []).some(([domain]) => integrations.includes(domain)))
      .map((d) => ({ id: d.id, name: d.name_by_user || d.name || d.id }))
      .sort((a, b) => a.name.localeCompare(b.name));
  }

  /** Fill power / energy / price from the chosen plug's device and the other printers. */
  _prefillPlug() {
    const switchId = this._data.switch_entity;
    if (!this._data.price_entity) {
      const known = Object.values(this._hass.states).find((s) => s.attributes?.printer_type && s.attributes?.price_entity);
      if (known) this._data.price_entity = known.attributes.price_entity;
    }
    if (!switchId) return;
    const ents = this._hass.entities || {};
    const deviceId = ents[switchId]?.device_id;
    if (!deviceId) return;
    for (const [id, e] of Object.entries(ents)) {
      if (e.device_id !== deviceId) continue;
      const dc = this._hass.states[id]?.attributes?.device_class;
      if (dc === "power" && !this._data.power_entity) this._data.power_entity = id;
      if (dc === "energy" && !this._data.energy_entity) this._data.energy_entity = id;
    }
  }

  _collect() {
    const out = {};
    const schema = this._flow?.result?.data_schema || [];
    for (const field of schema) {
      const el = this.shadowRoot.querySelector(`[data-name="${CSS.escape(field.name)}"]`);
      if (!el) continue;
      let v = el.value;
      if (el.dataset.num) v = v === "" ? undefined : Number(v);
      else v = typeof v === "string" ? v.trim() : v;
      if (v === "" || v === undefined) continue;
      out[field.name] = v;
    }
    return out;
  }

  // ------------------------------------------------------------------ events
  _bind() {
    const root = this.shadowRoot;
    root.getElementById("menu")?.addEventListener("click", () =>
      this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true })),
    );
    root.querySelectorAll("[data-act]").forEach((el) =>
      el.addEventListener("click", (ev) => {
        const act = el.dataset.act;
        if (act === "add") this._startAdd();
        else if (act === "edit") this._startEdit(el.dataset.entry);
        else if (act === "back") this._backToList();
        else if (act === "menu") this._submit({ next_step_id: el.dataset.step });
        else if (act === "plug") this._togglePlug(el);
        else if (act === "askremove") {
          this._confirm = "remove";
          this._render();
        } else if (act === "noremove") {
          this._confirm = null;
          this._render();
        } else if (act === "doremove") this._remove();
        ev.preventDefault();
      }),
    );
    const form = root.getElementById("form");
    if (form) {
      form.addEventListener("submit", (ev) => {
        ev.preventDefault();
        this._submit(this._collect());
      });
      const plug = root.querySelector('[data-name="switch_entity"]');
      plug?.addEventListener("change", () => {
        Object.assign(this._data, this._collect());
        this._data.switch_entity = plug.value;
        delete this._data.power_entity;
        delete this._data.energy_entity;
        this._prefillPlug();
        this._render();
      });
      const host = root.querySelector('[data-name="host"]');
      if (host) host.focus();
    }
  }

  async _togglePlug(el) {
    if (el.dataset.active === "1") return;
    const entity = el.dataset.entity;
    const domain = entity.split(".")[0];
    try {
      await this._hass.callService(domain, "toggle", { entity_id: entity });
    } catch (err) {
      console.error("FDM Printers: cannot toggle plug", err);
    }
  }

  async _remove() {
    const entryId = this._flow?.entryId;
    if (!entryId) return;
    this._busy = true;
    this._render();
    try {
      if (this._flow.id) {
        await this._hass.callApi("DELETE", `config/config_entries/options/flow/${this._flow.id}`).catch(() => {});
        this._flow.id = null;
      }
      await this._hass.callApi("DELETE", `config/config_entries/entry/${entryId}`);
      this._flow = null;
      this._confirm = null;
      this._view = "list";
      await this._loadMeta();
    } catch (err) {
      this._flow.error = String(err?.body?.message || err?.message || err);
    }
    this._busy = false;
    this._render();
  }
}

if (!customElements.get("fdm-printer-panel")) customElements.define("fdm-printer-panel", FdmPrinterPanel);
