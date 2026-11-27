---
title: Home
---

<div class="hayaa-hero" markdown>
<img src="assets/hayaa-icon.png" alt="Hayaa for Plex icon" width="128">

# Hayaa for Plex <small>(formerly Cleanplex)</small>

A home server service that watches your Plex streams and, for the accounts you choose, **skips or mutes** nudity and profanity.
</div>

!!! note "Cleanplex is becoming Hayaa for Plex"
    Only the name and look change for now: the repository, the Docker images (`ghcr.io/nazmolla/cleanplex`, `mnazmi/cleanplex`), settings, and data all stay the same, and nothing needs updating.

    Hayaa for Plex is an independent project; it works with Plex but is not made or endorsed by Plex.

Scenes are found ahead of time by scanning your library locally—or imported from skip files other people have made—so at playback time it is only a database lookup.

## Highlights

<div class="grid cards" markdown>

-   **Your home, your standards**

    ---

    Choose which Plex accounts are filtered and set per-category strictness levels (0–4). Viewers you do not filter see nothing change.

-   **Private by design**

    ---

    Scanning runs on your own server with local models (NudeNet, optional SigLIP) and subtitle checks. Titles, file names, and frames never leave your server.

-   **Works on every Plex client**

    ---

    Pair background scanning with the [front door](playback/front-door.md) to filter streams on every client—including Plex Web, iPhone, iPad, and Apple TV—without touching your original files.

-   **Skip files & community sync**

    ---

    Import `.skp`, `.edl`, `.mcf`, or pasted timestamp lists to avoid ML scanning altogether, or share what your scans found (time ranges and labels only) with other servers via [Hayaa community sync](hayaa-sync.md).

</div>

## Quick links

- [Install Hayaa for Plex](getting-started/installation.md) — Docker, Unraid, macOS, Linux, and Windows setup
- [How it works](guide/how-it-works.md) — Pre-scan, playback monitoring, web UI, and Plex integration
- [Hayaa community sync](hayaa-sync.md) — Scan once, share time ranges and labels (never titles or frames), and verify locally
- [GitHub repository](https://github.com/nazmolla/Cleanplex) — Source code, releases, and issue tracker
