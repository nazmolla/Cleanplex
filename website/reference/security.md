# Security

## Plex token handling

Once saved, your Plex token is never sent back to the browser: **Settings** shows *"Saved. Leave blank to keep it"*, and saving with the field empty keeps the stored token.

## Web UI and API access

The web UI and API have **no login**, and anyone who can reach the port (`7979` by default) can change settings and delete segments.

On a network you do not fully trust:

- Bind to `127.0.0.1` using [`CLEANPLEX_HOST`](environment-variables.md) behind a reverse proxy that adds authentication.
- Cross-origin API calls are refused unless you list the allowed origins in [`CLEANPLEX_CORS_ORIGINS`](environment-variables.md).
