<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo/fdm-printer-manager-logo-dark.svg">
    <img alt="FDM Printer Manager" src="docs/logo/fdm-printer-manager-logo.svg" width="380">
  </picture>
</p>

# FDM Printer Manager for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Tests](https://github.com/3devangelist/ha-3d-print-cost/actions/workflows/tests.yml/badge.svg)](https://github.com/3devangelist/ha-3d-print-cost/actions/workflows/tests.yml)

Add your FDM 3D printers to Home Assistant in one window, and get for every
print: what it is doing, how long it took, how much **energy** it used and what
that electricity **cost**. Filament is out of scope.

- **Klipper**: type the printer's IP address. Nothing else to install —
  Home Assistant talks to Moonraker directly.
- **Bambu Lab**: directly on the LAN (IP, serial number, access code — no cloud, no
  other integration), or through the [Bambu Lab integration](https://github.com/greghesp/ha-bambulab)
  if you already use it.
- **Found automatically**: Klipper printers (searched on your network, or announced by
  Moonraker zeroconf) and Bambu Lab printers (SSDP on the LAN) show up as *Discovered*.
- **OctoPrint**: planned, for older machines.
- **Any smart plug**. With an energy meter you also get consumption and cost;
  a plain on/off plug works too.

> Based on [ha-3d-print-cost](https://github.com/ZoziLaMalice/ha-3d-print-cost)
> by ZoziLaMalice (MIT). Thanks for the clean base.

> **Status: 0.4.0 — early version.** Expect changes. Klipper, Bambu LAN and Bambu (via ha-bambulab) work;
> print history, to-do list, safe power-off and the other items in the
> [roadmap](#roadmap) are being added.

## Installation (HACS)

1. HACS → ⋮ → **Custom repositories** → add
   `https://github.com/3devangelist/ha-3d-print-cost`, category **Integration**.
2. Install **FDM Printer Manager**, restart Home Assistant.
3. **Settings → Devices & services → Add integration → FDM Printer Manager.** The first
   time this only adds the **FDM Printers** page to the sidebar — one click, no
   questions. Add the printers from that page.

Manual install: copy `custom_components/fdm_printer_manager` into your
`config/custom_components/` folder and restart.

## Add a printer

**Settings → Devices & services → Add integration → FDM Printer Manager.**
Everything happens in that one window:

1. **What kind of printer?** Klipper, Bambu Lab (LAN) or Bambu Lab (via the Bambu
   Lab integration). Printers found on the network are listed first.
2. **Connection**
   - *Klipper*: just the IP address. Moonraker is looked for on port 7125, then 80
     (add `:port` only if it uses another one). The connection is tested before you can continue. Name, port
     and web interface (`http://<IP>/`, i.e. Mainsail or Fluidd) are filled in
     automatically. An API key is asked only if your Moonraker refuses the
     connection without one.
   - *Bambu Lab (LAN)*: IP address, serial number and access code (printer screen →
     Settings → LAN only mode; **Developer mode** is optional, only needed to send commands
     such as pause or the chamber light). A printer
     found on the network already has IP and serial filled in. **🖼️ Choose photo** fills
     the three fields from a photo of the printer's LAN Only page, taken with your camera
     app without flash (read with your AI Task, or
     read on your device). Step by step: [Connect a Bambu Lab printer over LAN](docs/bambu-lan-only.md)
     ([em português](docs/bambu-lan-only.pt.md)).
   - *Bambu Lab (via the Bambu Lab integration)*: pick the printer from ha-bambulab;
     its entities are found automatically.
3. **Smart plug** — the switch the printer is plugged into. Its power sensor
   and energy meter are found automatically; type the price per kWh once (it is
   reused for the next printers). The printer then gets its own counters:
   energy and cost in total and today.

Easiest: use the **FDM Printers** page in the sidebar. Klipper printers are searched
for on Home Assistant's own network (at most 254 addresses, ports 7125 and 80) when
the integration starts, every 30 minutes, when you open **Add printer** and with
**Search again**; Moonraker's `[zeroconf]` section is not needed. Bambu printers
announce themselves every few seconds while LAN mode is on.

The printer appears as a device with its name. For Klipper the device page has
a **Visit** button that opens Mainsail / Fluidd.

## Edit a printer

**Configure** on the printer's entry changes the connection (Klipper: IP
address, port, API key, web interface, name; Bambu LAN: IP, access code, name;
Bambu via ha-bambulab: its entities), the
plug, meters and price — without deleting it. The
device, its entities and its history are kept, and a print in progress
survives the reload.

| Option                   | Default                              |                                                                      |
| ------------------------ | ------------------------------------ | -------------------------------------------------------------------- |
| Sampling interval        | 30 s                                 | Klipper is also polled every 5 s; Bambu updates on every change      |
| Currency                 | HA currency                          | used for units only, no conversion                                   |
| Power integration method | left                                 | `left` suits step-like plug sensors, `trapezoidal` smooth ones       |
| Turn off after a print   | on                                   | needs a smart plug; off when the nozzle is below 80 °C (see below)   |
| Wait before turning off  | 0 min                                | minutes after a successful print before the auto-off starts          |
| Filament price per kg    | empty                                | this printer's price; empty: the generic price of the material       |
| SSH login (Klipper)      | empty                                | advanced: only for printers whose Moonraker cannot shut down         |

## Turning a printer off safely

The **Power** button on the card (`switch.<printer>_safe_power`) and the
**auto-off** after a print do the same thing:

1. never while printing or paused, never while a print is still open;
2. **never without contact with the printer**: offline, Moonraker or MQTT not
   answering, state unknown → refused with "No contact with the printer —
   power not cut. Use the plug directly if you are sure.";
3. never when it does not look like the same printer (see below);
4. wait for the nozzle to drop below **80 °C** (at most 30 min, else the plug stays on);
5. Klipper: shut the printer's computer down — Moonraker's `machine.shutdown`
   first, then SSH if Moonraker refuses;
6. cut the plug only once the computer **stops answering** (ping, or a TCP
   check): 3 checks in a row 5 s apart, then 10 s more. Still answering after
   5 minutes, the plug is left on and a notification says so.

Auto-off starts only after a **successful** print (on/off in Configure →
Advanced). With **Wait before turning off** at 0 it starts right away and turns
off as soon as the nozzle is below 80 °C; with, say, 10 min the card shows
"Auto off at HH:MM" first, so there is time to take the part off and start
another print. A failed, cancelled, interrupted or unknown print leaves the
printer on, so you can look at it. A new print, turning the plug off by hand,
tapping Power on the card, or **Keep on** cancels it. Losing contact with the
printer while the nozzle cools leaves the plug on.

**Keep on** (0.15.0): the card's **Keep on** button, `button.<printer>_keep_on`,
the action `fdm_printer_manager.keep_on`, or the button of the phone notification
below. While the auto-off waits or the nozzle cools, it cancels it; during a print,
that print will not turn the printer off. The next print turns it off as usual.

**Notification when a print ends** (0.15.0, **Edit → When a print ends**): printer,
file, result, duration and when the printer turns itself off, sent to the notify
actions you choose (e.g. `notify.mobile_app_phone`). A printer without its own list
uses the one of **FDM Printers → Configure**; with none, nothing is sent. With the
Home Assistant app the notification has a **Keep on** button.

**⚡ Power off now** (0.15.0): for a printer, Moonraker or OctoPrint that hangs. It
cuts the plug **at once**: no cooling, no shutdown of the computer, no contact
needed. On the card it needs **3 taps** ("Are you sure? (2 of 3)", "Last
confirmation (3 of 3)"), each within 5 s of the last, and warns when a print is
running. `button.<printer>_power_off_now` also needs 3 presses within 5 s each;
the action `fdm_printer_manager.force_power_off` needs `confirm: true`. A running
print is recorded as **failed by the user**, with a note, and the auto-off is
cancelled. Every printer with a smart plug has it (Klipper, Bambu Lab; the
integration does not talk to OctoPrint yet).

**Elegoo Centauri Carbon**: its Moonraker cannot shut down (HTTP 500 "Error
running shell command"). **OpenCentauri Cosmos 26.10 and later** shut down cleanly from Moonraker, so
no SSH is used; with an older Cosmos the card and Settings → Repairs recommend
the update. Older Cosmos is recognised (host name `cosmos` or
`centauri-carbon-N`, or that error)
and shut down with a single `poweroff` over SSH as `root`, without a password,
as the printer ships — nothing to set up. For any other printer that needs
SSH, give a login under Configure → Advanced (user, optional password or
private key, command such as `sudo poweroff`). Only that one command is run, in
the remote user's login shell. The printer's SSH host key is remembered the
first time and required afterwards, so the password is never sent to another
machine. The password and key are never logged and are redacted from
diagnostics.

**Is it still the same printer?** On first contact the integration remembers
who the printer is: the Bambu Lab broker's TLS certificate (checked before the
access code is sent), the Klipper computer's identity from Moonraker (CPU
serial, else MAC addresses, else host name) and the SSH host key. If something
else answers later at the printer's address, commands and power off are
blocked and a repair issue says so. After a firmware reinstall or a new board,
accept the new identity under Configure (first page).

## Lights, baby steps and filaments

The card only shows what the printer really has (read once, read-only:
`printer.objects.list` and `printer.gcode.help` on Klipper).

- **Lights**: 💡 one button per light — Klipper LEDs (`SET_LED`) and
  `output_pin`s named like a light (`SET_PIN`), the Bambu Lab chamber light.
  Also `light.<printer>_light_<name>`.
- **Baby steps** (Klipper, while printing): Z offset with ▼ / ▲, steps of
  0.005 / 0.01 / 0.025 / 0.05 mm, and Reset. Each tap sends
  `SET_GCODE_OFFSET Z_ADJUST=±step MOVE=1`, like Mainsail and Fluidd; the
  offset shown is Klipper's `gcode_move.homing_origin`. At most ±1 mm from 0
  when that offset is known; not known yet, the step is still sent. **Save** is off
  by default (Configure → Advanced); it applies the offset to the probe or
  endstop, then run `SAVE_CONFIG` yourself when the printer is idle. Action:

  ```yaml
  action: fdm_printer_manager.babystep
  data:
    entry_id: <the printer's entry id>
    direction: down # or up
    step: 0.01
  ```

- **Filaments**: one chip per slot of a multi-material unit, with its real
  colour, type and % left; the active slot is outlined. AFC / Box Turtle /
  Elegoo CANVAS (Centauri Carbon, "cosmos" firmware), Happy Hare, Spoolman's
  active spool, Bambu Lab AMS (LAN). Also `sensor.<printer>_filaments`.
- **Filament of each print** (0.15.0): "Recent prints" and the card's last line
  show the metres (and grams) before the cost, e.g. "112,2 m · 281 g · ASA", and
  the cost is electricity + filament. The **material** comes from the slicer
  (`filament_type`), else the spool loaded while printing (AFC / CANVAS lane,
  Happy Hare gate, Spoolman, Bambu AMS tray), else the file name (`…_ASA_….gcode`).
  Names are made the same (PLA, PETG, ABS, ASA, TPU, PC, PA, PP, PPA, PPS, PET,
  PLA-CF, PETG-CF, PET-CF). Not found: "?" and a list in "Recent prints" to choose
  it (or to correct a wrong one; action `fdm_printer_manager.set_material`).
- **Filament price**: the printer's own price per kg (Edit → Advanced), else a
  **generic price per material** from **FDM Printers → Configure** (starting
  values in €/kg: PLA 20, PLA-CF 35, PETG 20, PETG-CF 35, PET 25, PET-CF 50, ABS 20,
  ASA 25, TPU 30, PC 35, PA 40, PP 35, PPA 60, PPS 80), else no cost and a note.
  A cost from a generic price says so in the notes ("generic price") and has a `*`
  in "Recent prints".

## Watching prints

On by default (**Edit → Watch prints**). Nothing is ever resumed automatically.

| What happens | What you get |
| ------------ | ------------ |
| The printer stops answering in the middle of a print (2 min) | ⚠️ warning; the card says "Not answering" |
| It answers again and prints | 🔌 "resumed" |
| It answers again but does not print (Klipper comes back in *standby* after a power cut, or a state got stuck) | after 10 min the print is **closed as failed**, with a note |
| A Bambu Lab printer comes back *idle* after being down | the print ends as **failed** at once |
| The progress does not move for 60 min (0 = never) | ⏳ warning |

Each alert is a Home Assistant notification, the event `fdm_printer_manager_alert`
(`kind`: `down`, `back`, `resumed`, `interrupted`, `stalled`) and, optionally, your
notify actions (e.g. `notify.mobile_app_phone, notify.mobile_app_tablet`).

**Mark a print**: the printer's verdict is not always right. **✓ OK** / **✗ Failed** on the
card (last print) and on every row of "Recent prints", or the action
`fdm_printer_manager.mark_print`. "Recent prints" shows the success rate and what failed and
cancelled prints cost.

**When a print ends** (**Edit → When a print ends**): add "Remove the print from
&lt;printer&gt;" to a to-do list, and / or start a script with the print summary as variables,
and / or add a row to a Google Sheet (below).

### Google Sheets log

Every print that ends (finished, failed, cancelled or unknown) adds one row to a Google Sheet,
through Home Assistant's own [Google Sheets](https://www.home-assistant.io/integrations/google_sheets/)
integration.

Step by step, with the Google Cloud credentials it needs once:
[Log every print to a Google Sheet](docs/google-sheets.md) ([em português](docs/google-sheets.pt.md)).

1. Once: create Google Cloud OAuth credentials (about 10 minutes, see the guide).
2. **FDM Printers → Google Sheets → Connect Google Sheets**: Home Assistant's own window opens;
   you only sign in with Google. It creates a spreadsheet called "Home Assistant" in your Drive.
3. **FDM Printers → Google Sheets → Start logging**: pick the spreadsheet and the tab name
   (default **Prints**). The tab is created if it is missing (with the header row), and the log is
   turned on for every printer. The page then shows "✓ Logging to … › Prints", a link to the
   sheet, and **Turn off for all printers**.

The same from an automation or script: the action `fdm_printer_manager.sheets_setup`
(`config_entry`, `worksheet`, optional `entry_id`; returns the spreadsheet URL).
One printer on its own tab: **Edit → When a print ends** (**Google Sheets log** and **Worksheet**).

Columns: `created` (added by Google Sheets), `printer`, `file`, `result` (`success`, `failed`,
`cancelled`, `unknown`), `result_by` (`printer`, `watchdog` or `user`), `started`, `finished`
(local time), `duration_min`, `energy_kwh`, `electricity_cost`, `currency`, `notes`, `job_id`,
then (0.14.0) `filament_mm`, `filament_g`, `filament_type`, `filament_cost`: the filament really used,
then (0.15.0) `filament_m`, `filament_price_per_kg`, `filament_price_source` (`printer` or `generic`)
and `print_cost` (electricity + filament). Choosing a material by hand changes the material and cost
cells of the print's row in place, like marking a result.
Missing headers are added by Google Sheets on the first row.

- **One row per print.** If you later mark a print ("Wrong? Mark as failed / Mark as OK" on
  the card) and the result changes, the print's row is changed in place: its `result` and
  `result_by` (`user`) cells, found by `job_id`. A row still waiting to be sent is changed before
  it goes. Only when the row cannot be found or changed is a *correction* row added instead,
  with the same `job_id` and a note: then keep the last row of each `job_id` when you count.
- **Nothing is lost when Google is unreachable**: the row waits in the integration's storage
  and is sent before the next one, at the next print or when Home Assistant starts. You get one
  notification until it goes through. Up to 200 rows wait.
- **Limits of the Google Sheets integration**: it only writes to the spreadsheet it created
  (not to one you made yourself). The **Google Sheets** button creates the tab; with the options
  form alone, the tab must already exist.

**New access code (Bambu Lab LAN)**: it changes when LAN Only mode is turned off and on. When
the printer refuses it, Home Assistant asks for the new one (also at the top of the FDM
Printers page, with **🖼️ Choose photo**).

## How a print is detected

| Printer state (Klipper / Bambu)     | Meaning                                                    |
| ----------------------------------- | ---------------------------------------------------------- |
| `printing`, `paused` / `running`, `pause`, `prepare` | print in progress                         |
| `complete` / `finish`               | ended, success                                             |
| `cancelled` / `cancel`              | ended, cancelled                                           |
| `error` / `failed`                  | ended, failed (Klipper shutting down mid-print counts too) |
| printer unreachable                 | the print stays open until the printer answers again       |

## Electricity

- **With an energy meter** (recommended) the energy of a print is
  `meter_end − meter_start`, exact even if Home Assistant restarts mid-print.
- **With power only** the power is integrated over time (`left` or
  `trapezoidal`). A Home Assistant restart leaves a hole that is bridged and
  reported in the `notes` attribute.
- **The price can change during a print** (Tempo, Octopus, HP/HC…). The cost is
  accumulated interval by interval, `energy(interval) × price(interval)`, and a
  sample is taken at each price change. It is **never** `total energy × last price`.
- An unknown price is **never** counted as zero: the energy is still measured,
  `electricity_cost_complete` becomes `false` and the `notes` say how many kWh
  could not be priced.

Supported units: power `W`, `kW` · energy `Wh`, `kWh`, `MWh` · price `EUR/kWh`,
`€/kWh`, `c€/kWh`, `cent/kWh`, `EUR/MWh`, `€/MWh` (any currency symbol).

## Entities

One device named after the printer is created (for Bambu Lab it is linked to
the Bambu Lab device). Entity ids below assume a printer named _X2D_; an
_Ender 6_ gets `sensor.ender_6_…`.

**Current print** (the running print, then the one being finalised; `unknown` when idle)

| Entity                                      | Unit  |                                                                               |
| ------------------------------------------- | ----- | ----------------------------------------------------------------------------- |
| `sensor.x2d_current_print_cost`             | €     | electricity cost so far; attributes: job, progress, remaining time, notes     |
| `sensor.x2d_current_print_energy`           | kWh   |                                                                               |
| `sensor.x2d_current_print_duration`         | h     |                                                                               |
| `sensor.x2d_current_print_cost_per_hour`    | €/h   |                                                                               |
| `sensor.x2d_current_print_phase`            |       | `idle` / `active` / `finalizing`                                              |
| `sensor.x2d_electricity_price`              | €/kWh | diagnostic: normalised price currently used                                   |

**Last print** (kept across restarts)

`sensor.x2d_last_print_cost` (full summary in attributes), `…_energy`,
`…_duration`, `…_result` (`success` / `cancelled` / `failed` / `unknown`),
`…_filament` (metres; grams, material and costs in the attributes).

**Buttons** (with a smart plug): `button.x2d_keep_on`, `button.x2d_power_off_now`
(3 presses within 5 s each).

**Last print image**

`image.x2d_last_print_image` (Bambu Lab only, for now) — copy of the ha-bambulab cover image taken when
the print ended, so it still shows the right model once the next print has
started. It is also the picture of `sensor.x2d_last_print_cost`
(stored in `.storage/fdm_printer_manager.<entry_id>.image`).

**Event**

`event.x2d_print_finished` — and the bus event `fdm_printer_manager_print_finished` — with:

```yaml
name: Flower Lamp
result: success
started_at: "2026-10-28T10:02:00+00:00"
finished_at: "2026-10-28T11:14:00+00:00"
duration_s: 4320
energy_kwh: 1.02
electricity_cost: 0.16
electricity_cost_complete: true
notes: []
```

Per-print sensors have no `state_class` on purpose: they reset at every print
and must not pollute long-term statistics or the Energy dashboard.

## Dashboard cards

A dashboard that lists every printer automatically is on the [roadmap](#roadmap).

## Automations

```yaml
alias: Print cost notification
triggers:
  - trigger: state
    entity_id: event.x2d_print_finished
actions:
  - action: notify.mobile_app_phone
    data:
      title: "{{ state_attr(trigger.entity_id, 'name') }} — {{ state_attr(trigger.entity_id, 'result') }}"
      message: >
        {{ state_attr(trigger.entity_id, 'electricity_cost') | round(2) }} €
        ({{ state_attr(trigger.entity_id, 'energy_kwh') | round(2) }} kWh)
```

## Troubleshooting

| Symptom                    | Check                                                                                                  |
| -------------------------- | ------------------------------------------------------------------------------------------------------ |
| No print detected (Klipper) | open `http://<IP>:7125/server/info` in a browser: it must answer. If Moonraker asks for an API key, set it in **Configure**. |
| Bambu LAN "access code refused" / cannot connect | LAN only mode must be on; the code changes when LAN mode is toggled. Developer mode is not needed to connect (only to send commands). Port 8883 must be reachable. See [the Bambu LAN guide](docs/bambu-lan-only.md). |
| Choose photo reads wrong values | Retake the picture without flash and with lower exposure, filling the frame with the LAN Only page; the fields stay editable. |
| No print detected (Bambu)  | the _print status_ entity must report `prepare` / `running` / `pause` (ha-bambulab `print_status`).    |
| Two prints merged / split  | configure the _job name_ entity.                                                                       |
| Electricity cost `unknown` | the price entity was `unknown`/`unavailable` or in an unsupported unit; see `notes`.                   |
| No energy or cost          | the plug has no power sensor / energy meter: only the print itself is tracked.                         |

Download the diagnostics (device page → ⋮ → _Download diagnostics_): the API key
is redacted. Debug logs:

```yaml
logger:
  logs:
    custom_components.fdm_printer_manager: debug
```

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest            # units, tariff change, meter outage, life cycle, restart, flows, Klipper
ruff check . && ruff format --check .
```

```text
custom_components/fdm_printer_manager/
├── models/          PrintJob, ElectricityAccumulator                  ← pure Python
├── units.py         unit normalisation                                ← pure Python
├── tracker.py       print life cycle state machine                    ← pure Python
├── adapters/        moonraker (Klipper, HTTP), bambu_lan (MQTT/TLS, SSDP), bambu (ha-bambulab), electricity
├── coordinator.py   listeners, timer, storage, finalisation, event
├── sensor.py / event.py / config_flow.py / diagnostics.py / storage.py
```

Business logic has no Home Assistant import and is tested without it. External
systems sit behind adapters that are mocked in tests. Money is computed with
`Decimal` and only rounded for display.

## Roadmap

Done (from the original project): electricity cost per print, tariff changes,
restart recovery, last print, `print_finished` event.

- ✅ **0.3.0-alpha.1** — new name, one-window "Add printer", **Klipper direct**
  (Moonraker), plug without meter, edit the connection without deleting the
  printer, link to the printer's web interface; filament tracking removed
  (energy cost only)
- ✅ **0.3.0-alpha.2/3** — FDM Printers sidebar page, permanent print history, safe power
  switch, Pause / Resume / Cancel, the printer's own energy and cost counters, typed price
- ✅ **0.3.0-alpha.4** — Bambu Lab direct LAN, discovery (Moonraker zeroconf, Bambu SSDP),
  one-click panel on the first Add integration
- ✅ **0.5.0** — Klipper printers found by searching the network (no zeroconf needed),
  Moonraker on port 7125 or 80, "Search again" in the FDM Printers page
- ✅ **0.8.0** — auto power-off after a print, once the nozzle is cold and the printer's
  computer is down (SSH fallback for the Elegoo Centauri Carbon); camera inside the page
- ✅ **0.9.0** — "Mark success" / "Mark failed"; watch prints (printer down, interrupted or
  stuck print closed as failed, no progress); new access code (Bambu LAN); to-do item and
  script when a print ends; better default names
- 🚧 OctoPrint
- 🚧 LLM assistant to import an existing setup (to think about)
- ✅ **0.11.0** — one row per print in a Google Sheet (Google Sheets integration)
- ✅ **0.12.0** — "Google Sheets" button in the FDM Printers page: connect, create the tab and
  log every printer in one click; guide in English and Portuguese
- ✅ **0.13.0** — camera of Bambu Lab A1, A1 mini, P1P and P1S in LAN mode (no other
  integration needed) on the printer's card; X1 / H2 / P2S (RTSP) not supported yet

## License

MIT — see [LICENSE](LICENSE). Original work © ZoziLaMalice.
