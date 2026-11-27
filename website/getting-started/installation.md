# Installation

## The easy way: one command

Install this on the **same computer that runs Plex Media Server**.

=== "Windows"
    Open **PowerShell** and paste:

    ```powershell
    irm https://nazmolla.github.io/Cleanplex/install.ps1 | iex
    ```

    - Downloads Hayaa for Plex to your user folder (no administrator prompt needed)
    - Installs Python and `ffmpeg` automatically if they are missing
    - Sets Hayaa for Plex to start automatically when you sign in, with the web UI reachable only from this computer (`http://localhost:7979`)

    When it finishes, your browser opens Hayaa for Plex and a short setup guides you through the rest.

=== "Mac"
    Open **Terminal** and paste:

    ```bash
    curl -fsSL https://nazmolla.github.io/Cleanplex/install.sh | sh
    ```

    - Finds your Plex account and installs Hayaa for Plex as a background service
    - Sets up Python and `ffmpeg` (using Homebrew if installed) automatically
    - Starts Hayaa for Plex right away (reachable only from this Mac at `http://localhost:7979`) and opens it in your browser

    When it finishes, your browser opens Hayaa for Plex and a short setup guides you through the rest.

=== "Linux"
    Open a terminal and paste:

    ```bash
    curl -fsSL https://nazmolla.github.io/Cleanplex/install.sh | sh
    ```

    - Finds your Plex account and installs Hayaa for Plex as a `systemd` service
    - Installs Python and `ffmpeg` automatically using your system's package manager
    - Starts Hayaa for Plex right away (reachable only from this machine at `http://localhost:7979`) and opens the setup page

    When it finishes, your browser opens Hayaa for Plex and a short setup guides you through the rest.

**Filtering every device (front door):** The installer also sets up Hayaa's [front door](../playback/front-door.md), so Plex is filtered on TVs, phones and Plex Web too, not only on the apps Hayaa can control directly. It asks first and the answer is **yes** unless you type `n`. It opens nothing new to the internet: clients keep using Plex's usual port. To skip it without being asked, add `--no-frontdoor` (Mac/Linux) or set `$env:HAYAA_FRONTDOOR="0"` (Windows).

**Opening the web UI from other devices on your network:** Because Hayaa for Plex has no login, the one-line installer locks the web UI to this computer by default. If you want to open it from another phone or computer at home, pass `--allow-network` (Mac/Linux) or `$env:HAYAA_ALLOW_NETWORK="1"` before the command (Windows) — be aware that anyone on your network can then open and change Hayaa:

- **Mac / Linux:** `curl -fsSL https://nazmolla.github.io/Cleanplex/install.sh | sh -s -- --allow-network`
- **Windows (PowerShell):** `$env:HAYAA_ALLOW_NETWORK="1"; irm https://nazmolla.github.io/Cleanplex/install.ps1 | iex`

**Updating:** Run the same command again. Your settings, scanned library, and history in `~/.cleanplex` are kept untouched.

**Uninstalling:** Stops and removes the service and app files, while keeping your `~/.cleanplex` data folder:

- **Mac / Linux:** `curl -fsSL https://nazmolla.github.io/Cleanplex/install.sh | sh -s -- --uninstall`
- **Windows (PowerShell):** `$env:HAYAA_UNINSTALL="1"; irm https://nazmolla.github.io/Cleanplex/install.ps1 | iex`

!!! tip "Recommended for NAS boxes: Docker"
    On a NAS (Unraid, Synology, QNAP, TrueNAS) or if you already run Plex in Docker, use **Docker**. There is nothing to build or compile: pick the setup that matches yours below, start the container, and open `http://<your-server>:7979`. On **Unraid** it is a few clicks in Community Apps, with no command line at all.

## Install with Docker (recommended)

Works on NAS boxes (Unraid, Synology, QNAP, TrueNAS) and any computer that runs Docker.

The image `ghcr.io/nazmolla/cleanplex` (mirrored to Docker Hub as `mnazmi/cleanplex`) needs no manual setup. On first start it reads Plex's URL, token and certificate from Plex's config folder, starts the front door, turns enforcement on and switches Plex Relay and DLNA off (set `CLEANPLEX_CONFIGURE_PLEX=false` to leave Plex's settings alone).

### Choose your setup

- **Unraid:**
  Install from Community Apps (search *Cleanplex*), or add the template by hand: Docker tab, *Add Container*, template URL `https://raw.githubusercontent.com/nazmolla/unraid-templates/main/templates/cleanplex.xml` (the template lives in [nazmolla/unraid-templates](https://github.com/nazmolla/unraid-templates)). It uses host networking, as Unraid's Plex does, with `NET_ADMIN` for the redirect. Point *Plex appdata* at Plex's appdata folder and mount your media at the same path Plex uses (add one *Media* entry per Plex media mount). If your Plex is in bridge mode, use the existing-Plex compose file instead.
- **New setup (Plex and Hayaa for Plex together):**
  Set `MEDIA_PATH` (and optionally `PLEX_CONFIG`, `PUID`, `PGID`, `TZ`) in a `.env` file and run `docker compose up -d` with [`docker-compose.yml`](https://github.com/nazmolla/Cleanplex/blob/main/docker-compose.yml). Claim the server at `http://<host>:32400/web`; Hayaa for Plex is at `http://<host>:7979`. The container joins Plex's network, and the host's port `32400` goes to the front door, so every client passes through it.
- **You already run Plex in Docker:**
  Remove the `32400:32400` port line from your Plex container, then use [`docker/compose.existing-plex.yml`](https://github.com/nazmolla/Cleanplex/blob/main/docker/compose.existing-plex.yml). It publishes `32400` itself and reaches Plex by container name (`PLEX_HOST`, `PLEX_NETWORK`).
- **Plex uses host networking:**
  Use [`docker/compose.host-network.yml`](https://github.com/nazmolla/Cleanplex/blob/main/docker/compose.host-network.yml). It adds an `nftables` rule on the host for the lifetime of the container and needs a kernel with `nftables` (many Synology models do not have it; use the existing-plex file there).
- **Home Assistant:**
  The separate [ha-cleanplex](https://github.com/nazmolla/ha-cleanplex) integration (HACS custom repository) shows scan queue, progress, streams and front door state, and has a switch to pause scanning. It only polls the web API.

### Rules for every Docker layout

- Mount the Plex config folder read-only at `/plex-config` and set `CLEANPLEX_PLEX_DATA=/plex-config`; the container runs as that folder's owner (override with `PUID` / `PGID`).
- Mount media at the **same path Plex sees it at**: Plex reports file paths and the scanner opens them directly.
- Plex creates its certificate only after the server is claimed; until then Hayaa for Plex waits and retries, and the web UI works.
- To skip the front door entirely (scan and mark only) set `CLEANPLEX_FRONTDOOR=0`.
- **Updating:** `docker compose pull && docker compose up -d`. The database lives in the `cleanplex-data` volume.

## Advanced: install from source

For developers, or if you cannot use Docker. You will need Python and Node.js and be comfortable with a terminal.

??? note "Show the step-by-step source install"
    #### 1. Clone the repository

    ```bash
    git clone https://github.com/nazmolla/Cleanplex.git
    cd Cleanplex
    ```

    #### 2. Install Python dependencies

    ```bash
    pip install -e .
    ```

    #### 3. Build the frontend (optional — required for the web UI)

    ```bash
    cd frontend
    npm install
    npm run build
    cd ..
    ```

    #### 4. Run

    ```bash
    python -m cleanplex
    ```

    Open `http://localhost:7979` in your browser and configure your Plex connection in **Settings**.

    #### 5. Install as a service

    === "macOS"
        LaunchDaemon, runs as a regular user (default `server`). Re-running updates the code and restarts it:

        ```bash
        sudo bash scripts/install-macos-service.sh [run_user]
        tail -f ~/.cleanplex/cleanplex.log          # rotated log of the run user
        ```

        If `launchd` answers `Bootstrap failed: 5` right after an update, the old process was still exiting: wait a few seconds and run:

        ```bash
        sudo launchctl bootstrap system /Library/LaunchDaemons/com.homeserv.cleanplex.plist
        ```

    === "Linux (systemd)"
        ```bash
        sudo cp cleanplex.service /etc/systemd/system/
        sudo systemctl daemon-reload
        sudo systemctl enable --now cleanplex
        sudo journalctl -u cleanplex -f   # View logs
        ```

        To install on a remote Linux host over SSH (Python, Node and the frontend build included):

        ```bash
        ./deploy.sh user@host [remote_dir]
        ```

    === "Windows"
        Run `start.bat`, or register `.venv\Scripts\cleanplex.exe` as a service (for example with NSSM). Run it as the same account as Plex so the front door can read Plex's certificate.

    #### 6. Filter every client (optional)

    Plex's 2026 apps and Plex Web accept no remote commands, so live skipping only reaches a few players. To filter every client, install the [front door](../playback/front-door.md) on the Plex machine:

    === "macOS / Linux"
        ```bash
        sudo bash scripts/frontdoor/install.sh
        ```

    === "Windows"
        Run `scripts\frontdoor\install-windows.ps1` in an elevated PowerShell.

    ---
