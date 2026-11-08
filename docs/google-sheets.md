# Log every print to a Google Sheet

**English** | [Português](google-sheets.pt.md)

> Checked against the [Home Assistant Google Sheets documentation](https://www.home-assistant.io/integrations/google_sheets/)
> on 1 November 2026. Google renames the menus of its console from time to time: if a name differs, look for the closest one.

## In short

| Step | Where | How long | How often |
|---|---|---|---|
| 1. Google Cloud credentials | Google Cloud console | about 10 minutes | **once** |
| 2. Connect Google Sheets | FDM Printers → **Google Sheets** → **Connect Google Sheets** | 1 minute | once |
| 3. Start logging | FDM Printers → **Google Sheets** → **Start logging** | 1 click | once |

After that, every print that ends (finished, failed, cancelled) adds one row to the sheet.

## Why step 1 is needed

FDM Printer Manager does not talk to Google itself. It uses Home Assistant's own
**Google Sheets** integration. Google only lets a program write to your Drive with
credentials that **you** create in your own Google Cloud project. It is free.
This is the only step that cannot be done from Home Assistant.

## Step 1 — Google Cloud credentials (once)

### 1.1 Create a project

1. Open the [Google Cloud console](https://console.cloud.google.com/) and sign in.
2. At the top, open the project list → **New project**.
3. Name it, e.g. `Home Assistant`, and select **Create**.
4. Check that the new project is selected at the top.

### 1.2 Turn on two APIs

Open each link, check the project at the top, and select **Enable**:

| API | Link |
|---|---|
| Google Sheets API | [sheets.googleapis.com](https://console.cloud.google.com/apis/library/sheets.googleapis.com) |
| Google Drive API | [drive.googleapis.com](https://console.cloud.google.com/apis/library/drive.googleapis.com) |

### 1.3 Consent screen (Google Auth Platform)

1. Open [Google Auth Platform → Branding](https://console.cloud.google.com/auth/branding).
   If it says it is not configured, select **Get started**.
2. **App name**: e.g. `Home Assistant`. **User support email**: your email.
3. **Audience**: **External**.
4. **Contact information**: your email. Accept the data policy and select **Create**.
5. Open **Audience** and choose one:

| Choice | What happens |
|---|---|
| **Publish app** (recommended) | The login does not expire. Google shows a "not verified" warning once: that is normal for your own app. |
| Keep it in **Testing** | Add your own Google account under **Test users**. The login **expires every 7 days**: you must sign in again each week. |

### 1.4 Create the client

1. Open **Clients** → **Create client**.
2. **Application type**: **Web application**. Name: anything.
3. **Authorized redirect URIs** → **Add URI**:

   ```
   https://my.home-assistant.io/redirect/oauth
   ```

   Type it exactly like this. It is not a placeholder.
4. Select **Create**.
5. Copy the **Client ID** and the **Client secret** now. The secret is not shown again.

## Step 2 — Connect Google Sheets (from the FDM Printers page)

1. Open **FDM Printers** in the sidebar.
2. Select **Google Sheets** (top right, next to "Add printer").
3. Select **Connect Google Sheets**. Home Assistant opens its own "add Google Sheets" window.
4. The first time, it asks for **Application credentials**:
   - **Name**: e.g. `Google`
   - **OAuth Client ID** and **OAuth Client Secret**: the two values from step 1.4.
5. Sign in with your Google account. If Google says the app is not verified,
   select **Continue** (it is your own app).
6. Check the address on the "Link account to Home Assistant?" page and select **Link account**.
7. Home Assistant says **Success!**. It created a spreadsheet called **Home Assistant** in your Google Drive.
   You can rename it in Google Drive.

> Already connected Google Sheets before? Skip step 2: the FDM button lists your existing
> Google Sheets entries.

## Step 3 — Start logging (one click)

1. Go back to **FDM Printers** → **Google Sheets**.
2. Pick the spreadsheet (the list shows each Google Sheets entry by its name).
3. Keep the tab name **Prints**, or type another one.
4. Select **Start logging**.

What the button does:

| Action | Detail |
|---|---|
| Creates the tab | Only if it is missing. A new tab gets the header row, frozen at the top. An existing tab is not changed. |
| Turns the log on | For **all** printers (in each printer's options: **Edit → When a print ends**). Other options are kept. |
| Shows the result | "✓ Logging to *spreadsheet* › Prints", with **Open the sheet ↗**. |

To stop: **Google Sheets** → **Turn off for all printers**.
To log one printer to another tab: **Edit → When a print ends** on that printer (still works as before).

## The columns

| Column | Content |
|---|---|
| `created` | When the row was written (added by Google Sheets) |
| `printer` | Printer name |
| `file` | File name |
| `result` | `success`, `failed`, `cancelled` or `unknown` |
| `result_by` | Who decided the result: `printer`, `watchdog` or `user` |
| `started`, `finished` | Local time, `YYYY-MM-DD HH:MM:SS` |
| `duration_min` | Minutes |
| `energy_kwh` | Energy of the print |
| `electricity_cost`, `currency` | Cost of the electricity |
| `notes` | Notes (e.g. a correction) |
| `job_id` | The print's ID |
| `filament_mm` | Filament really used, in mm (since 0.14.0; also for cancelled and failed prints). Empty when the printer does not report it (Bambu Lab) |
| `filament_g` | The same in grams: `filament_mm` x the slicer's weight per mm of the file. Empty without that weight |
| `filament_type` | Filament type written by the slicer (e.g. `ASA`) |
| `filament_cost` | `filament_g` x the price per kg: the printer's **Filament price per kg** (Edit → Advanced), else (0.15.0) the generic price of the material (FDM Printers → Configure). Empty without a price or a known material |
| `filament_m` | The same length in metres (since 0.15.0) |
| `filament_price_per_kg` | The price per kg used for `filament_cost` |
| `filament_price_source` | `printer` (the printer's own price) or `generic` (the generic price of the material) |
| `print_cost` | `electricity_cost` + `filament_cost` (only the known part when one is empty) |

## Limits

| Limit | What it means |
|---|---|
| **Only the spreadsheet the integration created** | Google Sheets in Home Assistant can only write to the spreadsheet it created when you connected it. It cannot write to a spreadsheet you made yourself. Want a separate spreadsheet? Use **Add another spreadsheet**: it adds a second Google Sheets entry with a new spreadsheet. |
| **One row per print** | If you later mark a print ("Wrong? Mark as failed / Mark as OK" on the card) and the result changes, the print's row is **changed in place**: `result` and `result_by` = `user`, found by `job_id`. The same when its material is chosen by hand (0.15.0): the material and cost cells change (a row still waiting to be sent is changed before it goes). Only when that row cannot be found or changed is a **correction row** added, with the same `job_id` and a note; then keep the last row of each `job_id` when you count. |
| **Columns by header name** | Rows are matched to the header row by name. A missing column is added at the end of the header. |
| **No network** | The row waits (up to 200 rows) and is sent before the next one, at the next print or when Home Assistant starts. You get one notification. |

## Who can set it up

Only **administrators**. Setting up the log changes the options of every printer and creates a tab in Google, so the `fdm_printer_manager.sheets_setup` action, the "Google Sheets" button and marking a print (`fdm_printer_manager.mark_print`, which changes rows in the sheet) are refused for other users. Automations and scripts still work: they run without a user.

Home Assistant has no per-action permissions. To let someone else set up the log, make that person an administrator (Settings → People → the person → **Administrator**).

## Reusing an older log (Portuguese headers)

Had a tab written by an older automation, with Portuguese headers? Keep it: rename the
headers in row 1 and choose that tab's name in step 3. Existing rows stay.

| Old header | New header |
|---|---|
| `impressora` | `printer` |
| `ficheiro` | `file` |
| `inicio` | `started` |
| `fim` | `finished` |
| `duracao_min` | `duration_min` |
| `energia_kwh` | `energy_kwh` |
| `resultado` | `result` |
| `deteccao` | `result_by` |

The tab must be in the spreadsheet created by the Google Sheets integration (see Limits).

## Problems

| What you see | What to do |
|---|---|
| "Google Sheets is not set up" | Do step 2. |
| "is not working … sign in with Google again" | Settings → Devices & services → Google Sheets → reconfigure / sign in again. In **Testing** mode this happens every 7 days: publish the app (step 1.3). |
| "Cannot open the spreadsheet … or add the tab" | The spreadsheet was deleted, or the APIs are off (step 1.2). |
| Google says "Access blocked" or "redirect_uri_mismatch" | The redirect URI in step 1.4 is not exactly `https://my.home-assistant.io/redirect/oauth`. |
| Wrong client ID or secret | Settings → Devices & services → ⋮ → **Application credentials**: delete them and do step 2 again. |
