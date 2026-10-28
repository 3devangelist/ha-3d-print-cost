# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [0.3.0] — FDM Printer Manager

- Renamed to **FDM Printer Manager** (domain `fdm_printer_manager`), maintained by @3devangelist.
- "Add integration" starts with a choice of printer type.
- **Klipper printers connect directly** to Moonraker (IP address, optional API key);
  no other integration needed. Polled every 5 s; slicer metadata gives the remaining
  time and the planned filament weight.
- Device page: printer name, manufacturer, and a **Visit** link to Mainsail / Fluidd.
- Smart plug: new plug switch field; power and energy are optional (no cost without them);
  the electricity price is only required with a meter.
- **Configure** edits a Klipper printer's connection (IP, port, API key, web interface, name)
  without deleting it.
- English only (French translation and README removed).

## [0.2.x] — upstream (ZoziLaMalice)

### Changed
- Scope narrowed to the print in progress and the last print.
- Example cards (`examples/dashboard_cards.yaml`) replace the dashboard view and package.

### Added
- `image.<printer>_last_print_image`: thumbnail of the last print, kept across restarts and shown as the picture of `last_print_total_cost`.
- `switch.<printer>_show_last_print`: current / last print toggle for the switchable example card, so it no longer needs an `input_boolean` helper. Turns itself off when a new print starts.

### Removed
- Print history (`print_history` sensor, `get_history` action, history size option).
- G-code estimate (`estimate_cost` action, file select, buttons, `gcode_estimate` sensor, G-code folder option).

## [0.2.0] - unreleased

First public version, covering the v0.1 and v0.2 milestones.

### Electricity (v0.1)
- UI config flow: Bambu Lab printer, auto-discovered ha-bambulab entities, smart plug, price entity.
- Print start / end detection from ha-bambulab `print_status`, protected against duplicate states.
- Energy from a cumulative meter (preferred) or power integration (`left` / `trapezoidal`).
- Cost accumulated interval by interval, so tariff changes during a print are taken into account.
- Unit normalisation: W/kW, Wh/kWh/MWh, EUR/kWh, c€/kWh, EUR/MWh.
- Persistence with HA `Store`; a print in progress survives restarts and reloads.
- `current_print_*` and `last_print_*` sensors, `event.<printer>_print_finished`, bus event.

### Filament (v0.2)
- Read-only HaspelSync adapter: real consumption per spool from `lastPrintSummary`, cancelled prints included.
- Read-only Spoolman adapter: price per gram from spool price, filament price or `price_per_kg` / `price_per_g` extra fields.
- Configurable fallback on ha-bambulab sliced weights; unknown is never reported as zero.
- Options flow, history of the last N prints, diagnostics with redaction.
