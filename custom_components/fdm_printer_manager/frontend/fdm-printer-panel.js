/*
 * FDM Printer Manager — "FDM Printers" sidebar panel.
 *
 * Plain web component, no build step. It reads the printers from Home
 * Assistant's states, drives the integration's own config / options flows
 * through the REST API (so everything it creates is a normal config entry),
 * and reads the print history through the `fdm_printer_manager/history`
 * websocket command.
 */

const DOMAIN = "fdm_printer_manager";
// How long the power toggle stays orange ("wait") after "Turn off" while waiting for the switch to answer.
const OFFING_MS = 20000;

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
.top{display:flex;align-items:center;gap:12px;padding:12px 20px;background:var(--surface);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:3}
.menu{all:unset;cursor:pointer;color:var(--muted);font-size:1.4rem;padding:2px 6px;display:none}
.menu.show{display:block}
.mark{width:30px;height:30px;border-radius:7px;background:var(--accent);color:var(--accent-ink);display:grid;place-items:center;font:700 .72rem var(--mono)}
.title{font-weight:700;font-size:1.15rem;letter-spacing:.01em}
.ver{font:500 .68rem var(--mono);color:var(--muted)}
.spacer{flex:1}
main{padding:20px;max-width:1300px}
h1{font-size:1.7rem;margin:0;font-weight:700}
h2{font-size:1.2rem;margin:28px 0 10px;font-weight:700;display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.sub{color:var(--muted);font-size:.9rem}
.bar{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}
.btn{all:unset;box-sizing:border-box;cursor:pointer;font-weight:600;padding:9px 14px;border-radius:7px;border:1px solid var(--line);background:var(--surface-2);color:var(--fg);display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.btn:hover{border-color:var(--muted)}
.btn:focus-visible{outline:2px solid var(--accent)}
.btn.pri{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.btn.ghost{background:transparent}
.btn.danger{color:var(--bad)}
.btn.sm{padding:5px 10px;font-size:.82rem}
.btn[disabled]{opacity:.45;pointer-events:none}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:14px}
.pc{background:var(--surface);border:1px solid var(--line);border-radius:11px;overflow:hidden;display:flex;flex-direction:column}
.stripe{height:3px;background:var(--idle)}
.pc.run .stripe{background:var(--run)} .pc.ok .stripe{background:var(--ok)} .pc.off .stripe{background:var(--bad)}
.hd{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;padding:12px 14px 6px}
.nm{font-size:1.3rem;font-weight:700;line-height:1.1}
.ty{font:500 .68rem var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.state{font:600 .72rem var(--mono);text-transform:uppercase;letter-spacing:.06em;padding:3px 8px;border-radius:5px;background:var(--surface-2);color:var(--idle);white-space:nowrap}
.state.run{color:var(--run);background:rgba(240,163,58,.15)} .state.ok{color:var(--ok);background:rgba(87,194,122,.15)} .state.off{color:var(--bad);background:rgba(239,106,90,.15)}
.job{padding:0 14px;font-family:var(--mono);font-size:.78rem;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mark{display:flex;gap:6px;padding:6px 14px 0}
.rmark{display:inline-flex;gap:6px;margin-left:6px}
.stats{margin:-4px 0 8px}
.alert{margin:0 14px 8px;padding:6px 10px;border-radius:8px;background:rgba(219,68,55,.12);color:var(--error-color,#db4437);font-size:.85rem;font-weight:600}
.prog{margin:8px 14px 0;height:6px;border-radius:3px;background:var(--surface-2);overflow:hidden}
.prog i{display:block;height:100%;background:var(--run)}
.meta{display:flex;justify-content:space-between;gap:8px;padding:4px 14px 0;font:500 .74rem var(--mono);color:var(--muted);min-height:1.5em}
.temps{padding:6px 14px 0;font-family:var(--mono);font-size:.85rem;color:var(--heat);min-height:1.5em}
.ctrl{display:flex;gap:6px;padding:8px 14px 0;flex-wrap:wrap}
.btn.light.on{border-color:var(--accent);color:var(--accent)}
.fil{display:flex;gap:6px;padding:8px 14px 0;flex-wrap:wrap}
.chip{display:inline-flex;align-items:center;gap:5px;padding:3px 8px;border-radius:12px;border:1px solid var(--line);background:var(--surface-2);font:500 .74rem var(--mono)}
.chip i{width:11px;height:11px;border-radius:50%;border:1px solid var(--muted);flex:none}
.chip.act{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.chip.empty{opacity:.5}
.chip small{color:var(--muted)}
.z{display:flex;gap:6px;align-items:center;padding:8px 14px 0;flex-wrap:wrap}
.z b{font-family:var(--mono);font-size:.85rem;min-width:7.5em}
.stp{all:unset;cursor:pointer;font:500 .7rem var(--mono);padding:2px 5px;border-radius:4px;color:var(--muted)}
.stp.on{color:var(--accent-ink);background:var(--accent)}
.ft{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;margin-top:12px;background:var(--line);border-top:1px solid var(--line)}
.ft div{background:var(--surface);padding:8px 8px;min-width:0}
.k{font:500 .62rem var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.v{font-family:var(--mono);font-size:.8rem;white-space:nowrap}
.act{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:9px 12px;border-top:1px solid var(--line);flex-wrap:wrap}
.sw{display:inline-flex;align-items:center;gap:8px;font-size:.85rem;color:var(--muted);cursor:pointer;background:none;border:0;padding:0;font-family:inherit}
.sw[disabled]{cursor:not-allowed;opacity:.6}
.tg{width:34px;height:18px;border-radius:9px;background:var(--line);position:relative;flex:none}
.tg::after{content:"";position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:var(--fg);transition:left .2s}
.tg.on{background:var(--ok)} .tg.on::after{left:18px}
.tg.wait{background:var(--run)}
.links{display:flex;gap:12px;align-items:center}
a.link,.linkbtn{color:var(--accent);font-weight:600;font-size:.85rem;text-decoration:none;background:none;border:0;cursor:pointer;font-family:inherit;padding:0}
.ask{margin:8px 12px 10px;background:rgba(239,106,90,.1);border:1px solid rgba(239,106,90,.4);border-radius:8px;padding:10px;font-size:.88rem}
.ask ol{margin:6px 0 6px 18px;padding:0} .ask li{margin:2px 0} .ask p{margin:6px 0 0}
.ask .row{display:flex;gap:8px;justify-content:flex-end;margin-top:8px;flex-wrap:wrap}
.empty{color:var(--muted);padding:24px 0}
.tablewrap{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:11px}
table{width:100%;border-collapse:collapse;font-size:.86rem}
th{text-align:left;font:500 .64rem var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.08em;padding:9px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
tr:last-child td{border-bottom:0}
td.m{font-family:var(--mono);font-size:.8rem;max-width:340px;overflow:hidden;text-overflow:ellipsis}
td.n{font-family:var(--mono);text-align:right}
.mini{width:90px;height:5px;border-radius:3px;background:var(--surface-2);overflow:hidden;display:inline-block;vertical-align:middle;margin-right:6px}
.mini i{display:block;height:100%;background:var(--run)}
.filters{display:flex;gap:8px;align-items:center}
.filters select{background:var(--bg);border:1px solid var(--line);color:var(--fg);border-radius:7px;padding:6px 8px;font:inherit}
.more{display:flex;justify-content:center;padding:10px}
.wiz{max-width:660px}
.steps{display:flex;gap:6px;margin-bottom:16px;flex-wrap:wrap}
.steps span{font:500 .72rem var(--mono);padding:4px 10px;border-radius:14px;border:1px solid var(--line);color:var(--muted)}
.steps span.on{border-color:var(--accent);color:var(--accent)}
.steps span.done{background:var(--surface-2);color:var(--fg)}
.types{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:16px}
@media (max-width:600px){.types{grid-template-columns:minmax(0,1fr)}}
.type{all:unset;box-sizing:border-box;cursor:pointer;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px;display:flex;flex-direction:column;gap:4px}
.type:hover{border-color:var(--muted)} .type:focus-visible{outline:2px solid var(--accent)}
.type[disabled]{opacity:.45;cursor:not-allowed}
.type b{font-size:1.2rem}
.type small{color:var(--muted)}
.soon{font:500 .62rem var(--mono);color:var(--muted);border:1px dashed var(--line);padding:1px 6px;border-radius:4px;text-transform:uppercase;align-self:flex-start}
.f{display:flex;flex-direction:column;gap:4px;margin-bottom:14px;position:relative}
.f label{font-size:.82rem;color:var(--muted)}
.f label b{color:var(--fg);font-weight:600}
.f input{background:var(--bg);border:1px solid var(--line);color:var(--fg);border-radius:7px;padding:10px 11px;font:400 .95rem var(--mono);min-width:0;width:100%}
.f input:focus,.f select:focus{outline:2px solid var(--accent);border-color:transparent}
.f label.chk{display:flex;align-items:center;gap:8px;color:var(--fg);font-size:.9rem}
.f label.chk input{width:auto}
.f select{background:var(--bg);border:1px solid var(--line);color:var(--fg);border-radius:7px;padding:10px 11px;font:400 .95rem var(--mono);width:100%}
.f .h{font-size:.76rem;color:var(--muted)}
.f .e{font-size:.8rem;color:var(--bad)}
.f.big input{font-size:1.2rem;padding:12px 14px}
.unitwrap{display:flex;align-items:center;gap:8px}
.unitwrap input{max-width:180px}
.cb-list{position:absolute;left:0;right:0;top:100%;margin-top:2px;background:var(--surface);border:1px solid var(--line);border-radius:8px;max-height:300px;overflow:auto;z-index:5;box-shadow:0 12px 24px -12px rgba(0,0,0,.7)}
.cb-list[hidden]{display:none}
.cb-grp{font:500 .64rem var(--mono);color:var(--muted);text-transform:uppercase;letter-spacing:.08em;padding:8px 10px 4px}
.cb-opt{padding:7px 10px;cursor:pointer;display:flex;flex-direction:column}
.cb-opt small{font-family:var(--mono);color:var(--muted);font-size:.74rem}
.cb-opt.hl{background:var(--surface-2)}
.cb-opt.sel b::after{content:" ✓";color:var(--accent)}
.cb-none{padding:10px;color:var(--muted);font-size:.85rem}
.summary{display:flex;align-items:center;justify-content:space-between;gap:10px;background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-bottom:14px}
.banner{border-radius:7px;padding:9px 11px;margin-bottom:12px;font-size:.9rem}
.banner.err{background:rgba(239,106,90,.14);color:var(--bad)}
.banner.ok{background:rgba(87,194,122,.14);color:var(--ok)}
.banner.info{background:var(--surface-2);color:var(--fg)}
.banner.warn{background:rgba(255,152,0,.15);color:var(--fg)}
.wbtns{display:flex;justify-content:space-between;gap:8px;margin-top:8px;flex-wrap:wrap}
details{margin:4px 0 12px;color:var(--muted);font-size:.88rem}
details summary{cursor:pointer;margin-bottom:10px}
.confirm{background:rgba(239,106,90,.1);border:1px solid rgba(239,106,90,.4);border-radius:9px;padding:12px;margin-top:12px}
.busy{opacity:.6;pointer-events:none}
.toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:var(--surface-2);border:1px solid var(--line);color:var(--fg);padding:10px 14px;border-radius:8px;z-index:9;max-width:90vw}
.cam{margin:8px 14px 0;aspect-ratio:16/9;border-radius:8px;overflow:hidden;background:#000;cursor:zoom-in;position:relative}
.cam img{width:100%;height:100%;object-fit:contain;display:block}
.cam .live{position:absolute;top:6px;left:6px;font:600 .62rem var(--mono);letter-spacing:.08em;background:rgba(0,0,0,.6);color:var(--run);padding:2px 6px;border-radius:4px}
.zoom{position:fixed;inset:0;z-index:8;background:rgba(0,0,0,.85);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;padding:16px}
.zoom .zcam{width:min(100%,1200px);max-height:calc(100vh - 90px);display:flex;justify-content:center}
.zoom img{max-width:100%;max-height:calc(100vh - 90px);object-fit:contain;border-radius:8px;background:#000}
.zoom .zhd{width:min(100%,1200px);display:flex;justify-content:space-between;align-items:center;color:var(--fg);font-weight:700}
.photo{background:var(--surface);border:1px solid var(--line);border-radius:9px;padding:12px;margin-bottom:14px;display:flex;flex-direction:column;gap:8px}
.photo .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.photo .h{font-size:.8rem;color:var(--muted)}
.photo .st{font-size:.88rem}
.photo .st.err{color:var(--bad)} .photo .st.ok{color:var(--ok)}
.f.filled input{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.f .w{font-size:.8rem;color:var(--run)}
`;

// "Read from photo" (Bambu LAN): the photo is made smaller first; without an AI Task it is read in the browser.
const PHOTO_MAX_PX = 1600;
const TESSERACT_URL = "https://cdn.jsdelivr.net/npm/tesseract.js@5.1.1/dist/tesseract.min.js";
const LAN_GUIDE_URL = "https://github.com/3devangelist/ha-3d-print-cost/blob/main/docs/bambu-lan-only.md";
const PHOTO_FIELD_HINT = {
  host: "an IPv4 address like 192.168.1.96",
  printer_serial: "15 or 18 letters and digits",
  access_code: "8 characters",
};

// A printer's camera on its card: live while printing, a still image refreshed every minute otherwise.
const SNAPSHOT_EVERY_MS = 60000;

const TYPE_INFO = {
  klipper: { label: "Klipper", hint: "Moonraker · just the IP address" },
  bambu_lan: { label: "Bambu Lab (LAN)", hint: "Direct · IP, serial number and access code" },
  bambu_device: { label: "Bambu Lab (via Bambu Lab integration)", hint: "Uses a printer already set up in ha-bambulab" },
};

// Forms with "Choose photo" (a photo of the printer's LAN Only page fills them in).
const PHOTO_STEPS = ["bambu_lan", "reauth_confirm"];

const FIELD_LABELS = {
  host: "IP address",
  api_key: "API key",
  printer_serial: "Serial number",
  access_code: "Access code",
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
  price_per_kwh: "Price per kWh",
  price_entity: "Price entity (tariffs that change)",
  update_interval: "Sampling interval (s)",
  currency: "Currency",
  alerts: "Watch prints (printer down, interrupted or stuck print)",
  down_minutes: "Warn when the printer stops answering for (min)",
  close_minutes: "Close an interrupted print as failed after (min)",
  stall_minutes: "Warn when the progress does not move for (min, 0 = never)",
  notify_services: "Also send to (e.g. notify.mobile_app_phone)",
  todo_entity: "To-do list: add “Remove the print” when a print ends",
  end_script: "Script to run when a print ends",
};

const ERRORS = {
  cannot_connect: "Cannot reach the printer. Check the IP address and that the printer is on.",
  invalid_auth: "The printer refused this API key.",
  price_required: "Type the price per kWh: this plug measures energy.",
  unsupported_power_unit: "Unsupported unit (expected W or kW).",
  unsupported_energy_unit: "Unsupported unit (expected Wh, kWh or MWh).",
  unsupported_price_unit: "Unsupported unit (expected e.g. EUR/kWh).",
  already_configured: "This printer is already added.",
};

const ACTIVE = ["printing", "paused", "running", "pause", "prepare", "slicing"];
const DONE = ["complete", "finish", "finished"];
const BAD = ["error", "failed", "cancelled", "cancel", "unavailable", "offline"];
const RESULT_ICON = { success: "✅", failed: "❌", cancelled: "⛔", unknown: "❔" };

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const norm = (v) =>
  String(v ?? "")
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase();

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
function fmtWhen(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const now = new Date();
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return hm;
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return `yesterday ${hm}`;
  return `${d.toLocaleDateString([], { day: "2-digit", month: "2-digit" })} ${hm}`;
}
const money = (v, unit) => (v == null ? "—" : `${Number(v).toFixed(2)} ${esc(unit || "")}`.trim());
const kwh = (v, d = 2) => (v == null ? "—" : `${Number(v).toFixed(d)} kWh`);

class FdmPrinterPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._view = "list";
    this._registry = null;
    this._entries = [];
    this._renderTimer = null;
    this._flow = null;
    this._data = {};
    this._busy = false;
    this._confirm = null; // "remove" in the edit view
    this._ask = null; // {kind:"power"|"cancel", entry}
    this._history = { rows: [], total: 0, limit: 10, filter: "" };
    this._showPrice = false;
    this._toast = null;
    this._scanning = false;
    this._cams = new Map(); // key -> {img, mode, at}; kept across renders so a live stream is not restarted
    this._zoom = null; // camera entity shown full size
    this._photo = { ai: undefined, status: "", kind: "", filled: {}, warnings: [], reading: false };
    this._onVisibility = () => (document.hidden ? this._stopCams() : this._attachCams());
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
    this._historyTimer = setInterval(() => this._view === "list" && this._loadHistory(), 60000);
    this._camTimer = setInterval(() => this._attachCams(), 15000);
    document.addEventListener("visibilitychange", this._onVisibility);
  }
  disconnectedCallback() {
    clearInterval(this._historyTimer);
    clearInterval(this._camTimer);
    document.removeEventListener("visibilitychange", this._onVisibility);
    this._stopCams();
    this._dropFlow();
  }

  // ------------------------------------------------------------------ cameras
  /** URL of a camera entity (camera.*: live stream or one still) or of a ha-bambulab image entity. */
  _camUrl(entity, live) {
    const st = this._st(entity);
    if (!st || st.state === "unavailable") return null;
    if (entity.startsWith("image.")) return st.attributes.entity_picture || null;
    const token = st.attributes.access_token;
    if (!token) return null;
    return live
      ? `/api/camera_proxy_stream/${entity}?token=${token}`
      : `/api/camera_proxy/${entity}?token=${token}&t=${Date.now()}`;
  }

  /** Put the kept <img> elements back into the placeholders of the last render. */
  _attachCams() {
    if (!this.shadowRoot || !this._hass) return;
    const seen = new Set();
    if (!document.hidden) {
      this.shadowRoot.querySelectorAll("[data-cam]").forEach((box) => {
        const entity = box.dataset.cam;
        const live = box.dataset.live === "1" && !entity.startsWith("image.");
        const key = `${box.dataset.slot || "card"}:${entity}`;
        seen.add(key);
        let cam = this._cams.get(key);
        if (!cam) {
          const img = document.createElement("img");
          img.alt = "Printer camera";
          img.decoding = "async";
          cam = { img, mode: null, at: 0, url: null };
          this._cams.set(key, cam);
        }
        const mode = live ? "live" : "still";
        const now = Date.now();
        const imageEntityChanged = entity.startsWith("image.") && cam.url !== this._camUrl(entity, false);
        if (cam.mode !== mode || (mode === "still" && (now - cam.at >= SNAPSHOT_EVERY_MS || imageEntityChanged))) {
          const url = this._camUrl(entity, live);
          if (url) {
            cam.img.src = url;
            cam.url = url;
            cam.mode = mode;
            cam.at = now;
          }
        }
        if (cam.img.parentNode !== box) box.prepend(cam.img);
      });
    }
    for (const [key, cam] of this._cams) {
      if (seen.has(key)) continue;
      cam.img.removeAttribute("src"); // closes the live stream
      this._cams.delete(key);
    }
  }

  _stopCams() {
    for (const cam of this._cams.values()) cam.img.removeAttribute("src");
    this._cams.clear();
  }

  /** Full-size view of one camera, live, over the page. */
  _renderZoom() {
    if (!this._zoom || !this._avail(this._zoom)) return "";
    const printer = this._printers().find((p) => this._st(p.ents.printer_status)?.attributes?.camera_entity === this._zoom);
    return `<div class="zoom" data-act="zoomclose"><div class="zhd"><span>${esc(printer?.entry.title || "Camera")}</span><button class="btn sm" data-act="zoomclose">✕ Close</button></div><div class="zcam" data-cam="${esc(this._zoom)}" data-slot="zoom" data-live="1"></div></div>`;
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
      await this._loadDiscovered();
    } catch (err) {
      console.error("FDM Printers: cannot load registry", err);
      this._registry = [];
    }
    await this._loadHistory(false);
    if (this._view === "list") this._render();
  }

  /** Discovery flows of this domain waiting for the user (Klipper via zeroconf or the scan, Bambu via SSDP). */
  async _loadDiscovered() {
    try {
      const flows = await this._hass.callWS({ type: "config_entries/flow/progress" });
      this._discovered = flows.filter((f) => f.handler === DOMAIN && ["zeroconf", "integration_discovery"].includes(f.context?.source));
      this._reauth = flows.filter((f) => f.handler === DOMAIN && f.context?.source === "reauth");
    } catch (err) {
      this._discovered = [];
      this._reauth = [];
    }
  }

  /** Ask Home Assistant to look for Klipper printers now; found ones appear in "Found on the network". */
  async _scan() {
    if (this._scanning) return;
    this._scanning = true;
    this._renderIfIdle();
    try {
      await this._hass.callWS({ type: `${DOMAIN}/scan` });
    } catch (err) {
      console.error("FDM Printers: network search failed", err);
    }
    await this._loadDiscovered();
    this._scanning = false;
    this._renderIfIdle();
  }

  /** Re-render only the printer list or the "Add printer" menu, never a form the user may be typing in. */
  _renderIfIdle() {
    if (this._view === "list" || (this._view === "flow" && this._flow?.result?.type === "menu")) this._render();
  }

  /** "Found on the network". The list view always shows it (with "Search again"); the add menu only when there is something. */
  _renderDiscovered(always = false) {
    const found = this._discovered || [];
    if (!found.length && !always && !this._scanning) return "";
    const items = found
      .map((f) => {
        const name = f.context?.title_placeholders?.name || f.step_id;
        const kind = f.step_id === "bambu_lan" ? "Bambu Lab" : "Klipper";
        return `<button class="type" data-act="found" data-flow="${esc(f.flow_id)}"><b>${esc(name)}</b><small>${kind} · found on the network</small></button>`;
      })
      .join("");
    const search = this._scanning
      ? `<span class="sub">Searching the network…</span>`
      : `<button class="btn sm" data-act="scan">Search again</button>`;
    const empty = found.length ? "" : `<p class="empty">${this._scanning ? "" : "No new printers found."}</p>`;
    return `<h2>Found on the network ${search}</h2>${found.length ? `<div class="types">${items}</div>` : empty}`;
  }

  async _continueFound(flowId) {
    this._view = "flow";
    this._flow = { kind: "config", title: "Add printer", discovered: true };
    this._data = {};
    this._busy = true;
    this._render();
    try {
      this._setResult(await this._hass.callApi("GET", `config/config_entries/flow/${flowId}`));
    } catch (err) {
      this._flow.error = String(err?.body?.message || err?.message || err);
    }
    this._busy = false;
    this._render();
  }

  async _loadHistory(render = true) {
    try {
      const msg = { type: `${DOMAIN}/history`, limit: this._history.limit };
      if (this._history.filter) msg.entry_id = this._history.filter;
      const res = await this._hass.callWS(msg);
      this._history.rows = res.rows || [];
      this._history.total = res.total || 0;
      this._history.stats = res.stats || null;
    } catch (err) {
      console.error("FDM Printers: cannot load history", err);
    }
    if (render && this._view === "list") this._render();
  }

  _printers() {
    const printers = [];
    for (const entry of this._entries) {
      const ents = {};
      for (const e of this._registry || []) {
        if (e.config_entry_id !== entry.entry_id) continue;
        ents[(e.unique_id || "").slice(entry.entry_id.length + 1)] = e.entity_id;
      }
      if (!ents.printer_status && !ents.current_print_cost) continue; // panel-only entry
      printers.push({ entry, ents });
    }
    return printers;
  }
  _st(id) {
    return id ? this._hass.states[id] : undefined;
  }
  _avail(id) {
    const s = this._st(id);
    return !!s && s.state !== "unavailable";
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
        <div><div class="title">FDM Printers</div><div class="ver">FDM Printer Manager · v0.10.1</div></div>
        <span class="spacer"></span>
      </div>`;
    let body;
    if (!this._hass) body = `<main><p class="empty">Loading…</p></main>`;
    else if (this._view === "list") body = this._renderList();
    else body = this._renderFlow();
    const toast = this._toast ? `<div class="toast">${esc(this._toast)}</div>` : "";
    const zoom = this._view === "list" ? this._renderZoom() : "";
    this.shadowRoot.innerHTML = `<style>${STYLE}</style>${top}${body}${toast}${zoom}`;
    this._bind();
    this._attachCams();
  }

  _renderList() {
    if (this._registry === null) return `<main><p class="empty">Loading printers…</p></main>`;
    const printers = this._printers();
    const active = printers.filter((p) => ACTIVE.includes(this._st(p.ents.printer_status)?.state));
    return `
      <main>
        <div class="bar">
          <div><h1>Printers</h1><div class="sub">${printers.length} printer${printers.length === 1 ? "" : "s"} · ${active.length} printing</div></div>
          <button class="btn pri" data-act="add">＋ Add printer</button>
        </div>
        ${(this._reauth || [])
          .map(
            (f) =>
              `<div class="banner warn">🔑 <b>${esc(f.context?.title_placeholders?.name || "A Bambu Lab printer")}</b> refused its access code. <button class="btn sm" data-act="found" data-flow="${esc(f.flow_id)}">Type the new code</button></div>`,
          )
          .join("")}
        ${printers.length ? `<div class="grid">${printers.map((p) => this._card(p)).join("")}</div>` : `<p class="empty">No printers yet. Use “＋ Add printer” to add the first one.</p>`}
        ${this._renderDiscovered(true)}
        ${printers.length ? this._renderNow(active) + this._renderHistory(printers) : ""}
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
    const paused = ["paused", "pause"].includes(raw);
    const progress = num(s("print_progress"));
    const file = s("print_file")?.state;
    const left = num(s("print_time_left"));
    const nozzle = num(s("nozzle_temperature"));
    const unit = s("cost_total")?.attributes?.unit_of_measurement || s("current_print_cost")?.attributes?.unit_of_measurement || "";
    const type = attrs.printer_type === "klipper" ? "Klipper" : attrs.printer_type === "bambu_lan" ? "Bambu Lab · LAN" : "Bambu Lab";
    const where = attrs.host ? ` · ${esc(attrs.host)}` : "";
    const fileShown = file && !["unknown", "unavailable"].includes(file) ? file : "";
    const lastResult = s("last_print_result")?.state;
    const lastLine =
      lastResult && !["unknown", "unavailable"].includes(lastResult)
        ? `Last: ${RESULT_ICON[lastResult] || ""} ${lastResult} · ${money(num(s("last_print_cost")), unit)}`
        : "";
    const eid = p.entry.entry_id;
    // The printer's verdict can be wrong (a "finished" print that came off the plate): mark the last print by hand.
    const hasLast = s("last_print_result") && !["unavailable"].includes(lastResult);
    const markHtml =
      !active && hasLast
        ? `<div class="mark">${lastResult !== "success" ? `<button class="btn sm" data-act="mark" data-entry="${esc(eid)}" data-result="success" title="Mark the last print as a success">✓ OK</button>` : ""}${
            lastResult !== "failed" ? `<button class="btn sm danger" data-act="mark" data-entry="${esc(eid)}" data-result="failed" title="Mark the last print as failed">✗ Failed</button>` : ""
          }</div>`
        : "";

    const foot = active
      ? `<div><div class="k">Energy</div><div class="v">${kwh(num(s("current_print_energy")))}</div></div>
         <div><div class="k">Cost</div><div class="v">${money(num(s("current_print_cost")), unit)}</div></div>
         <div><div class="k">Time</div><div class="v">${num(s("current_print_duration")) != null ? fmtMin(num(s("current_print_duration")) * 60) : "—"}</div></div>`
      : `<div><div class="k">Today</div><div class="v">${kwh(num(s("energy_today")))}</div></div>
         <div><div class="k">Cost today</div><div class="v">${money(num(s("cost_today")), unit)}</div></div>
         <div><div class="k">Cost total</div><div class="v">${money(num(s("cost_total")), unit)}</div></div>`;

    const ctl = [];
    if (this._avail(p.ents.pause_print)) ctl.push(`<button class="btn sm" data-act="ctl" data-what="pause" data-entity="${esc(p.ents.pause_print)}">⏸ Pause</button>`);
    if (this._avail(p.ents.resume_print)) ctl.push(`<button class="btn sm" data-act="ctl" data-what="resume" data-entity="${esc(p.ents.resume_print)}">▶ Resume</button>`);
    if (this._avail(p.ents.cancel_print)) ctl.push(`<button class="btn sm danger" data-act="askcancel" data-entry="${esc(eid)}">✕ Cancel</button>`);

    const power = s("safe_power");
    let powerHtml = `<span class="sub">No plug</span>`;
    if (power) {
      const phase = power.attributes?.phase;
      const on = power.state === "on";
      const hhmm = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "");
      // Shutting down: the computer is going off; the plug is cut once it stops answering.
      const waiting = phase === "shutting_down";
      // Cooling: the nozzle has to drop below cool_to before the plug is cut; a tap keeps the printer on.
      const cooling = phase === "cooling";
      // Auto-off: the print ended, the printer turns itself off at auto_off_at; a tap keeps it on.
      const autoOff = phase === "waiting";
      const keepable = cooling || autoOff;
      // Asked: the "Turn off?" box is open under the card. Sent: "Turn off" tapped, the new phase not here yet
      // (with nothing running the phase attribute reads "on" / "off").
      const asking = this._ask?.entry === eid && this._ask.kind === "power";
      const sentAt = this._offing?.[eid];
      const turningOff = waiting || cooling || autoOff;
      const sent = !!sentAt && on && !turningOff && Date.now() - sentAt < OFFING_MS;
      if (sentAt && !sent) delete this._offing[eid];
      const coolTo = power.attributes?.cool_to ?? 80;
      const label = cooling
        ? `Cooling nozzle — ${nozzle != null ? `${Math.round(nozzle)}°` : "…"} → ${coolTo}°`
        : autoOff ? `Auto off at ${hhmm(power.attributes?.auto_off_at)}`
        : waiting ? `Shutting down… waiting for it to go off (until ${hhmm(power.attributes?.cut_at)})`
        : sent ? "Turning off…"
        : asking ? "Turning off? Confirm below"
        : on ? "Power on" : power.state === "off" ? "Power off" : "Plug unavailable";
      const disabled = waiting || sent || power.state === "unavailable" || (on && active && !keepable);
      const title = cooling ? "Waiting for the nozzle to cool. Tap to keep the printer on"
        : autoOff ? "Turns off by itself after the print. Tap to keep the printer on"
        : asking ? "Tap to keep the printer on"
        : on && active ? "Not while printing" : "";
      // Why the last power off left the plug on (also sent as a notification).
      const problem = on && !phase && !asking && !sent ? {
        hot: "Not turned off: the nozzle stayed hot",
        still_on: "Not turned off: the computer kept running",
        no_shutdown: "Not turned off: could not shut the computer down (set the SSH login)",
      }[power.attributes?.last_result] : null;
      powerHtml = `<button class="sw" data-act="power" data-entry="${esc(eid)}" data-on="${on && !keepable ? 1 : 0}" ${disabled ? "disabled" : ""} title="${title}"><span class="tg ${waiting || keepable || asking || sent ? "wait" : on ? "on" : ""}"></span>${esc(label)}</button>${problem ? `<span class="sub warn">${esc(problem)}</span>` : ""}`;
    }

    // Camera: only when the printer has one (Klipper webcam found by the integration, or ha-bambulab's own).
    const camEntity = attrs.camera_entity;
    const camLive = active && !String(camEntity || "").startsWith("image.");
    const camHtml =
      camEntity && this._avail(camEntity)
        ? `<div class="cam" data-act="camzoom" data-entity="${esc(camEntity)}" data-cam="${esc(camEntity)}" data-live="${camLive ? 1 : 0}" title="Open full size">${camLive ? `<span class="live">● LIVE</span>` : ""}</div>`
        : "";

    // Lights the printer has (Klipper LEDs / light pins, the Bambu chamber light): one toggle each.
    const lightHtml = (attrs.lights || [])
      .filter((l) => this._st(l.entity))
      .map((l) => {
        const st = this._st(l.entity).state;
        return `<button class="btn sm light ${st === "on" ? "on" : ""}" data-act="light" data-entity="${esc(l.entity)}" ${st === "unavailable" ? "disabled" : ""} title="${st === "on" ? "Turn off" : "Turn on"}">💡 ${esc(l.name)}</button>`;
      })
      .join("");

    // Filaments of a multi-material unit (AFC / CANVAS, Happy Hare, AMS): real colour, type, % left; the active one outlined.
    const slots = attrs.filaments || [];
    const filHtml = slots.length
      ? `<div class="fil">${slots
          .map(
            (f) =>
              `<span class="chip ${f.active ? "act" : ""} ${f.empty ? "empty" : ""}" title="${esc(f.name)}${f.tool ? ` · ${esc(f.tool)}` : ""}${f.active ? " · in use" : ""}"><i style="background:${esc(f.color || "transparent")}"></i>${esc(f.material || (f.empty ? "empty" : "?"))}${f.remaining != null ? ` <small>${Math.round(f.remaining)}%</small>` : ""}</span>`,
          )
          .join("")}</div>`
      : "";

    // Baby steps (Klipper): move the nozzle while printing; "Save" only when turned on in the options.
    const bs = attrs.babystep;
    let zHtml = "";
    if (bs && (active || (bs.save && bs.z_offset))) {
      const step = this._zstep?.[eid] ?? 0.01;
      const z = bs.z_offset;
      const zText = z != null ? `${z > 0 ? "+" : ""}${z.toFixed(3)} mm` : "—";
      const moves = active
        ? `<button class="btn sm" data-act="zstep" data-entry="${esc(eid)}" data-dir="down" title="Nozzle closer to the bed">▼ ${step}</button>
           <button class="btn sm" data-act="zstep" data-entry="${esc(eid)}" data-dir="up" title="Nozzle further from the bed">▲ ${step}</button>
           <span>${(bs.steps || []).map((v) => `<button class="stp ${v === step ? "on" : ""}" data-act="zsize" data-entry="${esc(eid)}" data-step="${v}">${v}</button>`).join("")}</span>
           ${z ? `<button class="btn sm" data-act="zreset" data-entry="${esc(eid)}" title="Back to 0">Reset</button>` : ""}`
        : "";
      const save = bs.save && z ? `<button class="btn sm" data-act="zasksave" data-entry="${esc(eid)}" title="Keep this offset">Save</button>` : "";
      zHtml = `<div class="z"><span class="k">Z offset</span><b>${zText}</b>${moves}${save}</div>`;
    }

    let ask = "";
    if (this._ask?.entry === eid && this._ask.kind === "zsave")
      ask = `<div class="ask">Keep the Z offset ${bs?.z_offset != null ? `(${bs.z_offset.toFixed(3)} mm)` : ""}? It is applied now; run SAVE_CONFIG when the printer is idle to keep it after a restart (Klipper restarts).<div class="row"><button class="btn sm" data-act="askno">Not now</button><button class="btn sm" data-act="zsave" data-entry="${esc(eid)}">Save</button></div></div>`;
    if (this._ask?.entry === eid && this._ask.kind === "power") {
      const coolTo = power?.attributes?.cool_to ?? 80;
      const hot = nozzle != null && nozzle >= coolTo;
      const waitMin = power?.attributes?.shutdown_wait_minutes;
      const steps = [hot ? `Waits for the nozzle to cool below ${coolTo}° (now ${Math.round(nozzle)}°).` : ""];
      if (attrs.printer_type === "klipper")
        steps.push("Shuts down its computer safely.", "Cuts the plug only after the computer is off.");
      else steps.push("Cuts the plug (this printer has no computer to shut down).");
      const note = attrs.printer_type === "klipper"
        ? `<p><b>It never cuts the power while the printer is still on.</b> If it does not turn off within ${waitMin ? `${waitMin} min` : "a few minutes"}, the plug stays on and you get a notification.</p>`
        : "";
      ask = `<div class="ask"><b>Turn ${esc(p.entry.title)} off?</b><ol>${steps.filter(Boolean).map((t) => `<li>${esc(t)}</li>`).join("")}</ol>${note}<div class="row"><button class="btn sm" data-act="askno">Keep on</button><button class="btn sm danger" data-act="poweroff" data-entry="${esc(eid)}" data-entity="${esc(p.ents.safe_power)}">Turn off</button></div></div>`;
    }
    if (this._ask?.entry === eid && this._ask.kind === "cancel")
      ask = `<div class="ask">Cancel the print${fileShown ? ` of <b>${esc(fileShown)}</b>` : ""}? This cannot be undone.<div class="row"><button class="btn sm" data-act="askno">Keep printing</button><button class="btn sm danger" data-act="ctl" data-what="cancel" data-entity="${esc(p.ents.cancel_print)}">Cancel print</button></div></div>`;

    return `
      <article class="pc ${cls}">
        <div class="stripe"></div>
        <div class="hd"><div><div class="nm">${esc(p.entry.title)}</div><div class="ty">${type}${where}</div></div><span class="state ${cls}">${esc(shown)}</span></div>
        ${attrs.alert === "down" ? `<div class="alert">⚠️ Not answering during the print</div>` : attrs.alert === "stalled" ? `<div class="alert">⏳ No progress for a long time</div>` : ""}
        ${camHtml}
        <div class="job">${esc(active ? fileShown || "—" : lastLine || "—")}</div>
        ${markHtml}
        <div class="prog"><i style="width:${active && progress != null ? Math.min(100, progress) : 0}%"></i></div>
        <div class="meta"><span>${active && progress != null ? `${Math.round(progress)}%${paused ? " · paused" : ""}` : ""}</span><span>${active && left != null ? `${fmtMin(left)} left` : ""}</span></div>
        <div class="temps">${nozzle != null ? `▲ ${nozzle.toFixed(1)}°` : ""}</div>
        ${filHtml}
        ${ctl.length || lightHtml ? `<div class="ctrl">${ctl.join("")}${lightHtml}</div>` : ""}
        ${zHtml}
        <div class="ft">${foot}</div>
        ${ask}
        <div class="act">
          ${powerHtml}
          <span class="links">
            ${attrs.web_url ? `<a class="link" href="${esc(attrs.web_url)}" target="_blank" rel="noopener">Open web UI ↗</a>` : ""}
            <button class="linkbtn" data-act="edit" data-entry="${esc(eid)}">Edit</button>
          </span>
        </div>
      </article>`;
  }

  _renderNow(active) {
    const rows = active
      .map((p) => {
        const s = (k) => this._st(p.ents[k]);
        const unit = s("current_print_cost")?.attributes?.unit_of_measurement || "";
        const progress = num(s("print_progress"));
        return `<tr><td>${esc(p.entry.title)}</td><td class="m">${esc(s("print_file")?.state || "—")}</td>
          <td><span class="mini"><i style="width:${progress ?? 0}%"></i></span>${progress != null ? `${Math.round(progress)}%` : "—"}</td>
          <td class="n">${num(s("print_time_left")) != null ? fmtMin(num(s("print_time_left"))) : "—"}</td>
          <td class="n">${kwh(num(s("current_print_energy")))}</td><td class="n">${money(num(s("current_print_cost")), unit)}</td></tr>`;
      })
      .join("");
    return `<h2>Printing now</h2>${
      active.length
        ? `<div class="tablewrap"><table><thead><tr><th>Printer</th><th>File</th><th>Progress</th><th>Left</th><th>Energy</th><th>Cost</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : `<p class="empty" style="padding:4px 0">Nothing printing.</p>`
    }`;
  }

  _renderHistory(printers) {
    const h = this._history;
    const opts = [`<option value="">All printers</option>`]
      .concat(printers.map((p) => `<option value="${esc(p.entry.entry_id)}" ${h.filter === p.entry.entry_id ? "selected" : ""}>${esc(p.entry.title)}</option>`))
      .join("");
    const markBtn = (r, result, label, title) =>
      r.result !== result && r.job_id
        ? `<button class="linkbtn" data-act="mark" data-entry="${esc(r.entry_id)}" data-job="${esc(r.job_id)}" data-result="${result}" title="${title}">${label}</button>`
        : "";
    const rows = h.rows
      .map(
        (r) => `<tr><td>${RESULT_ICON[r.result] || "❔"} ${esc(r.result || "")}${r.result_by === "user" ? ` <span class="sub" title="Marked by hand">✎</span>` : ""}
          <span class="rmark">${markBtn(r, "success", "✓", "Mark as a success")}${markBtn(r, "failed", "✗", "Mark as failed")}</span></td><td>${esc(r.printer)}</td><td class="m">${esc(r.name || "—")}</td>
          <td>${fmtWhen(r.finished_at)}</td><td class="n">${fmtMin((r.duration_s || 0) / 60)}</td>
          <td class="n">${kwh(r.energy_kwh)}</td><td class="n">${money(r.electricity_cost, r.currency)}</td></tr>`,
      )
      .join("");
    const st = h.stats;
    const cur = h.rows[0]?.currency || "";
    const statsHtml =
      st && st.prints
        ? `<div class="stats sub">${st.prints} prints · ${st.success_rate != null ? `${st.success_rate}% successful` : "no verdicts yet"} · ${st.counts.failed} failed${
            st.counts.cancelled ? ` · ${st.counts.cancelled} cancelled` : ""
          } · failed and cancelled prints cost ${money(st.wasted_cost, cur)}</div>`
        : "";
    const more = h.total > h.rows.length ? `<div class="more"><button class="btn sm" data-act="more">Show more (${h.total - h.rows.length})</button></div>` : "";
    return `<h2>Recent prints <span class="filters"><select id="hfilter" aria-label="Filter by printer">${opts}</select></span></h2>${statsHtml}${
      h.rows.length
        ? `<div class="tablewrap"><table><thead><tr><th>Result</th><th>Printer</th><th>File</th><th>Ended</th><th>Time</th><th>Energy</th><th>Cost</th></tr></thead><tbody>${rows}</tbody></table>${more}</div>`
        : `<p class="empty" style="padding:4px 0">No finished prints yet. They appear here as soon as a print ends.</p>`
    }`;
  }

  // ------------------------------------------------------------------ flows
  _flowBase(kind) {
    return (kind || this._flow?.kind) === "options" ? "config/config_entries/options/flow" : "config/config_entries/flow";
  }

  async _startAdd() {
    this._scan(); // in the background: printers found while choosing the type show up in the menu
    this._view = "flow";
    this._flow = { kind: "config", title: "Add printer" };
    await this._openFlow("config/config_entries/flow", { handler: DOMAIN, show_advanced_options: false });
  }

  async _startEdit(entryId) {
    const entry = this._entries.find((e) => e.entry_id === entryId);
    this._view = "flow";
    this._flow = { kind: "options", title: `Edit · ${entry ? entry.title : ""}`, entryId };
    this._confirm = null;
    await this._openFlow("config/config_entries/options/flow", { handler: entryId });
  }

  async _openFlow(path, body) {
    this._data = {};
    this._busy = true;
    this._render();
    try {
      this._setResult(await this._hass.callApi("POST", path, body));
    } catch (err) {
      this._flow.error = String(err?.body?.message || err?.message || err);
    }
    this._busy = false;
    this._render();
  }

  _setResult(result) {
    if (result.step_id !== this._flow.result?.step_id) Object.assign(this._photo, { status: "", kind: "", filled: {}, warnings: [] });
    if (PHOTO_STEPS.includes(result.step_id) && this._photo.ai === undefined) this._loadPhotoAi();
    this._flow.id = result.flow_id || this._flow.id;
    this._flow.result = result;
    this._data = {};
    this._showPrice = false;
    if (result.type === "form") {
      for (const field of result.data_schema || []) {
        const suggested = field.description?.suggested_value;
        if (suggested !== undefined && suggested !== null) this._data[field.name] = suggested;
        else if (field.default !== undefined) this._data[field.name] = field.default;
      }
      if (result.step_id === "electricity") this._prefillPlug(this._flow.kind === "config");
    }
  }

  async _submit(payload) {
    if (!this._flow?.id) return;
    this._busy = true;
    this._render();
    try {
      const result = await this._hass.callApi("POST", `${this._flowBase()}/${this._flow.id}`, payload);
      const keep = result.type === "form" && result.step_id === this._flow.result?.step_id ? { ...this._data, ...payload } : null;
      this._setResult(result);
      if (keep) Object.assign(this._data, keep); // same step with errors: keep what was typed
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

  _dropFlow() {
    const flow = this._flow;
    if (flow?.id && this._hass && !flow.discovered) this._hass.callApi("DELETE", `${this._flowBase(flow.kind)}/${flow.id}`).catch(() => {});
    if (flow) flow.id = null;
  }

  _backToList() {
    this._dropFlow();
    this._flow = null;
    this._confirm = null;
    this._view = "list";
    this._loadMeta();
    this._render();
  }

  _stepLabel() {
    if (this._flow?.kind === "options") return null;
    const step = this._flow?.result?.step_id;
    if (this._flow?.result?.type === "create_entry") return 3;
    return step === "electricity" ? 2 : 1;
  }

  _renderFlow() {
    const f = this._flow || {};
    const r = f.result;
    let inner;
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
        ? `<div style="margin-top:24px">${
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
    const found = this._renderDiscovered();
    return `${found}${found ? "<h2>Or add one by hand</h2>" : ""}<div class="types">${cards}<button class="type" disabled><b>OctoPrint</b><small>Older machines</small><span class="soon">planned</span></button></div>`;
  }

  _renderForm(r) {
    const errors = r.errors || {};
    const base = errors.base ? `<div class="banner err">${esc(ERRORS[errors.base] || errors.base)}</div>` : "";
    const hint =
      {
        klipper: `<div class="banner info">Type the printer's IP address. Name, port and web interface are filled in automatically.</div>`,
        zeroconf_confirm: `<div class="banner info">Klipper printer <b>${esc(r.description_placeholders?.name || "")}</b> found at ${esc(r.description_placeholders?.host || "")}. Add it?</div>`,
        reauth_confirm: `<div class="banner warn">The printer refused its access code: it changes when LAN Only mode is turned off and on again. Type the new one, shown on the printer's LAN Only page (or take a photo of it).</div>${this._renderPhoto()}`,
        bambu_lan: `<div class="banner info">On the printer, turn on <b>LAN Only mode</b> (A1: Settings → page 3 → LAN Only Mode; P1: Settings → WLAN; X1/H2: Settings → LAN Only). The IP address and the 8-character access code are shown on that same page. The printer must be switched on. <b>Developer Mode</b> is optional: status, temperatures and safe power work without it; it is only needed to send commands (pause, resume, stop, chamber light). <a class="link" href="${LAN_GUIDE_URL}" target="_blank" rel="noreferrer">Step-by-step guide</a></div>${this._renderPhoto()}`,
        klipper_auth: `<div class="banner info">This printer only accepts connections with a Moonraker API key.</div>`,
        electricity: `<div class="banner info">Pick the plug the printer is connected to. Its power and energy sensors are found automatically.</div>`,
      }[r.step_id] || "";
    const names = (r.data_schema || []).map((f) => f.name);
    const byName = Object.fromEntries((r.data_schema || []).map((f) => [f.name, f]));
    const main = [];
    const advanced = [];
    if (r.step_id === "electricity") {
      for (const n of ["switch_entity", "power_entity", "energy_entity"]) if (byName[n]) main.push(this._field(byName[n], errors[n]));
      if (byName.price_per_kwh) main.push(this._priceField(byName.price_per_kwh, errors.price_per_kwh));
      if (byName.price_entity) advanced.push(this._field(byName.price_entity, errors.price_entity));
      for (const n of names) if (!["switch_entity", "power_entity", "energy_entity", "price_per_kwh", "price_entity"].includes(n)) advanced.push(this._field(byName[n], errors[n]));
    } else {
      for (const n of names) main.push(this._field(byName[n], errors[n]));
    }
    const adv = advanced.length ? `<details ${errors.price_entity ? "open" : ""}><summary>Advanced</summary>${advanced.join("")}</details>` : "";
    let submitLabel = r.step_id === "zeroconf_confirm" ? "Add →" : "Next →";
    if (this._flow.kind === "options" && r.step_id === "behaviour") submitLabel = "Save";
    if (this._flow.kind === "config" && r.step_id === "electricity") submitLabel = "Add printer";
    return `${hint}${base}<form id="form" autocomplete="off">${main.join("")}${adv}<div class="wbtns"><span></span><button class="btn pri" type="submit">${submitLabel}</button></div></form>`;
  }

  _priceField(field, error) {
    const meter = this._data.power_entity || this._data.energy_entity;
    const unit = `${this._hass.config?.currency || "€"}/kWh`;
    const known = this._knownPrice();
    const value = this._data.price_per_kwh;
    if (known != null && value == known && !this._showPrice && !error && this._flow.kind === "config") {
      return `<div class="summary"><span>Price: <b>${esc(value)} ${esc(unit)}</b> <span class="sub">(same as your other printers)</span></span><button type="button" class="linkbtn" data-act="showprice">Change</button><input type="hidden" data-name="price_per_kwh" data-num="1" value="${esc(value)}"></div>`;
    }
    const label = meter ? `<b>Price per kWh</b>` : `Price per kWh <span class="h">(only needed with an energy meter)</span>`;
    const err = error ? `<span class="e">${esc(ERRORS[error] || error)}</span>` : "";
    return `<div class="f"><label for="f-price_per_kwh">${label}</label><div class="unitwrap"><input id="f-price_per_kwh" data-name="price_per_kwh" data-num="1" type="number" step="any" min="0" inputmode="decimal" placeholder="0.16" value="${esc(value ?? "")}"><span class="sub">${esc(unit)}</span></div><span class="h">What you pay per kWh. Used for the cost of every print.</span>${err}</div>`;
  }

  _field(field, error) {
    const name = field.name;
    const label = FIELD_LABELS[name] || name;
    const sel = field.selector || {};
    const value = this._data[name];
    const msg = name === "access_code" && error === "invalid_auth" ? "The printer refused this access code." : ERRORS[error] || error;
    const err = error ? `<span class="e">${esc(msg)}</span>` : "";
    const auto = ["power_entity", "energy_entity"].includes(name) && this._flow?.result?.step_id === "electricity";
    const hint = auto ? `<span class="h">${value ? "Found from the plug." : "Not found on this plug — search one, or leave empty."}</span>` : "";
    const req = field.required || name === "switch_entity" || auto ? "" : ` <span class="h">(optional)</span>`;
    if (sel.entity || sel.device) {
      const shown = value ? this._optionLabel(sel, value) : "";
      return `<div class="f"><label for="f-${esc(name)}">${esc(label)}${req}</label>
        <input id="f-${esc(name)}" class="cb" data-cb="${esc(name)}" type="text" value="${esc(shown)}" placeholder="Type to search…" role="combobox" aria-expanded="false" spellcheck="false">
        <input type="hidden" data-name="${esc(name)}" value="${esc(value ?? "")}">
        <div class="cb-list" id="cbl-${esc(name)}" role="listbox" hidden></div>${hint}${err}</div>`;
    }
    if (sel.select) {
      const options = (sel.select.options || []).map((o) => (typeof o === "string" ? { value: o, label: o } : o));
      const opts = options.map((o) => `<option value="${esc(o.value)}" ${o.value === value ? "selected" : ""}>${esc(o.label)}</option>`).join("");
      return `<div class="f"><label for="f-${esc(name)}">${esc(label)}</label><select id="f-${esc(name)}" data-name="${esc(name)}" class="native">${opts}</select>${err}</div>`;
    }
    if (sel.number) {
      return `<div class="f"><label for="f-${esc(name)}">${esc(label)}${req}</label><input id="f-${esc(name)}" data-name="${esc(name)}" data-num="1" type="number" step="any" value="${esc(value ?? "")}">${err}</div>`;
    }
    if (sel.boolean !== undefined) {
      return `<div class="f"><label class="chk"><input id="f-${esc(name)}" data-name="${esc(name)}" data-bool="1" type="checkbox" ${value ? "checked" : ""}> ${esc(label)}</label>${err}</div>`;
    }
    // A value read from a photo is shown (also the access code) and highlighted, so it gets checked.
    const filled = !!this._photo.filled[name];
    const type = sel.text?.type === "password" && !filled ? "password" : "text";
    const ph = name === "host" ? "192.168.1.89" : name === "access_code" ? "8 characters" : "";
    const cls = `${name === "host" ? " big" : ""}${filled ? " filled" : ""}`;
    const check = !filled
      ? ""
      : this._photo.warnings.includes(name)
        ? `<span class="w">Read from the photo, but it doesn't look right (expected ${esc(PHOTO_FIELD_HINT[name] || "")}). Check it.</span>`
        : `<span class="h">Read from the photo — check it.</span>`;
    return `<div class="f${cls}"><label for="f-${esc(name)}">${esc(label)}${req}</label><input id="f-${esc(name)}" data-name="${esc(name)}" type="${type}" value="${esc(value ?? "")}" placeholder="${ph}" spellcheck="false" autocapitalize="off">${check}${err}</div>`;
  }

  // ------------------------------------------------------------------ options for the searchable lists
  _schemaField(name) {
    return (this._flow?.result?.data_schema || []).find((f) => f.name === name);
  }

  _optionLabel(sel, value) {
    if (sel.device) {
      const d = this._hass.devices?.[value];
      return d ? d.name_by_user || d.name || value : value;
    }
    const fn = this._hass.states[value]?.attributes?.friendly_name;
    return fn && fn !== value ? `${fn} · ${value}` : value;
  }

  /** [{group, items:[{value,label,detail}]}] for one field. */
  _options(name) {
    const field = this._schemaField(name);
    const sel = field?.selector || {};
    if (sel.device) {
      const filters = Array.isArray(sel.device.filter) && sel.device.filter.length ? sel.device.filter : [sel.device];
      const integrations = filters.map((f) => f.integration).filter(Boolean);
      const items = Object.values(this._hass.devices || {})
        .filter((d) => !integrations.length || (d.identifiers || []).some(([domain]) => integrations.includes(domain)))
        .map((d) => ({ value: d.id, label: d.name_by_user || d.name || d.id, detail: d.model || "" }))
        .sort((a, b) => a.label.localeCompare(b.label));
      return [{ group: "", items }];
    }
    const cfg = sel.entity || {};
    const filters = Array.isArray(cfg.filter) && cfg.filter.length ? cfg.filter : [cfg];
    const list = (v) => (v == null ? null : Array.isArray(v) ? v : [v]);
    const ok = (id) =>
      filters.some((f) => {
        const domains = list(f.domain);
        const classes = list(f.device_class);
        if (domains && !domains.includes(id.split(".")[0])) return false;
        if (classes && !classes.includes(this._hass.states[id]?.attributes?.device_class)) return false;
        return true;
      });
    let ids = Object.keys(this._hass.states).filter(ok);
    if (name === "price_entity") ids = ids.filter((id) => /\/\s*(k|m)?wh$/i.test(this._hass.states[id]?.attributes?.unit_of_measurement || ""));
    if (name === "switch_entity") ids = ids.filter((id) => !id.startsWith(`switch.`) || !this._isOwnEntity(id));
    const item = (id) => {
      const st = this._hass.states[id];
      return { value: id, label: st?.attributes?.friendly_name || id, detail: id };
    };
    const sorted = (arr) => arr.map(item).sort((a, b) => a.label.localeCompare(b.label));
    if (["power_entity", "energy_entity"].includes(name) && this._data.switch_entity) {
      const dev = this._hass.entities?.[this._data.switch_entity]?.device_id;
      if (dev) {
        const mine = ids.filter((id) => this._hass.entities?.[id]?.device_id === dev);
        const rest = ids.filter((id) => !mine.includes(id));
        return [
          { group: "From this plug", items: sorted(mine) },
          { group: "Other", items: sorted(rest) },
        ];
      }
    }
    return [{ group: "", items: sorted(ids) }];
  }

  _isOwnEntity(id) {
    return (this._registry || []).some((e) => e.entity_id === id);
  }

  _knownPrice() {
    const known = Object.values(this._hass.states).find((s) => s.attributes?.printer_type && s.attributes?.price_per_kwh != null);
    return known ? known.attributes.price_per_kwh : null;
  }

  /** Fill power / energy from the chosen plug's device, and the price from the other printers. */
  _prefillPlug(withPrice) {
    if (withPrice && this._data.price_per_kwh == null) {
      const p = this._knownPrice();
      if (p != null) this._data.price_per_kwh = p;
    }
    const switchId = this._data.switch_entity;
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
    for (const field of this._flow?.result?.data_schema || []) {
      const el = this.shadowRoot.querySelector(`[data-name="${CSS.escape(field.name)}"]`);
      if (!el) continue;
      if (el.dataset.bool) {
        out[field.name] = el.checked;
        continue;
      }
      let v = el.value;
      if (el.dataset.num) v = v === "" ? undefined : Number(String(v).replace(",", "."));
      else v = typeof v === "string" ? v.trim() : v;
      if (v === "" || v === undefined || Number.isNaN(v)) continue;
      out[field.name] = v;
    }
    return out;
  }

  // ------------------------------------------------------------------ searchable list (combobox)
  _setupCombo(input) {
    const name = input.dataset.cb;
    const list = this.shadowRoot.getElementById(`cbl-${name}`);
    const hidden = this.shadowRoot.querySelector(`[data-name="${CSS.escape(name)}"]`);
    let hl = -1;
    let flat = [];

    const draw = (query) => {
      const words = norm(query).split(/\s+/).filter(Boolean);
      const match = (it) => {
        const hay = norm(`${it.label} ${it.detail} ${it.value}`);
        return words.every((w) => hay.includes(w));
      };
      const groups = this._options(name)
        .map((g) => ({ group: g.group, items: g.items.filter(match).slice(0, 200) }))
        .filter((g) => g.items.length);
      flat = [];
      let html = name === "switch_entity" || this._schemaField(name)?.required ? "" : `<div class="cb-opt" data-i="-1"><b>— none —</b></div>`;
      for (const g of groups) {
        if (g.group) html += `<div class="cb-grp">${esc(g.group)}</div>`;
        for (const it of g.items) {
          const i = flat.length;
          flat.push(it);
          html += `<div class="cb-opt ${it.value === hidden.value ? "sel" : ""}" data-i="${i}" role="option"><b>${esc(it.label)}</b>${it.detail && it.detail !== it.label ? `<small>${esc(it.detail)}</small>` : ""}</div>`;
        }
      }
      if (!flat.length) html += `<div class="cb-none">Nothing matches “${esc(query)}”.</div>`;
      list.innerHTML = html;
      hl = flat.length ? 0 : -1;
      paint();
    };
    const paint = () => {
      list.querySelectorAll(".cb-opt").forEach((el) => el.classList.toggle("hl", Number(el.dataset.i) === hl));
      list.querySelector(".cb-opt.hl")?.scrollIntoView({ block: "nearest" });
    };
    const open = () => {
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      draw(input.dataset.typed ? input.value : "");
    };
    const close = () => {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.dataset.typed = "";
      input.value = hidden.value ? this._optionLabel(this._schemaField(name)?.selector || {}, hidden.value) : "";
    };
    const choose = (i) => {
      const it = i >= 0 ? flat[i] : null;
      hidden.value = it ? it.value : "";
      this._data[name] = hidden.value || undefined;
      close();
      if (name === "switch_entity") {
        Object.assign(this._data, this._collect());
        this._data.switch_entity = hidden.value || undefined;
        delete this._data.power_entity;
        delete this._data.energy_entity;
        this._prefillPlug(false);
        this._render();
      } else if (["power_entity", "energy_entity"].includes(name)) {
        Object.assign(this._data, this._collect());
        this._render();
      }
    };

    input.addEventListener("focus", () => {
      input.select();
      open();
    });
    input.addEventListener("input", () => {
      input.dataset.typed = "1";
      list.hidden = false;
      draw(input.value);
    });
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "ArrowDown") {
        if (list.hidden) open();
        hl = Math.min(flat.length - 1, hl + 1);
        paint();
        ev.preventDefault();
      } else if (ev.key === "ArrowUp") {
        hl = Math.max(0, hl - 1);
        paint();
        ev.preventDefault();
      } else if (ev.key === "Enter") {
        if (!list.hidden) {
          ev.preventDefault();
          if (hl >= 0) choose(hl);
        }
      } else if (ev.key === "Escape") {
        close();
        input.blur();
      }
    });
    input.addEventListener("blur", () => setTimeout(() => !list.matches(":hover") && close(), 150));
    list.addEventListener("mousedown", (ev) => ev.preventDefault());
    list.addEventListener("click", (ev) => {
      const opt = ev.target.closest(".cb-opt");
      if (opt) choose(Number(opt.dataset.i));
    });
  }

  // ------------------------------------------------------------------ read from photo (Bambu LAN)
  _renderPhoto() {
    const p = this._photo;
    const where = p.ai
      ? `The photo is sent to your AI provider (<b>${esc(p.ai.name)}</b>) to read it.`
      : p.ai === null
        ? "Read on this device: the photo never leaves it (the first time downloads the reader, about 10 MB)."
        : "";
    const status = p.status ? `<div class="st ${esc(p.kind)}">${esc(p.status)}</div>` : "";
    // A gallery photo only: the in-page camera needed https and the Android app ignores capture=.
    return `<div class="photo ${p.reading ? "busy" : ""}">
      <div class="row">
        <button type="button" class="btn pri" data-act="photochoose">🖼️ Choose photo</button>
        <input type="file" id="ph-file" class="photo-in" accept="image/*" hidden>
      </div>
      <span class="h">Take a picture of the LAN Only screen with your camera app (no flash, lower the exposure if you can), then choose it here. IP address, serial number and access code are filled in for you. ${where}</span>
      ${status}</div>`;
  }

  async _loadPhotoAi() {
    this._photo.ai = null;
    try {
      this._photo.ai = await this._hass.callWS({ type: `${DOMAIN}/photo_ai` });
    } catch (_err) {
      this._photo.ai = null;
    }
    if (this._view === "flow" && PHOTO_STEPS.includes(this._flow?.result?.step_id) && !this._busy) {
      Object.assign(this._data, this._collect());
      this._render();
    }
  }

  _setPhotoStatus(status, kind = "") {
    Object.assign(this._data, this._collect());
    Object.assign(this._photo, { status, kind });
    this._render();
  }

  /** Open the file picker (gallery on phones). */
  _pickPhoto() {
    const input = this.shadowRoot.getElementById("ph-file");
    if (!input) return;
    input.value = "";
    input.click();
  }

  async _readPhoto(file) {
    if (!file) return;
    this._photo.reading = true;
    this._setPhotoStatus("Reading…");
    let res = null;
    try {
      const img = await this._shrinkPhoto(file);
      if (this._photo.ai) {
        this._setPhotoStatus(`Reading… (asking ${this._photo.ai.name})`);
        const ai = await this._hass.callWS({ type: `${DOMAIN}/read_photo`, image: img.b64, mime: "image/jpeg" }).catch(() => null);
        if (ai?.status === "ok" && Object.keys(ai.fields || {}).length) res = ai;
      }
      if (!res) {
        this._setPhotoStatus("Reading on this device… (the first time downloads the reader, about 10 MB)");
        const text = await this._ocr(img.blob);
        const ocr = await this._hass.callWS({ type: `${DOMAIN}/parse_photo_text`, text: text.slice(0, 20000) });
        if (ocr?.status === "ok") res = ocr;
      }
    } catch (err) {
      console.warn("FDM Printer Manager: reading the photo failed", err);
    }
    this._photo.reading = false;
    if (!res) return this._setPhotoStatus("Could not read the photo — type the values.", "err");
    this._applyPhoto(res);
  }

  /** Fill the fields of this form with what was read; they stay editable. */
  _applyPhoto(res) {
    Object.assign(this._data, this._collect());
    const names = (this._flow?.result?.data_schema || []).map((f) => f.name);
    const filled = {};
    for (const [name, value] of Object.entries(res.fields || {})) {
      if (!names.includes(name)) continue;
      if (name === "printer_name" && String(this._data.printer_name || "").trim()) continue; // a name typed by the user wins
      this._data[name] = value;
      filled[name] = true;
    }
    const how = res.source === "ai" ? `read by ${res.entity || "your AI provider"}` : "read on this device";
    Object.assign(this._photo, {
      filled,
      warnings: (res.warnings || []).filter((n) => filled[n]),
      status: Object.keys(filled).length
        ? `Filled from the photo (${how}). Check the highlighted values: 0/O, 1/l/I and 5/S look alike.`
        : "Could not read the photo — type the values.",
      kind: Object.keys(filled).length ? "ok" : "err",
    });
    this._render();
  }

  /** At most PHOTO_MAX_PX wide or high, as JPEG: quick to send and to read. */
  async _shrinkPhoto(file) {
    const bitmap = await createImageBitmap(file);
    const scale = Math.min(1, PHOTO_MAX_PX / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    bitmap.close?.();
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.88));
    const b64 = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(blob);
    });
    return { blob, b64 };
  }

  _loadTesseract() {
    if (window.Tesseract) return Promise.resolve(window.Tesseract);
    if (!this._tesseract) {
      this._tesseract = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = TESSERACT_URL;
        script.onload = () => resolve(window.Tesseract);
        script.onerror = () => {
          this._tesseract = null;
          reject(new Error("The text reader could not be downloaded"));
        };
        document.head.appendChild(script);
      });
    }
    return this._tesseract;
  }

  async _ocr(blob) {
    const Tesseract = await this._loadTesseract();
    const { data } = await Tesseract.recognize(blob, "eng");
    return data?.text || "";
  }

  // ------------------------------------------------------------------ events
  _bind() {
    const root = this.shadowRoot;
    root.getElementById("menu")?.addEventListener("click", () =>
      this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true })),
    );
    root.querySelectorAll("[data-act]").forEach((el) =>
      el.addEventListener("click", (ev) => {
        ev.preventDefault();
        this._action(el);
      }),
    );
    root.getElementById("hfilter")?.addEventListener("change", (ev) => {
      this._history.filter = ev.target.value;
      this._history.limit = 10;
      this._loadHistory();
    });
    // A wheel over a number field must scroll the page, not change the value.
    root.querySelectorAll('input[type="number"]').forEach((el) => el.addEventListener("wheel", () => el.blur(), { passive: true }));
    root.querySelectorAll("input.cb").forEach((el) => this._setupCombo(el));
    root.querySelectorAll("input.photo-in").forEach((el) => {
      el.addEventListener("change", () => this._readPhoto(el.files?.[0]));
    });
    const form = root.getElementById("form");
    if (form) {
      form.addEventListener("submit", (ev) => {
        ev.preventDefault();
        this._submit(this._collect());
      });
      const code = root.querySelector('[data-name="access_code"]');
      (code && !code.value && this._data.host ? code : root.querySelector('[data-name="host"]'))?.focus();
    }
  }

  async _action(el) {
    const act = el.dataset.act;
    if (act === "photochoose") return this._pickPhoto();
    if (act === "add") return this._startAdd();
    if (act === "scan") return this._scan();
    if (act === "found") {
      this._dropFlow(); // leaving the menu flow of "Add printer"
      return this._continueFound(el.dataset.flow);
    }
    if (act === "edit") return this._startEdit(el.dataset.entry);
    if (act === "back") return this._backToList();
    if (act === "menu") return this._submit({ next_step_id: el.dataset.step });
    if (act === "more") {
      this._history.limit += 20;
      return this._loadHistory();
    }
    if (act === "showprice") {
      Object.assign(this._data, this._collect());
      this._showPrice = true;
      return this._render();
    }
    if (act === "askremove" || act === "noremove") {
      this._confirm = act === "askremove" ? "remove" : null;
      return this._render();
    }
    if (act === "doremove") return this._remove();
    if (act === "camzoom" || act === "zoomclose") {
      this._zoom = act === "camzoom" ? el.dataset.entity : null;
      return this._render();
    }
    if (act === "askno") {
      this._ask = null;
      return this._render();
    }
    if (act === "askcancel") {
      this._ask = { kind: "cancel", entry: el.dataset.entry };
      return this._render();
    }
    if (act === "power") {
      if (el.dataset.on === "1") {
        // A second tap while the box is open is the same as "Keep on".
        const open = this._ask?.kind === "power" && this._ask.entry === el.dataset.entry;
        this._ask = open ? null : { kind: "power", entry: el.dataset.entry };
        return this._render();
      }
      const p = this._printers().find((x) => x.entry.entry_id === el.dataset.entry);
      return this._call("switch", "turn_on", p?.ents.safe_power, "Turning the plug on…");
    }
    if (act === "poweroff") {
      this._ask = null;
      // Keep the toggle orange ("wait") until the switch reports its new phase (or the plug is off).
      const entry = el.dataset.entry;
      this._offing = { ...(this._offing || {}), [entry]: Date.now() };
      setTimeout(() => this._view === "list" && this._render(), OFFING_MS + 500);
      const ok = await this._call("switch", "turn_off", el.dataset.entity, "Turning off…");
      if (!ok && this._offing) {
        delete this._offing[entry];
        this._render();
      }
      return;
    }
    if (act === "ctl") {
      this._ask = null;
      const what = el.dataset.what;
      return this._call("button", "press", el.dataset.entity, what === "cancel" ? "Cancelling the print…" : what === "pause" ? "Pausing…" : "Resuming…");
    }
    if (act === "mark") return this._mark(el.dataset.entry, el.dataset.result, el.dataset.job);
    if (act === "light") return this._call("light", "toggle", el.dataset.entity, "Switching the light…");
    if (act === "zsize") {
      this._zstep = { ...(this._zstep || {}), [el.dataset.entry]: Number(el.dataset.step) };
      return this._render();
    }
    if (act === "zasksave") {
      this._ask = { kind: "zsave", entry: el.dataset.entry };
      return this._render();
    }
    if (act === "zstep" || act === "zreset" || act === "zsave") {
      this._ask = null;
      const data = { entry_id: el.dataset.entry };
      if (act === "zstep") Object.assign(data, { direction: el.dataset.dir, step: this._zstep?.[el.dataset.entry] ?? 0.01 });
      if (act === "zreset") data.reset = true;
      if (act === "zsave") data.save = true;
      return this._service("babystep", data, act === "zsave" ? "Saving the Z offset…" : act === "zreset" ? "Z offset back to 0" : `Z ${data.direction === "up" ? "+" : "−"}${data.step} mm`);
    }
  }

  async _service(service, data, message) {
    this._flash(message);
    try {
      await this._hass.callService(DOMAIN, service, data);
    } catch (err) {
      this._flash(String(err?.message || err));
    }
  }

  async _mark(entryId, result, jobId) {
    const data = { entry_id: entryId, result };
    if (jobId) data.job_id = jobId;
    this._flash(result === "success" ? "Marked as a success" : "Marked as failed");
    try {
      await this._hass.callService(DOMAIN, "mark_print", data);
      await this._loadHistory();
    } catch (err) {
      this._flash(String(err?.message || err));
    }
  }

  async _call(domain, service, entity, message) {
    if (!entity) return;
    this._flash(message);
    try {
      await this._hass.callService(domain, service, { entity_id: entity });
      return true;
    } catch (err) {
      this._flash(String(err?.message || err));
      return false;
    }
  }

  _flash(text) {
    this._toast = text;
    this._render();
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => {
      this._toast = null;
      this._render();
    }, 4000);
  }

  async _remove() {
    const entryId = this._flow?.entryId;
    if (!entryId) return;
    this._busy = true;
    this._render();
    try {
      this._dropFlow();
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
