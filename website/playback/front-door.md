# Stream Filtering for Every Client (Front Door)

Plex's 2026 apps (iPhone, iPad, Apple TV) and Plex Web accept no remote commands, so live skipping cannot reach them. The **front door** filters every client instead, in or out of the house: Hayaa for Plex sits in front of Plex's port and removes the filtered ranges from the stream itself.

## How it works

- A firewall redirect sends Plex's port `32400` to an `nginx` front on port `32480`. `nginx` uses Plex's own `plex.direct` certificate, so encrypted connections keep working, and streams video straight from Plex. Plex stays on `127.0.0.1:32400`, untouched.
- For a viewer whose filtering is on, playing a title with segments their levels skip, Hayaa for Plex refuses direct play, asks Plex for an HLS stream and removes the segments covering those ranges from the playlist (and re-times subtitles). When the file's keyframes are indexed (a background job does this for flagged titles) the video is **copied, not re-encoded**, so 4K, HDR and Dolby Vision are kept; cuts then widen to whole segments (up to ~10 s). Otherwise Plex re-encodes, with ~1 s precision.
- **Fail closed**: the original file, downloads and DASH/progressive streams of such a title are refused for that viewer.
- **Backstop**: a filtered viewer's stream of a flagged title that did not pass the front door (Plex Relay, DLNA) is stopped with a message.
- Everyone else, and unflagged titles, play exactly as before (direct play included).
- Each cut a viewer plays across is recorded as a skip in the skip history.

## Installing the front door

=== "macOS / Linux"
    Uses `pf` on macOS or `nftables` on Linux; needs `nginx` with the stream module:

    ```bash
    sudo bash scripts/frontdoor/install.sh            # macOS default account: server; Linux: plex (--user to change)
    sudo bash scripts/frontdoor/install.sh --uninstall
    ```

=== "Docker"
    The Docker image sets the front door up automatically — see [Docker installation](../getting-started/installation.md#docker-nas-unraid-synology-qnap-truenas-any-docker-host).

=== "Windows (untested)"
    Mode B (untested): `scripts/frontdoor/install-windows.ps1` runs the front door on `32480`, prints the URL to set as Plex's *Custom server access URL*, and blocks other machines from `32400` so clients fail over to it.

## Settings

Under **Settings → Stream Filtering**:

| Setting | Default | Notes |
|---|---|---|
| Mute segments in filtered streams | Leave | A stream cannot be muted by editing it: profanity is either cut like a skip or left in (`enforcement_mute_mode`: `cut`/`leave`) |
| Stop streams that bypassed the front door | On | Acts only once the front door is installed (`enforcement_enabled`, set by the installer) |
| Plex Relay | — | **Recommended off**: relayed streams never reach the front door. Remote viewers then need Plex's direct remote access (Settings → Remote Access, "mapped"); behind CGNAT without a port forward they lose remote access |
| Plex DLNA | — | Recommended off: DLNA serves files on its own port |
| Simultaneous transcodes | Plex's | Filtered streams of flagged titles are copied or re-encoded by Plex; raise it for several viewers at once |

## Known limits

- The viewer sees a shortened timeline (progress and resume are mapped back for Plex).
- Scrub-bar preview thumbnails are not filtered (a deliberate choice: a small leak, and previews keep working).
- Offline downloads made before the front door are unfiltered.
