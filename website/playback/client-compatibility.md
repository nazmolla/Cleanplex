# Client Compatibility

The seek command is sent via the Plex Player Control API and works with some modern Plex clients, while others require the [front door](front-door.md):

| Client | Supported |
|---|---|
| Plex Web (browser) | ❌ for live skipping (tested 2026-11-05: Plex answers its seek with 404). ✅ with the front door, see [Stream Filtering (Front Door)](front-door.md) |
| Plex for iOS / Android | ❌ for live skipping on Plex for iOS 2026.20 (no remote-control port). Use the front door |
| Plex HTPC | ✅ Fully supported |
| Plex Media Player (desktop) | ✅ Fully supported |
| Apple TV | ❌ for live skipping on Plex for Apple TV 2026.20 (no remote-control port). Use the front door |
| Roku | ⚠️ Limited support |
| Some Smart TV apps | ⚠️ Limited support |

The Dashboard shows a **Controllable** badge per stream so you can see which clients support seeking.

**Remote Device Access:** The app proxies Plex images through its own server, so posters and artwork load correctly on remote clients (not just localhost).
