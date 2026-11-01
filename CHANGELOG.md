# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [0.9.0]

### Added

- **Mark a print as a success or a failure** by hand: **✓ OK** / **✗ Failed** on the printer
  card (last print) and on every row of "Recent prints". The printer's own verdict is not always
  right (a "finished" print may have come off the plate). New action
  `fdm_printer_manager.mark_print` (`entry_id`, `result`, optional `job_id`; empty = the last
  print) and event `fdm_printer_manager_print_marked`. A marked print shows ✎ in the history and
  `result_by: user` in the last print attributes.
- **History numbers** above "Recent prints": number of prints, success rate (successes against
  failures; cancelled prints don't count) and what the failed and cancelled prints cost.
  The `fdm_printer_manager/history` websocket command returns them as `stats`.
- **Watch prints** (on by default; *Edit → Watch prints* to change or turn off):
  - **Printer not answering** in the middle of a print for 2 min → a warning (and the card says
    so); when it answers again: "resumed" or "back, not printing".
  - **Interrupted print**: a print still open while the printer answers but does not print
    (Klipper comes back in *standby* after a power cut, or a state got stuck) is **closed as
    failed** after 10 min, with a note. A Bambu Lab printer that comes back *idle* after being
    down ends the print as failed right away. Nothing is ever resumed automatically.
  - **No progress** for 60 min while printing → a warning (0 = never).
  - Each alert is a Home Assistant notification, the event `fdm_printer_manager_alert`
    (`kind`: `down`, `back`, `resumed`, `interrupted`, `stalled`) and, optionally, your notify
    actions (e.g. `notify.mobile_app_phone`).
  - The printer status sensor has an `alert` attribute (`down` / `stalled`).
- **New access code (Bambu Lab LAN)**: when the printer refuses its access code (it changes
  when LAN Only mode is turned off and on), Home Assistant asks for the new one (re-authentication,
  also shown at the top of the FDM Printers page, with **📷 Take photo**). The history is kept.
- **When a print ends** (*Edit → When a print ends*, optional): add **"Remove the print from
  &lt;printer&gt;"** to a to-do list (with the file name and the result), and / or start a
  **script** with the print summary as variables.
- **Better printer names**: a new Klipper printer is no longer named after a hostname shared by
  every machine of a kind: `cosmos` becomes **Elegoo Centauri Carbon**, `mainsailos`,
  `fluiddpi`, `raspberrypi`… become **Klipper &lt;IP&gt;** (a typed name always wins).
  The device now follows the entry name you see (and can rename) in Home Assistant. Entity IDs
  of printers already added are **not** changed; Home Assistant offers to rename them when you
  rename the device.
- Last print attributes and the history: `result_by` (`user`, `watchdog`, or empty for the
  printer's own verdict).

## [0.8.0]

### Added

- **Auto-off after a print**: when a print ends (finished, cancelled or failed), the printer is
  turned off safely after **15 minutes** without a new print (Configure → Advanced, 0 = never).
  It waits for the nozzle to drop below 80 °C, shuts the printer's computer down and cuts the
  plug only once that computer stops answering. A new print, the plug turned off by hand, or
  tapping the power button on the card cancels it. The card shows "Auto off at HH:MM".
- **SSH shutdown fallback** for Klipper printers whose Moonraker cannot shut down. The
  **Elegoo Centauri Carbon** ("cosmos" firmware) answers `POST /machine/shutdown` with HTTP 500
  "Error running shell command: 'sudo systemctl poweroff'" and stays on; it is recognised by its
  host name or that error and shut down with one `poweroff` over SSH as `root` (no password, as
  the printer ships), with nothing to set up. Other printers can be given an SSH login in the
  options (user, optional password or key, command). Only that one command is ever run.
- **Take photo opens the camera inside the page** (getUserMedia), because the Home Assistant
  Android app ignores `capture="environment"`. Set up for photographing a lit screen: flash off,
  exposure towards −3 EV, focus and metering on the centre, high resolution (each only when the
  camera supports it). Without a camera API (Home Assistant over plain `http://`) or when access
  is refused, it falls back to the file picker with a clear message.

### Changed

- **Safe power** cuts the plug only once the printer's computer stops answering a ping (or a
  TCP check): 3 checks in a row 5 s apart, then 10 s more. Moonraker going quiet is not enough
  any more. Still answering after 5 minutes, or no way to shut it down, the plug is **left on**
  and a notification says why (before: cut after 3 minutes regardless).
- New `safe_power` attributes: `auto_off_at`, `auto_off_minutes`, `last_result`
  (`power_off`, `aborted`, `hot`, `still_on`, `no_shutdown`) and `ssh`; new phase `waiting`.
- The SSH password and key are redacted from diagnostics.
- `asyncssh` is now a requirement (only imported when an SSH shutdown is needed).

## [0.7.0]

### Added

- **Read from photo (Bambu Lab LAN)**: on the Bambu Lab (LAN) form, **📷 Take photo** opens the
  camera on a phone or tablet (Home Assistant app or browser; a file picker on a computer) and
  **Choose image** picks a photo already taken. The IP address, serial number and access code
  are filled in from a photo of the printer's LAN Only page and stay editable; the filled fields
  are highlighted (the access code is shown), with a warning when a value doesn't look right.
  The photo is made smaller first (at most 1600 px).
  - With an **AI Task** set up in Home Assistant, it reads the photo (`ai_task.generate_data`
    with the photo attached, structured answer). The form says which provider gets the photo;
    the temporary copy in the media folder is deleted right after, also on errors.
  - Without one, the text is read **in the browser** with Tesseract.js (downloaded on first use,
    about 10 MB); the photo never leaves the device, only the text is sent to Home Assistant.
  - In the Android app, a clear message explains how to allow the camera when no photo comes back.
- **Guide**: [Connect a Bambu Lab printer over LAN](docs/bambu-lan-only.md) (LAN Only and
  Developer Mode per model, what you lose, troubleshooting), linked from the form and the README.
- New websocket commands `fdm_printer_manager/photo_ai`, `read_photo` and `parse_photo_text`.

## [0.6.0]

### Added

- **Printer camera on the card**, found by itself, nothing to configure. While the printer
  prints the card shows the live picture (● LIVE); otherwise a still image refreshed every
  minute. Click it to open it full size. Printers without a camera look as before.
- **Klipper**: the first enabled webcam Moonraker lists (crowsnest, mjpg-streamer…) becomes
  `camera.<printer>_camera`, usable on any dashboard. Relative webcam URLs are resolved
  against the printer's web interface and redirects are followed. It is looked for when the
  integration starts, when the printer comes back online and every 30 minutes: a camera
  installed later shows up by itself, a removed one goes unavailable. MJPEG streams are
  relayed as they are; other stream types (WebRTC, HLS) show refreshed snapshots.
- **Bambu Lab through the Bambu Lab integration**: its own camera (a camera entity on X1 / H2,
  the camera image entity on A1 / P1) is shown on the card. No second entity is created.
- The live stream stops while the page or the browser tab is hidden.

### Not yet

- **Bambu Lab (LAN) direct**: no camera yet. The video protocols over LAN (RTSPS on port 322
  for X1 / H2, a JPEG stream on port 6000 for A1 / P1) are not documented officially enough
  to be supported safely; planned for a later version.

### Development

- Test dependencies: `PyTurboJPEG` (imported by Home Assistant's camera component).

## [0.5.1]

### Safety

- **Safe power waits for the nozzle to cool**: turning a printer off with the nozzle at 80 °C
  or more no longer cuts the plug right away. The hotend fan keeps running until the nozzle
  is below 80 °C (phase `cooling`, shown on the card as "Cooling nozzle — 142° → 80°"),
  then Klipper shuts its computer down and the plug is cut as before. A hot hotend without
  its fan can soften the filament in the heat break and clog the nozzle.
- If the nozzle is still hot after 30 minutes (a heater may be on), the plug is left on
  and a notification says so.
- No temperature reading (printer off or unreachable): the plug is cut right away, as before.
- Turning the plug back on, or a new print, cancels the wait.
- Bambu Lab printers through the Bambu Lab integration now also report the nozzle temperature.

## [0.5.0]

- **Klipper printers found without zeroconf**: the integration searches Home Assistant's
  own network for Moonraker (ports 7125 and 80, at most 254 addresses, a few at a time)
  when it starts, every 30 minutes, when you open **Add printer** and with the new
  **Search again** button in "Found on the network". Printers already added are not offered again.
- **Klipper with just the IP address** now finds Moonraker on port 80 too
  (for example the Elegoo Centauri Carbon), not only on 7125.

## [0.4.0] — first numbered release

First release published on GitHub, so HACS shows a real version number.
It contains everything from the 0.3.0 development versions below:

- **FDM Printer Manager**: new name, English only, energy and electricity cost only (no filament).
- **FDM Printers page** in the Home Assistant sidebar: printer cards, Add printer / Edit,
  Printing now, Recent prints, Found on the network. The first "Add integration" creates it.
- **Klipper direct** (Moonraker, just the IP address) with network discovery.
- **Bambu Lab direct over LAN** (IP, serial, access code) with network discovery;
  Bambu through the Bambu Lab integration is still available.
- Per printer: **safe power** switch, **Pause / Resume / Cancel** buttons, energy and cost
  counters (total and today), print history, typed price per kWh.
- Passes the Hassfest and HACS checks: no HTML in the translations, integration
  categories in the manifest.

From now on versions are numbered 0.x.y and published as GitHub releases.

## [0.3.0-alpha.4]

- **Bambu Lab direct (LAN)**: a new printer type that talks to the printer itself
  (IP address, serial number and access code; MQTT over TLS on port 8883). No other
  integration needed. State, file, progress, time left, nozzle, Pause / Resume / Cancel.
  The printer must have LAN only mode on (and Developer mode on recent firmware).
- **Found on the network**: Klipper printers announced by Moonraker (zeroconf) and Bambu
  Lab printers announcing themselves on the LAN (SSDP) appear in Home Assistant's
  *Discovered* card and in the FDM Printers page. A Bambu printer only needs its access
  code; a Klipper printer only a click.
- The first **Add integration** creates the FDM Printers page directly (no menu); the
  next ones ask for the printer type.
- "Bambu Lab (via Bambu Lab integration)" stays available for ha-bambulab users.
- Diagnostics redact the access code.

## [0.3.0-alpha.3]

- **Safe power** switch per printer (`switch.<printer>_safe_power`), for any dashboard:
  never turns off while printing or paused; Klipper shuts its computer down first and
  the plug is cut once it is down (at most 3 minutes).
- **Pause / Resume / Cancel** buttons per printer (Klipper via Moonraker, Bambu via the
  Bambu Lab integration's buttons), available only when they apply.
- **The printer's own counters**, created automatically when the plug measures energy:
  energy total, cost total, energy today, cost today (Energy dashboard ready).
- **Price per kWh typed in** when adding a printer (no price entity needed); a price
  entity for changing tariffs is still possible under Advanced.
- **Print history** of every finished print (last 500 per printer), shown in the panel.
- The power integration method is no longer asked: the plug's energy meter is used when
  there is one, otherwise power is integrated automatically.
- Panel: one Add printer button, idle cards show today's energy / cost and the total
  cost, Pause / Resume / Cancel with confirmation, plug toggle with confirmation and
  shutdown progress, "Printing now" and "Recent prints" lists, searchable lists (any
  words, accents ignored) for plug, sensors and price, mouse wheel never changes a value.

## [0.3.0-alpha.2] — FDM Printers panel (alpha)

- New **FDM Printers** page in the Home Assistant sidebar (admins only): a card per
  printer (state, file, progress, time left, nozzle, energy, cost, plug, link to the
  web interface) and an **Add printer** / **Edit** wizard in the app's own layout.
  The wizard drives the integration's normal config and options flows.
- "Add integration" offers "Only add the FDM Printers panel to the sidebar", so the
  page exists before the first printer.
- The plug step in the panel finds the plug's power and energy sensors on its device
  and reuses the electricity price of the other printers.
- New sensors: printer state (with the panel's settings as attributes), print
  progress, print file, print time left, nozzle temperature.

## [0.3.0-alpha.1] — FDM Printer Manager (alpha)

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
- **Filament tracking removed** (HaspelSync, Spoolman, filament weight and cost):
  the integration tracks energy and electricity cost only. Sensors renamed to
  `current_print_cost` / `last_print_cost`; the separate electricity / filament /
  total cost and weight sensors are gone.

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
