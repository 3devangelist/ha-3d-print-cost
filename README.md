# 3D Print Cost for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Tests](https://github.com/ZoziLaMalice/ha-3d-print-cost/actions/workflows/tests.yml/badge.svg)](https://github.com/ZoziLaMalice/ha-3d-print-cost/actions/workflows/tests.yml)
[![Hassfest](https://github.com/ZoziLaMalice/ha-3d-print-cost/actions/workflows/hassfest.yml/badge.svg)](https://github.com/ZoziLaMalice/ha-3d-print-cost/actions/workflows/hassfest.yml)

_[Version française](README.fr.md)_

**What did that print actually cost?** This integration computes, for every
Bambu Lab print, the **real electricity cost**, the **real filament cost** and
the **total**, and exposes them as standard Home Assistant entities — for
the **print in progress** and the **last print** only. Long-term history and
statistics are deliberately left to other tools.

```text
Flower Lamp — success — 3 h 31 min

Electricity   0.640 kWh     0.13 €
Filament      PLA Black   62.4 g   1.21 €
              PLA White   22.2 g   0.51 €
              total       84.6 g   1.72 €
TOTAL                              1.85 €
```

It is an **aggregation layer**: it does not re-implement what other tools
already do well.

> **This integration never modifies your Spoolman stock.**
> HaspelSync stays responsible for filament consumption and spool synchronisation.

---

## Contents

1. [How it works](#how-it-works)
2. [Requirements](#requirements)
3. [Installation (HACS)](#installation-hacs)
4. [Configuration](#configuration)
5. [Electricity](#electricity)
6. [Filament: HaspelSync and Spoolman](#filament-haspelsync-and-spoolman)
7. [Entities](#entities)
8. [Dashboard cards](#dashboard-cards)
9. [Automations](#automations)
10. [Troubleshooting](#troubleshooting)
11. [Development](#development)
12. [Roadmap](#roadmap)

---

## How it works

```text
 ha-bambulab ─── printer state, job name, progress, sliced weight ──┐
                                                                     │
 Smart plug  ─── power (W) and/or energy meter (Wh/kWh) ────────────┤
                                                                     ├──► 3D Print Cost ──► sensors + event
 Price sensor ── EUR/kWh, c€/kWh, EUR/MWh (may change any time) ────┤      (cost of the print)
                                                                     │
 HaspelSync  ─── real grams per spool, read from the G-code ────────┤
                                                                     │
 Spoolman    ─── spool prices (read-only) ──────────────────────────┘
```

| System                                                 | Responsible for                                                                                            |
| ------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| [ha-bambulab](https://github.com/greghesp/ha-bambulab) | what the printer is doing                                                                                  |
| [HaspelSync](https://github.com/Rdiger-36/HaspelSync)  | which spool is in which slot, what was really consumed (cancelled prints included), booking it in Spoolman |
| [Spoolman](https://github.com/Donkie/Spoolman)         | spools, materials, colours, prices                                                                         |
| **this integration**                                   | putting it together and computing the costs                                                                |

Everything runs **locally**; no internet access is needed.

## Requirements

|                          | Required   | Notes                                                                            |
| ------------------------ | ---------- | -------------------------------------------------------------------------------- |
| Home Assistant           | ✅ 2025.2+ |                                                                                  |
| ha-bambulab              | ✅         | any Bambu Lab printer it supports                                                |
| Smart plug               | ✅         | a power sensor (W) **or** a cumulative energy meter (Wh/kWh); both is best       |
| Electricity price entity | ✅         | any `sensor` / `input_number` in a supported unit; can be a fixed `input_number` |
| HaspelSync               | optional   | needed for the real filament consumption (v0.2)                                  |
| Spoolman                 | optional   | needed for filament prices (v0.2)                                                |

Without HaspelSync and Spoolman the integration tracks **electricity only**, and
the total cost equals the electricity cost (`filament_status: disabled`).

## Installation (HACS)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/ZoziLaMalice/ha-3d-print-cost`, category **Integration**.
2. Install **3D Print Cost**, restart Home Assistant.
3. **Settings → Devices & services → Add integration → 3D Print Cost**.

Manual install: copy `custom_components/ha_3d_print_cost` into your
`config/custom_components/` folder and restart.

## Configuration

Everything is done in the UI, no YAML. The flow has four steps:

1. **Printer** — pick the Bambu Lab device. One config entry per printer.
2. **Printer entities** — discovered automatically from the device registry by
   `translation_key` / `unique_id` (never by `entity_id`, so renamed or localised
   entities are found). Only _print status_ is required; _job name_ is strongly
   recommended (it tells two prints apart and matches HaspelSync).
3. **Electricity** — power sensor, energy meter, price entity, integration method.
4. **Filament (optional)** — HaspelSync URL + API key, Spoolman URL, fallback policy.
   If HaspelSync monitors several printers, the one whose serial number matches
   the Bambu device is selected; otherwise you are asked.

Everything can be changed later in **Configure** (options) without removing the
integration; a print in progress survives the reload.

| Option                   | Default                              |                                                                      |
| ------------------------ | ------------------------------------ | -------------------------------------------------------------------- |
| Sampling interval        | 30 s                                 | samples are also taken on every state change of the watched entities |
| Currency                 | HA currency                          | used for units only, no conversion                                   |
| Power integration method | left                                 | `left` suits step-like plug sensors, `trapezoidal` smooth ones       |
| Filament fallback        | planned weight for successful prints | see below                                                            |

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

## Filament: HaspelSync and Spoolman

When a print ends:

1. the electricity values are frozen;
2. HaspelSync is asked for the consumption it booked for **this** print
   (`GET /api/print/{printer}` → `lastPrintSummary`). HaspelSync books a few
   seconds after the end, so the integration retries for up to ~2 minutes;
3. each spool's price is read from Spoolman (`GET /api/v1/spool/{id}`);
4. filament and total costs are computed, the job is stored and the event fired.

**Order of trust for the weight**

1. HaspelSync — real grams from the G-code; cancelled prints are pro-rated per layer by HaspelSync;
2. ha-bambulab per-slot _sliced_ weights, mapped to spools with the slot snapshot taken from HaspelSync at print start;
3. ha-bambulab total _sliced_ weight (priced only if a single spool was loaded);
4. nothing — weight and cost stay unknown rather than wrong.

Fallbacks 2–3 are _planned_ weights. The **filament fallback** option decides when they are used:

| Value                       | Successful print | Failed / cancelled print |
| --------------------------- | ---------------- | ------------------------ |
| `none`                      | never estimated  | never estimated          |
| `planned_success` (default) | sliced weight    | unknown                  |
| `planned_progress`          | sliced weight    | sliced weight × progress |

**Price per gram**, from the Spoolman spool, in this order:

1. a custom _extra_ field `price_per_kg` or `price_per_g` (on the spool or the filament);
2. spool `price` ÷ spool `initial_weight` (or the filament's net `weight`);
3. filament `price` ÷ filament `weight`.

If any spool used by the print has no price, the filament cost — and therefore
the total — is **unknown**; the attribute `partial_total_cost` gives the sum of
what is known.

**HaspelSync API key**: create one in HaspelSync's web UI.
It is stored in the config entry, never in entity attributes, logs or diagnostics.
From HaspelSync 1.3.0, use its IP address or add the host name under _Allowed
host names_, otherwise it answers `403`.

## Entities

One device **3D Print Cost — &lt;printer&gt;** is created and linked to the
Bambu Lab device. Entity ids below assume a printer named _X2D_.

**Current print** (the running print, then the one being finalised; `unknown` when idle)

| Entity                                      | Unit  |                                                                               |
| ------------------------------------------- | ----- | ----------------------------------------------------------------------------- |
| `sensor.x2d_current_print_total_cost`       | €     | attributes: job, progress, per-spool detail, notes; picture = print thumbnail |
| `sensor.x2d_current_print_electricity_cost` | €     |                                                                               |
| `sensor.x2d_current_print_filament_cost`    | €     | known once the print is finalised                                             |
| `sensor.x2d_current_print_energy`           | kWh   |                                                                               |
| `sensor.x2d_current_print_filament_weight`  | g     | known once the print is finalised (`planned_weight_g` attribute meanwhile)    |
| `sensor.x2d_current_print_duration`         | h     |                                                                               |
| `sensor.x2d_current_print_cost_per_hour`    | €/h   |                                                                               |
| `sensor.x2d_current_print_phase`            |       | `idle` / `active` / `finalizing`                                              |
| `sensor.x2d_electricity_price`              | €/kWh | diagnostic: normalised price currently used                                   |

**Last print** (kept across restarts)

`sensor.x2d_last_print_total_cost` (full summary in attributes),
`…_electricity_cost`, `…_filament_cost`, `…_energy`, `…_filament_weight`,
`…_duration`, `…_result` (`success` / `cancelled` / `failed` / `unknown`).

**Last print image**

`image.x2d_last_print_image` — copy of the ha-bambulab cover image taken when
the print ended, so it still shows the right model once the next print has
started. It is also the picture of `sensor.x2d_last_print_total_cost`
(stored in `.storage/ha_3d_print_cost.<entry_id>.image`).

**Event**

`event.x2d_print_finished` — and the bus event `ha_3d_print_cost_print_finished` — with:

```yaml
name: Flower Lamp
result: success
duration_s: 12702
energy_kwh: 0.64
electricity_cost: 0.13
filament_weight_g: 84.6
filament_cost: 1.72
total_cost: 1.85
filament_status: resolved # resolved / partial / unavailable / disabled
filaments:
  - {
      spool_id: 23,
      slot: A1,
      material: PLA,
      name: Matte Black,
      weight_g: 62.4,
      cost: 1.21,
    }
  - {
      spool_id: 45,
      slot: A2,
      material: PLA,
      name: Basic White,
      weight_g: 22.2,
      cost: 0.51,
    }
```

Per-print sensors have no `state_class` on purpose: they reset at every print
and must not pollute long-term statistics or the Energy dashboard.

## Dashboard cards

No custom Lovelace card is shipped. [`examples/dashboard_cards.yaml`](examples/dashboard_cards.yaml)
holds five ready-made cards built with [Bubble Card](https://github.com/Clooos/Bubble-Card)
and native cards:

| Card                   |                                                                                           |
| ---------------------- | ----------------------------------------------------------------------------------------- |
| Current print — simple | thumbnail, name, cost so far, progress; electricity €, kWh, planned weight, duration      |
| Last print — simple    | thumbnail, name, total; electricity €, filament €, kWh, g, duration, result               |
| Current print — full   | + remaining time, cost per hour, tariff, progress bar, notes, power / cost graph          |
| Last print — full      | + start / end time, per-spool detail, notes                                               |
| Switchable             | one card showing the current print, with a button to flip to the last print and back     |

The totals sensors carry the print thumbnail as `entity_picture`, so Bubble
Card shows it in place of the icon. The switchable card needs an
`input_boolean` helper (see the comments in the file).

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
        {{ state_attr(trigger.entity_id, 'total_cost') | round(2) }} €
        ({{ state_attr(trigger.entity_id, 'energy_kwh') | round(2) }} kWh,
        {{ state_attr(trigger.entity_id, 'filament_weight_g') }} g)
```

## Troubleshooting

| Symptom                    | Check                                                                                                  |
| -------------------------- | ------------------------------------------------------------------------------------------------------ |
| No print detected          | the _print status_ entity must report `prepare` / `running` / `pause` (ha-bambulab `print_status`).    |
| Two prints merged / split  | configure the _job name_ entity.                                                                       |
| Electricity cost `unknown` | the price entity was `unknown`/`unavailable` or in an unsupported unit; see `notes`.                   |
| Filament `unavailable`     | HaspelSync unreachable, wrong printer id, or HaspelSync had no slice info for the print (see its log). |
| Filament `partial`         | a spool has no price in Spoolman, or a slot had no spool assigned in HaspelSync.                       |
| HaspelSync `host_refused`  | use its IP, or add the host name under _Allowed host names_ (HaspelSync ≥ 1.3).                        |

Download the diagnostics (device page → ⋮ → _Download diagnostics_): API key and
URLs are redacted. Debug logs:

```yaml
logger:
  logs:
    custom_components.ha_3d_print_cost: debug
```

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
pytest            # 100 tests: units, tariff change, meter outage, life cycle, restart, filament, flows
ruff check . && ruff format --check .
```

```text
custom_components/ha_3d_print_cost/
├── models/          PrintJob, ElectricityAccumulator, FilamentUsage   ← pure Python
├── units.py         unit normalisation                                ← pure Python
├── tracker.py       print life cycle state machine                    ← pure Python
├── filament.py      HaspelSync → fallback → Spoolman resolution       ← pure Python
├── adapters/        bambu, electricity (HA states), haspelsync, spoolman (HTTP, read-only)
├── coordinator.py   listeners, timer, storage, finalisation, event
├── sensor.py / event.py / config_flow.py / diagnostics.py / storage.py
```

Business logic has no Home Assistant import and is tested without it. External
systems sit behind adapters that are mocked in tests. Money is computed with
`Decimal` and only rounded for display.

## Roadmap

- **v0.1** ✅ electricity cost per print, tariff changes, restart recovery, last print, event
- **v0.2** ✅ HaspelSync + Spoolman, cost per spool, total cost, cancelled prints
- **v0.3** 🚧 scope narrowed to the current and last print, last print thumbnail, example cards

History, G-code estimates and statistics were removed: Home Assistant is not
the right place for them. Use the `print_finished` event to feed another tool.

## License

MIT
