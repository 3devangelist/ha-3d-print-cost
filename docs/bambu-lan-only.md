# Connect a Bambu Lab printer over LAN (LAN Only + Developer Mode)

**English** | [Português](bambu-lan-only.pt.md)

> Checked against the official Bambu Lab wiki on 1 November 2026.
> Lines marked **(not confirmed)** come from community reports, not from Bambu Lab.

## In short

1. Turn on **LAN Only** on the printer screen.
2. Turn on **Developer Mode** on the same page.
3. Take a photo of that page (no flash).
4. In Home Assistant: **FDM Printers → Add printer → Bambu Lab (LAN) → 🖼️ Choose photo**.
5. Check the three values and finish.

## Why you need this

FDM Printer Manager talks to Bambu Lab printers **directly on your home network**.
It does not use the Bambu cloud.

| Mode | What it does | Needed? |
|---|---|---|
| **LAN Only** | The printer stops using the cloud and talks only on your network | Yes |
| **Developer Mode** | Opens local control (MQTT), the camera and file transfer to other software | Yes, on current firmware |

Since the January 2025 firmware, plain LAN mode only accepts commands through Bambu Connect.
Without **Developer Mode**, Home Assistant can't read the status or control the printer.

## What you need from the printer

| What | Looks like | Where |
|---|---|---|
| **IP address** | `192.168.1.96` | LAN Only page on the printer screen |
| **Access code** | 8 characters, for example `1a2b3c4d` | LAN Only page on the printer screen |
| **Serial number** | 15 or 16 characters, for example `0309DA561105372` | Device info on the screen, Bambu Studio or Bambu Handy |

The serial number is **not** the printer name like `3DP-000-000`.

## Before you start

- Printer and Home Assistant on the **same network**. Different VLANs: see [Network and ports](#network-and-ports).
- Give the printer a **fixed IP** in your router (DHCP reservation). Otherwise the IP can change. **(not confirmed — community advice)**
- **Update the firmware first.** In LAN Only you lose over-the-air updates. See [What you lose](#what-you-lose-in-lan-only).

## Step 1 — Turn on LAN Only

| Printer | Menu |
|---|---|
| **A1, A1 mini** (touch screen) | **Settings** → page 3 → **LAN Only Mode** → switch on (green) |
| **P1P, P1S** (buttons) | **Settings → WLAN → LAN Only Mode** → **Yes** → shows **ON** |
| **X1C, X1E, H2D, H2S, P2S** (large touch screen) | Left side **Settings → Settings → LAN Only** → switch on |

On the X1 and H2 series you can also turn on **LAN mode liveview** (the camera on your network).

<!-- screenshot: A1 mini, LAN Only page with the switch on, IP and access code -->

## Step 2 — Turn on Developer Mode

| Printer | How |
|---|---|
| **A1, A1 mini** | On the **LAN Only Mode** page, turn on **Developer Mode**. Tick the confirmation and tap **Enable**. |
| **P1P, P1S** | With LAN Only **ON**, scroll down to **Developer Mode**. Read to the end and select **Enable**. |
| **X1, H2, P2S** | On the **LAN Only** page, turn on **Developer Mode**. Tick the confirmation and select **Enable Developer Mode**. |

> ⚠️ **Read the notice on the printer.** In Developer Mode **you are responsible for the security of your network**,
> and Bambu Lab gives **no support** for it. Keep the printer on a trusted network. Never expose it to the internet.

## Step 3 — Take the photo

The LAN Only page shows the **IP address** and the **access code**. One photo is enough.

| Do | Don't |
|---|---|
| Use the phone's normal **camera app** | Use the flash |
| **Lower the exposure** (−3 EV worked well) | Leave the screen white and burnt out |
| Fill the frame with the **whole screen**, straight on | Shoot at a steep angle |
| Hold still, let it focus on the text | Catch lamp reflections on the glass |

## Step 4 — Add the printer in Home Assistant

1. Open **FDM Printers** in the sidebar.
2. **Add printer → Bambu Lab (LAN)**.
3. Tap **🖼️ Choose photo** and pick the photo. The fields fill in.
   Or type the **IP address**, **serial number** and **access code** by hand.
4. **Check the values.** `0`/`O`, `1`/`l`/`I` and `5`/`S` look alike.
5. Choose the **smart plug** that powers the printer (optional).
6. Finish. The printer card appears.

The printer must be **on** while you add it. The connection is tested before you continue.

### How the photo is read

| You have | What happens | Where the photo goes |
|---|---|---|
| An **AI Task** in Home Assistant (OpenAI, Gemini, Anthropic…) | It reads the three values | To that AI provider. The form tells you which one. The copy in Home Assistant is deleted right after. |
| No AI Task | The text is read **on your phone or computer** (Tesseract.js) | Nowhere. Only the text is sent. The first time downloads about 10 MB. |

## When the access code changes

The code can change when you:

- turn LAN Only **off and on** again **(not confirmed — community reports)**;
- reset the printer or the network settings;
- tap **refresh** next to the code (A1 / A1 mini).

What you see: the printer becomes **unavailable** and Home Assistant shows **"Access code rejected"**.

What to do:

1. Take a new photo of the LAN Only page.
2. Home Assistant asks for the new code (a **Re-authenticate** notice). It also appears on the **FDM Printers** page.
3. Tap **🖼️ Choose photo**, check the code, and save.

You keep the printer, its history and its entity IDs.

> If you also use the **Bambu Lab (ha-bambulab)** integration, it keeps its own copy of the code.
> Update it there too: **Settings → Devices & services → Bambu Lab → Configure**.

## What you lose in LAN Only

According to Bambu Lab:

- **No remote printing** from outside your home.
- **Bambu Handy** doesn't work.
- **Print History** is not available.
- **X1 series:** printing from Bambu Studio over LAN needs a **micro SD card**.

Also reported by users **(not confirmed)**:

- No firmware update notices and no over-the-air updates.
  To update: LAN Only off → update → LAN Only on (the code may change).

**Bambu Studio and Orca Slicer** still work on your network.
The printer shows with a **lock icon**. Select it and enter the **access code** once.

## Network and ports

Automatic discovery doesn't cross VLANs or subnets. Type the **IP address** by hand,
and allow these ports from Home Assistant to the printer:

| Use | Port | Type |
|---|---|---|
| Status and control (MQTT over TLS) | **8883** | TCP |
| Files (FTPS) | 990, 50000–50100 | TCP |
| Camera — A1, A1 mini, P1 (JPEG stream) | 6000 | TCP |
| Camera — X1, H2, P2S (RTSP, needs **LAN mode liveview**) | 322 | TCP |
| Discovery (SSDP) | 1990, 2021 | UDP |

These ports come from Bambu Lab's official port list.

## Common problems

| Problem | What to check |
|---|---|
| **"Access code rejected"** | Code changed? See [When the access code changes](#when-the-access-code-changes). It is 8 characters, **not** the 6-digit cloud binding PIN. |
| **Printer not found automatically** | Discovery uses UDP 1990/2021. Mesh systems and guest networks often block it. Type the IP by hand. |
| **Two integrations, discovery fails** | FDM and ha-bambulab listen on the same discovery port. Only discovery is affected; the direct connection works. |
| **Bambu Handy shows the printer offline** | Normal in LAN Only. Handy needs the cloud. |
| **Bambu Studio can't see the printer** | Same network? Select the printer with the lock icon and enter the access code. |
| **Connected, but no status** | **Developer Mode** is off. Turn it on (Step 2). |
| **No camera on X1 / H2** | Turn on **LAN mode liveview** on the LAN Only page. |
| **Cover image or print weight missing (newer H2 / P2S firmware)** | Turn on **Store sent files on external storage** in the printer's print options and insert a USB drive or SD card. **(not confirmed for FDM — reported by ha-bambulab)** |

For a bug report, attach the **diagnostics** file (device page → **Download diagnostics**)
and debug logs (`custom_components.fdm_printer_manager: debug`).

## Security

- The **access code is a password**. Anyone on your network with it can control the printer.
- **Don't share photos** of the LAN Only page. Crop or blur the code first.
- Original phone photos can also contain your **GPS location**.
- Keep the printer off guest networks and never forward its ports to the internet.

## How to go back to cloud mode

1. Open the **LAN Only** page on the printer.
2. Turn **Developer Mode** off, then **LAN Only** off.
3. Bambu Handy works again. Home Assistant loses the direct connection until you turn both back on.

## Technical notes

- MQTT uses **TLS on port 8883**. User name `bblp`, password = **access code**. **(user name not confirmed by Bambu Lab — used by community tools)**

## Sources

Official (Bambu Lab):

- [How to enable LAN Mode](https://wiki.bambulab.com/en/knowledge-sharing/enable-lan-mode)
- [How to enable Developer Mode](https://wiki.bambulab.com/en/knowledge-sharing/enable-developer-mode)
- [Printer network ports](https://wiki.bambulab.com/en/general/printer-network-ports)
- [How to find the serial number](https://wiki.bambulab.com/en/general/find-sn)
- [Updates and third-party integration with Bambu Connect (January 2025)](https://blog.bambulab.com/updates-and-third-party-integration-with-bambu-connect/)

Community (only for lines marked **not confirmed**):

- [ha-bambulab setup guide](https://github.com/greghesp/ha-bambulab/blob/main/docs/setup.mdx)
- [A1 mini: where do I find the access code?](https://forum.bambulab.com/t/a1-mini-where-do-i-find-the-access-code/56991)
- [Access code always needs to be re-entered](https://forum.bambulab.com/t/access-code-always-needs-to-be-re-entered/82201)
- [How to get into LAN mode and use it](https://forum.bambulab.com/t/how-to-get-into-lan-mode-and-use-it/142072)
