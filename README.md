# textpad_local

Minimal shared text pad for the local LAN. Opens in any browser, auto-saves as you type, syncs instantly across all open browser instances, and keeps a few named tabs.

Git tracks only source and operational documentation. Live tabs, deleted-tab
archives, local configuration, and generated SMB mirrors are intentionally
outside Git.

## Features

- Auto-saves 300ms after you stop typing
- Keeps an immediate browser-side draft until the server confirms the save
- Writes server files with atomic replace plus fsync, so saved text survives service crashes and reboots
- Named tabs at the top of the page
- Create, rename, and delete tabs
- Tabs are shown with simple left-to-right numbers, starting at 1
- Blank tab names become `Untitled`
- Deleted tab files are archived on disk under `deleted/`
- Real-time sync across all connected clients on the same tab via SSE (no refresh needed)
- Line numbers and cursor position indicator
- IDE-style `Tab` and `Shift+Tab` indentation for the cursor or all selected lines
- `add row below` button for appending a line without scrolling through long text
- Opens every page and tab at the top instead of restoring an arbitrary scroll position
- Clear all button
- Dark theme, monospace font
- One-way plain-text tab mirror for the shared SMB folder
- No dependencies beyond Python stdlib

## Setup

### 1. Clone and configure

```bash
git clone https://github.com/Grigory-T/textpad_local.git textpad_local
cd textpad_local
cp config.example.py config.py
```

Edit `config.py`:

```python
# Bind to the required private interface, or use 0.0.0.0 with a LAN-only firewall.
HOST = '0.0.0.0'

# Use an unprivileged port by default; port 80 requires CAP_NET_BIND_SERVICE.
PORT = 8080

# Optional plain-text mirror directory.
MIRROR_DIR = './mirror'
```

### 2. Create a systemd service

This repo includes `textpad_local.service`. Install it:

```bash
sudo cp textpad_local.service /etc/systemd/system/textpad_local.service
sudo systemctl daemon-reload
sudo systemctl enable --now textpad_local.service
```

Notes:

- The example unit assumes a dedicated `textpad-local` user and `/opt/textpad-local` checkout.
- Change the unit paths and user for your installation.
- `CAP_NET_BIND_SERVICE` is only needed when using a port below 1024.
- Restrict access to a trusted private network because the application has no authentication.

### 3. (Optional) Set up a hostname

To access via `http://pad` instead of an IP address, add a DNS entry pointing `pad` to your server's LAN IP. Works with any local DNS server (e.g. Technitium, Pi-hole, dnsmasq).

## How to use

Find the machine's LAN IP (example):

```bash
ip -4 addr show | grep -E 'inet 192\\.168\\.'
```

Open `http://<private-host-address>:<port>/`.

- **Type** — content saves automatically after a short pause
- **Indent** — `Tab` indents the current selection; `Shift+Tab` removes one tab or up to four leading spaces from each selected line
- **Add row below** — appends a new last line, moves the caret there, and returns horizontal scroll to the left
- **Tabs** — numbered tab names are at the top; use `add tab` to create a new one after the active tab
- **Rename** — edit the current tab name and press `rename tab`
- **Delete tab** — deletes the current tab; the last remaining tab cannot be deleted
- **Crash recovery** — if the browser closes before a save finishes, the next page load restores the local draft and saves it again
- **Sync** — any other open instance of the same tab updates instantly without refreshing
- **Clear all** — button in the toolbar wipes the current tab and syncs to all clients on that tab
- **Mobile** — use browser menu → "Add to Home Screen" for one-tap access

## Service management

```bash
sudo systemctl status textpad_local.service
sudo systemctl restart textpad_local.service
sudo systemctl stop textpad_local.service
sudo journalctl -u textpad_local.service -f
```

## Files

- `pad.py`             — server
- `config.example.py`  — config template
- `config.py`          — your local config (gitignored)
- `pad.txt`            — legacy single-pad state, copied into the first `Main` tab on migration (gitignored)
- `tabs.json`          — tab list and names (gitignored)
- `tabs.json.bak`      — last written tab registry backup (gitignored)
- `tabs/<tab-id>.txt`  — stored tab text (gitignored)
- `deleted/*.txt`      — server-side archive of deleted tab text files (gitignored)
- `mirror/*.txt`        — optional generated one-way mirror (gitignored)

## Data safety

- Text saves are written to a temporary file, fsynced, atomically moved into place, then the directory is fsynced.
- `tabs.json` is written the same way and mirrored to `tabs.json.bak`.
- If `tabs.json` is damaged, the service tries `tabs.json.bak`; if that also fails, it rebuilds the tab list from `tabs/*.txt` so text files remain reachable.
- Browser edits are written to local storage immediately. The draft is removed only after a confirmed server save.
- Deleted tabs disappear from the UI but their last server-side text file is moved to `deleted/`.

## Security Scope

This application intentionally has no accounts, authentication, or TLS. Run it
only on a trusted private network or behind an authenticated reverse proxy. Do
not commit `config.py`, tab data, deleted-tab archives, or generated mirrors.
