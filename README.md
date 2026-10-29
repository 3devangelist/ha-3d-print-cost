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
- **Bambu Lab**: through the [Bambu Lab integration](https://github.com/greghesp/ha-bambulab)
  for now. A direct LAN (offline) connection is planned.
- **OctoPrint**: planned, for older machines.
- **Any smart plug**. With an energy meter you also get consumption and cost;
  a plain on/off plug works too.

> Based on [ha-3d-print-cost](https://github.com/ZoziLaMalice/ha-3d-print-cost)
> by ZoziLaMalice (MIT). Thanks for the clean base.

> **Status: alpha (0.3.0-alpha.1).** Expect breaking changes. Klipper and Bambu (via ha-bambulab) work;
> print history, to-do list, safe power-off and the other items in the
> [roadmap](#roadmap) are being added.

## Installation (HACS)

1. HACS → ⋮ → **Custom repositories** → add
   `https://github.com/3devangelist/ha-3d-print-cost`, category **Integration**.
2. Install **FDM Printer Manager**, restart Home Assistant.

Manual install: copy `custom_components/fdm_printer_manager` into your
`config/custom_components/` folder and restart.

## Add a printer

**Settings → Devices & services → Add integration → FDM Printer Manager.**
Everything happens in that one window:

1. **What kind of printer?** Klipper or Bambu Lab.
2. **Connection**
   - *Klipper*: just the IP address (add `:port` only if Moonraker does not
     use 7125). The connection is tested before you can continue. Name, port
     and web interface (`http://<IP>/`, i.e. Mainsail or Fluidd) are filled in
     automatically. An API key is asked only if your Moonraker refuses the
     connection without one.
   - *Bambu Lab*: pick the printer from the Bambu Lab integration; its entities
     are found automatically.
3. **Smart plug** — the switch the printer is plugged into. Its power sensor
   and energy meter are found automatically; type the price per kWh once (it is
   reused for the next printers). The printer then gets its own counters:
   energy and cost in total and today.

Easiest: use the **FDM Printers** page in the sidebar (Add integration →
FDM Printer Manager → "Only add the FDM Printers panel" the first time).

The printer appears as a device with its name. For Klipper the device page has
a **Visit** button that opens Mainsail / Fluidd.

## Edit a printer

**Configure** on the printer's entry changes the connection (Klipper: IP
address, port, API key, web interface, name; Bambu Lab: its entities), the
plug, meters and price — without deleting it. The
device, its entities and its history are kept, and a print in progress
survives the reload.

| Option                   | Default                              |                                                                      |
| ------------------------ | ------------------------------------ | -------------------------------------------------------------------- |
| Sampling interval        | 30 s                                 | Klipper is also polled every 5 s; Bambu updates on every change      |
| Currency                 | HA currency                          | used for units only, no conversion                                   |
| Power integration method | left                                 | `left` suits step-like plug sensors, `trapezoidal` smooth ones       |

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
`…_duration`, `…_result` (`success` / `cancelled` / `failed` / `unknown`).

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
├── adapters/        moonraker (Klipper, HTTP), bambu, electricity (HA states)
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
- 🚧 to-do list entry per print (⏳ while printing, ✅ / ❌ at the end)
- 🚧 run a script / notify when a print ends
- 🚧 printer down while printing: notify, wait, close as failed if it comes back idle
- 🚧 auto power-off after a successful print, once the nozzle is cold
- 🚧 "Mark success" / "Mark failed" buttons
- 🚧 Bambu Lab direct LAN connection (no other integration)
- 🚧 OctoPrint
- 🚧 LLM assistant to import an existing setup (to think about)
- 🚧 guide: sending each print to Google Sheets

## License

MIT — see [LICENSE](LICENSE). Original work © ZoziLaMalice.
