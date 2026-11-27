# Requirements

Before installing Hayaa for Plex natively (without Docker), make sure your server has:

- **Python 3.11+**
- **`ffmpeg` and `ffprobe`** installed on the server
- **Plex Media Server** on the same network
- A **[Plex authentication token](plex-token.md)**

## Additional requirements for the front door

To filter every client using the [front door](../playback/front-door.md) (recommended because Plex's 2026 apps and Plex Web do not accept remote control commands):

- Hayaa for Plex running on the **same machine as Plex**
- **`nginx` with the stream module**:
  - **macOS:** `brew install nginx`
  - **Debian / Ubuntu:** `apt install nginx-full`
  - **Fedora:** `dnf install nginx nginx-mod-stream`
  - **Windows:** nginx for Windows

If you use the [Docker installation](installation.md#docker-nas-unraid-synology-qnap-truenas-any-docker-host), `ffmpeg`, `ffprobe`, Python, and `nginx` are already bundled in the container image.
