# Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CLEANPLEX_DATA` | `~/.cleanplex` | Data directory for DB and thumbnails |
| `CLEANPLEX_PORT` | `7979` | Web UI port |
| `CLEANPLEX_HOST` | `0.0.0.0` | Bind address. The API has no login, so use `127.0.0.1` behind a reverse proxy if the network is not trusted |
| `CLEANPLEX_LOG_FILE` | none | Write the log to this file, rotated at 10 MB with 4 backups, instead of stdout. The macOS install script sets it to `~/.cleanplex/cleanplex.log` |
| `CLEANPLEX_NICE` | `10` | How much to lower the service's CPU priority at startup, so Plex's transcoder wins when the scanner is busy. `0` leaves the priority alone |
| `CLEANPLEX_CORS_ORIGINS` | none | Comma-separated origins allowed to call the API from another site; same-origin use needs nothing |
| `CLEANPLEX_PROXY_PORT` | `7981` | Port of the stream proxy the front door talks to; `0` turns it off |
| `CLEANPLEX_PROXY_HOST` | `CLEANPLEX_HOST` | Bind address of the stream proxy. The service installers set `127.0.0.1`: only `nginx` needs it |
| `CLEANPLEX_PLEX_DATA` | per OS | The folder that contains `Library/Application Support/Plex Media Server` (a Plex Docker volume's `/config`), for a layout the front door cannot find itself |
| `CLEANPLEX_PLEX_UPSTREAM` | `127.0.0.1:32400` | `host:port` the front door and Hayaa for Plex reach Plex at (Plex in another container) |
| `CLEANPLEX_PLEX_URL` / `CLEANPLEX_PLEX_TOKEN` | from Plex's folder | Docker: set the Plex URL and token without the web UI; they override saved values on every start |
| `CLEANPLEX_FRONTDOOR` | `1` | Docker: `0` runs without the front door (no `nginx`, no stream filtering) |
| `CLEANPLEX_CONFIGURE_PLEX` | `1` | Docker: switch Plex Relay and DLNA off on the first start |
| `CLEANPLEX_REDIRECT` | — | Docker host-network mode: `nft` adds the `32400` to `32480` redirect |
| `PUID` / `PGID` | owner of Plex's folder | Docker: user and group the container runs as |
| `CLEANPLEX_PLEX_MACHINE_ID` | from Plex | Plex's `ProcessedMachineIdentifier`, if it cannot be read from Plex's preferences |
| `CLEANPLEX_NGINX` | `nginx` | `nginx` binary used to reload the front door when Plex renews its certificate |
