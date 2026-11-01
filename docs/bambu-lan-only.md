# Connect a Bambu Lab printer over LAN (LAN Only + Developer Mode)

> Checked against the official Bambu Lab wiki on 1 November 2026.
> Lines marked **(not confirmed)** come from community reports, not from Bambu Lab.

## Why you need this

FDM Printer Manager talks to Bambu Lab printers **directly on your home network**.
It does not use the Bambu cloud.

For that, the printer must be in **LAN Only mode**, and on current firmware also in **Developer Mode**.
Without Developer Mode, the printer only accepts commands that go through Bambu Connect,
so Home Assistant can't read its status or control it.

You need three things from the printer:

| What | Looks like | Where |
|---|---|---|
| **IP address** | `192.168.1.96` | LAN Only page on the printer screen |
| **Access code** | 8 characters, for example `1a2b3c4d` | LAN Only page on the printer screen |
| **Serial number** | 15 or 18 characters, for example `0309DA561105372` | Printer screen menus, Bambu Studio or Bambu Handy |

> **Tip — "Read from photo".** Take a picture of the LAN Only page with your camera app (no flash,
> lower the exposure if you can). Then, in **Add printer → Bambu Lab (LAN)**, tap **🖼️ Choose photo**
> and pick it. The fields are filled in for you
> and stay editable — check them before you continue: `0`/`O`, `1`/`l`/`I` and `5`/`S` look alike.
> See [Read from photo](#read-from-photo) for where the photo goes.

## Before you start

- The printer and Home Assistant must be on the **same network**. If they are on different VLANs or subnets, see [Troubleshooting](#troubleshooting).
- Give the printer a **fixed IP address** in your router (a DHCP reservation). Otherwise the IP can change after a restart. **(not confirmed — community advice)**
- Update the firmware **before** you switch to LAN Only. See [What you lose](#what-you-lose-in-lan-only-mode).

## Step 1 — Turn on LAN Only mode

The menu depends on the printer.

### A1 and A1 mini (touch screen)

1. Tap **Settings**.
2. Scroll down to **page 3** and tap **LAN Only Mode**.
3. Turn **LAN Only Mode** on. The switch turns **green**.
4. Write down the **IP address** and the **access code** shown on this page.

<!-- screenshot: A1 mini, Settings page 3 with "LAN Only Mode" -->
<!-- screenshot: A1 mini, LAN Only page with the switch on, IP and access code -->

### P1P and P1S (screen with buttons)

1. Open **Settings → WLAN**.
2. Select **LAN Only Mode**. It is **OFF** by default.
3. Select **Yes** to confirm.
4. When it shows **ON**, write down the **access code** shown on this page.

<!-- screenshot: P1S, Settings → WLAN with "LAN Only Mode ON" and the access code -->

### X1 series, H2 series and P2S (large touch screen)

1. On the left side of the screen, open **Settings**, then tap **Settings** again.
2. Tap **LAN Only**.
3. Turn **LAN Only mode** on.
4. Choose whether to turn on **LAN mode liveview** (the camera on your local network).
5. Write down the **IP address** and the **access code** shown on this page.

<!-- screenshot: X1C, Settings → LAN Only page -->

## Step 2 — Turn on Developer Mode

Developer Mode keeps the local **MQTT** channel, the **live stream** and **FTP** open.
This is what lets Home Assistant read the printer status and send commands.

| Printer | How |
|---|---|
| **A1, A1 mini** | On the **LAN Only Mode** page, turn on **Developer Mode**. Read the notice, tick **"I confirm reading and understanding the Important Notice above"** and tap **Enable**. The button turns green. |
| **P1P, P1S** | After LAN Only is **ON**, scroll down on the same page to **Developer Mode**. Read the notice, scroll to the end and select **Enable**. It shows **ON**. |
| **X1, H2, P2S** | On the **LAN Only** page, turn on **Developer Mode**. Read the notice, tick the confirmation and select **Enable Developer Mode**. The button turns green. |

<!-- screenshot: Developer Mode notice with the confirmation box -->

> ⚠️ **Read the notice on the printer.** Bambu Lab says that with Developer Mode
> **you are responsible for the security of your local network**, and that they
> **can't give customer support** for this mode, because the local protocols are not officially supported.
> Keep the printer on a trusted network. Don't expose it to the internet.

## Step 3 — Find the serial number

Use any of these:

- **Printer screen:** the device or firmware information menu. The exact menu differs by model. **(not confirmed — the wiki shows it only in pictures)**
- **Bambu Studio:** **Device → Update**.
- **Bambu Handy:** **Settings → Firmware Version** (only while the printer is still in cloud mode).

## Step 4 — Add the printer in Home Assistant

1. Open **FDM Printers** in the sidebar.
2. Choose **Add printer → Bambu Lab (LAN)**.
3. Enter the **IP address**, the **serial number** and the **access code** — or tap **🖼️ Choose photo** to fill them in from a photo of the printer screen.
4. On the next page, choose the **smart plug** that powers the printer (optional).
5. Finish. The printer card appears on the page.

> The printer must be **switched on** while you add it. The connection is tested before you continue.

## Read from photo

1. Take a picture of the printer's **LAN Only** page with your phone's camera app.
   The screen is lit already: **no flash**, and lower the exposure if you can (about −3 EV read well),
   so the text is not burnt out. Fill the frame and avoid reflections.
2. On the Bambu Lab (LAN) form, tap **🖼️ Choose photo** and pick it from the gallery (or a file on a computer).

How the photo is read:

| You have | What happens | Where the photo goes |
|---|---|---|
| An **AI Task** set up in Home Assistant (Settings → AI Task, for example OpenAI, Google Gemini or Anthropic) | Home Assistant asks it to read the IP, serial number and access code | To your AI provider. The form tells you which one before you take the photo. The temporary copy in Home Assistant is deleted right after. |
| No AI Task | The text is read **on your phone or computer** (Tesseract.js) | Nowhere — only the text found is sent to Home Assistant. The first time downloads the reader (about 10 MB). |

The values are always editable. Check them before you tap **Next**.

## What you lose in LAN Only mode

According to Bambu Lab, in LAN mode:

- You **can't start prints from outside your home network**.
- The **Bambu Handy app** doesn't work.
- **Print History** is not available.
- On the **X1 series**, starting a print from Bambu Studio over LAN needs a **micro SD card** in the printer.

Also reported by users:

- You don't get firmware update notifications, and you can't update over the air.
  To update, switch LAN Only off for a moment, update, and switch it back on. **(not confirmed)**

Bambu Studio and Orca Slicer still work on your local network. In Bambu Studio, the printer shows with a **lock icon**.
Select it and enter the **access code** once.

## How to go back

1. Open the same **LAN Only** page on the printer.
2. Turn **Developer Mode** off, then **LAN Only** off.
3. The printer goes back to cloud mode, and Bambu Handy works again.

Home Assistant then loses the direct connection until you turn both back on.

## Troubleshooting

### "Connection refused" or "wrong access code"

- Check that **LAN Only** and **Developer Mode** are both **on**.
- Check the **access code** on the printer screen. It is 8 characters. It is **not** the 6-digit PIN used to bind the printer to the cloud.
- The access code can change when you turn LAN Only off and on again. **(not confirmed — community reports)**
  If it changed, open the printer in **FDM Printers → Edit** and type the new code. You keep your print history.

### The printer is not found automatically

- Discovery uses **SSDP on UDP ports 1990 and 2021** (broadcast/multicast).
  Some routers, mesh systems and **guest networks** block it. You can always type the IP address by hand.
- If you also use the **Bambu Lab (ha-bambulab)** integration, both listen on the same discovery port.
  If discovery fails, the direct connection still works. Only automatic discovery is affected.
- Check that the printer is switched on and connected to Wi-Fi.

### Printer and Home Assistant on different VLANs or subnets

Discovery doesn't cross subnets. Type the **IP address** by hand, and allow these ports from Home Assistant to the printer:

| Use | Port | Type |
|---|---|---|
| Status and control (MQTT over TLS) | **8883** | TCP |
| Files (FTPS) | 990, 50000–50100 | TCP |
| Camera | 322, 6000 | TCP |
| Discovery | 1990, 2021 | UDP (SSDP) |

These ports come from Bambu Lab's official port list.

## Technical notes

- The local MQTT connection uses **TLS on port 8883**. The user name is `bblp` and the password is the **access code**. **(user name not confirmed by Bambu Lab — widely used by community tools)**
- Since the January 2025 firmware with **Authorization Control**, local control by third-party software needs **Developer Mode**. The standard LAN mode goes through **Bambu Connect** instead.

## Sources

Official (Bambu Lab):

- [How to enable LAN Mode on Bambu Lab printers](https://wiki.bambulab.com/en/knowledge-sharing/enable-lan-mode)
- [How to enable Developer Mode on Bambu Lab printers](https://wiki.bambulab.com/en/knowledge-sharing/enable-developer-mode)
- [Printer network ports](https://wiki.bambulab.com/en/general/printer-network-ports)
- [How to find and register the serial number](https://wiki.bambulab.com/en/general/find-sn)
- [Updates and Third-Party Integration with Bambu Connect (blog, January 2025)](https://blog.bambulab.com/updates-and-third-party-integration-with-bambu-connect/)
- [Firmware Update Introducing New Authorization Control System (blog)](https://blog.bambulab.com/firmware-update-introducing-new-authorization-control-system-2/)

Community (used only for lines marked **not confirmed**):

- [A1 mini: Where do I find the access code?](https://forum.bambulab.com/t/a1-mini-where-do-i-find-the-access-code/56991)
- [Access Code always needs to be re-entered](https://forum.bambulab.com/t/access-code-always-needs-to-be-re-entered/82201)
- [How to get into LAN mode and use it](https://forum.bambulab.com/t/how-to-get-into-lan-mode-and-use-it/142072)
- [FTP and MQTT password after update](https://forum.bambulab.com/t/ftp-and-mqtt-password-after-update/5200)
