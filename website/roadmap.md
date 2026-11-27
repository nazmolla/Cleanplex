# Roadmap: Beyond Plex

Hayaa started on Plex, but families use other media servers too. Planned, in this order:

| Platform | Approach | Why this order |
|---|---|---|
| **Kodi** ([#242](https://github.com/nazmolla/Cleanplex/issues/242)) | Write each title's segments as a Kodi `.edl` file next to the video. Kodi reads these itself and skips (cut) or mutes (mute) with no add-on. Library scanning works on folders, so it needs no Kodi connection. | Kodi already understands `.edl`, and Hayaa already imports them; exporting is the missing half. |
| **Jellyfin** ([#243](https://github.com/nazmolla/Cleanplex/issues/243)) | Open source. Read libraries and sessions through its API, and either send seek and mute commands like the Plex integration does, or publish segments through Jellyfin's media segments feature so its own clients skip them. | Open source and documented, so integration and testing are straightforward. |
| **Emby** ([#244](https://github.com/nazmolla/Cleanplex/issues/244)) | Closed source since 2018, but its API is close to Jellyfin's (Jellyfin began as a fork of Emby), so most of the Jellyfin work carries over. | Builds on the Jellyfin work. |

The scanning, detection, review and Hayaa community sync parts do not depend on Plex, so each new platform is mainly a new "connector": listing the library, watching playback, and skipping or muting. Follow progress, or tell us which matters most to you, on the linked GitHub issues.
