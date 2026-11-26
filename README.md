# Cleanplex

A home server service that watches your Plex streams and, for the accounts you choose, **skips or mutes** the parts of a film that contain nudity or profanity. The scenes are found ahead of time by scanning your library (or imported from skip files other people have made); at playback time it is only a database lookup.

## How It Works

### 1. Pre-scan (background analysis)
New titles are queued and scanned inside the **scan window** (23:00–06:00 by default), a few at a time:
- `ffmpeg` extracts a frame every **Scan Frame Interval** (5 s by default), using the machine's hardware video decoder when it has one
- Each frame goes through [NudeNet](https://github.com/notAI-tech/NudeNet), a local ONNX body-part detector (the bundled 320n model, or the larger 640m)
- Optionally, frames NudeNet passed over are judged by a second, whole-frame model, **SigLIP**, to catch scenes NudeNet misses (see [Detection Pipeline](#detection-pipeline-and-accuracy))
- Flagged frames are grouped into **segments**; each flagged window is then re-sampled every 500 ms so the thumbnail is the strongest frame and the segment covers the whole scene
- A segment's severity comes from what was detected, and the optional **false-positive verifier** can reject detections that look like ones you have rejected before
- Titles with an imported skip file are never scanned, and subtitles are scanned for profanity without touching a frame (see below)
- Scan time depends on the frame interval, worker count and hardware decoding; everything runs locally, nothing is uploaded

### 2. Real-time playback monitoring
While someone watches, the service polls Plex (every 5 s by default, tighter as a stream approaches a segment):
- Checks whether a filtered user's position is entering a flagged segment, widened by the **pre/post buffers** (3 s each by default)
- Sends a seek past the whole segment (or lowers the volume for a `mute` segment) and verifies on the next tick that the client really moved
- This is a database lookup; no ML runs at playback time

### 3. Web UI
A full browser interface at `http://your-server:7979`. Every page updates live: the server pushes changes (scans, new segments, synced titles, skips, active streams, user and marker changes) over Server-Sent Events, so nothing needs a manual refresh. Pages: Dashboard, Library, Segments, Markers, Analytics, Users and Settings (see [Web UI Pages](#web-ui-pages)).

### 4. Plex integration
Cleanplex also writes a short **"Cleanplex scan:" block into each scanned title's Plex summary** (status, segment count and what found them, false positives and corrections). It is refreshed a couple of seconds after scans, deletes, false-positive marks, relabels and imports, and only touches its own block.

## Requirements

- Python 3.11+
- `ffmpeg` and `ffprobe` installed on the server
- Plex Media Server on the same network
- A Plex authentication token
- For filtering every client ([front door](#stream-filtering-for-every-client-front-door)): Cleanplex on the same machine as Plex, and `nginx` with the stream module (macOS: `brew install nginx`; Debian/Ubuntu: `apt install nginx-full`; Fedora: `dnf install nginx nginx-mod-stream`; Windows: nginx for Windows)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/nazmolla/Cleanplex.git
cd Cleanplex
```

### 2. Install Python dependencies

```bash
pip install -e .
```

### 3. Build the frontend (optional — required for the web UI)

```bash
cd frontend
npm install
npm run build
cd ..
```

### 4. Run

```bash
python -m cleanplex
```

Open `http://localhost:7979` in your browser and configure your Plex connection in **Settings**.

### 5. Install as a service

**macOS** (LaunchDaemon, runs as a regular user, default `server`; re-running updates the code and restarts it):

```bash
sudo bash scripts/install-macos-service.sh [run_user]
tail -f ~/.cleanplex/cleanplex.log          # rotated log of the run user
```

If launchd answers `Bootstrap failed: 5` right after an update, the old process was still exiting: wait a few seconds and run `sudo launchctl bootstrap system /Library/LaunchDaemons/com.homeserv.cleanplex.plist`.

**Linux (systemd)**

```bash
sudo cp cleanplex.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cleanplex
sudo journalctl -u cleanplex -f   # View logs
```

To install on a remote Linux host over SSH (Python, Node and the frontend build included): `./deploy.sh user@host [remote_dir]`.

**Windows**: run `start.bat`, or register `.venv\Scripts\cleanplex.exe` as a service (for example with NSSM). Run it as the same account as Plex so the front door can read Plex's certificate.

### 6. Filter every client (optional)

Plex's 2026 apps and Plex Web accept no remote commands, so live skipping only reaches a few players. To filter every client, install the [front door](#stream-filtering-for-every-client-front-door) on the Plex machine:

```bash
sudo bash scripts/frontdoor/install.sh              # macOS / Linux
```

On Windows, run `scripts\frontdoor\install-windows.ps1` in an elevated PowerShell.

### Docker (NAS, Unraid, Synology, QNAP, TrueNAS, any Docker host)

The image `ghcr.io/nazmolla/cleanplex` (mirrored to Docker Hub as `mnazmi/cleanplex`) needs no manual setup. On first start it reads Plex's URL, token and certificate from Plex's config folder, starts the front door, turns enforcement on and switches Plex Relay and DLNA off (set `CLEANPLEX_CONFIGURE_PLEX=false` to leave Plex's settings alone).

**New setup (Plex and Cleanplex together):** set `MEDIA_PATH` (and optionally `PLEX_CONFIG`, `PUID`, `PGID`, `TZ`) in a `.env` file and run `docker compose up -d` with [docker-compose.yml](docker-compose.yml). Claim the server at `http://<host>:32400/web`; Cleanplex is at `http://<host>:7979`. Cleanplex joins Plex's network, and the host's port 32400 goes to the front door, so every client passes it.

**You already run Plex in Docker:** remove the `32400:32400` port line from your Plex container, then use [docker/compose.existing-plex.yml](docker/compose.existing-plex.yml). It publishes 32400 itself and reaches Plex by container name (`PLEX_HOST`, `PLEX_NETWORK`).

**Plex uses host networking:** use [docker/compose.host-network.yml](docker/compose.host-network.yml). It adds an nftables rule on the host for the lifetime of the container and needs a kernel with nftables (many Synology models do not have it; use the existing-plex file there).

**Unraid:** install from Community Apps (search *Cleanplex*), or add the template by hand: Docker tab, *Add Container*, template URL `https://raw.githubusercontent.com/nazmolla/unraid-templates/main/templates/cleanplex.xml` (the template lives in [nazmolla/unraid-templates](https://github.com/nazmolla/unraid-templates)). It uses host networking, as Unraid's Plex does, with `NET_ADMIN` for the redirect. Point *Plex appdata* at Plex's appdata folder and mount your media at the same path Plex uses (add one *Media* entry per Plex media mount). If your Plex is in bridge mode, use the existing-Plex compose file instead.

**Home Assistant:** the separate [ha-cleanplex](https://github.com/nazmolla/ha-cleanplex) integration (HACS custom repository) shows scan queue, progress, streams and front door state, and has a switch to pause scanning. It only polls Cleanplex's web API.

Rules for every layout:
- Mount the Plex config folder read-only at `/plex-config` and set `CLEANPLEX_PLEX_DATA=/plex-config`; the container runs as that folder's owner (override with `PUID`/`PGID`).
- Mount media at the **same path Plex sees it at**: Plex reports file paths and Cleanplex opens them to scan.
- Plex creates its certificate only after the server is claimed; until then Cleanplex waits and retries, and the web UI works.
- To skip the front door entirely (scan and mark only) set `CLEANPLEX_FRONTDOOR=0`.
- Updating: `docker compose pull && docker compose up -d`. The database lives in the `cleanplex-data` volume.

## Configuration

All configuration is done via the **web UI** at `http://your-server:7979/settings`, grouped into sections (Plex Connection, Users, Playback, Stream Filtering, Scan Scope & Schedule, NudeNet Detection, SigLIP Fallback, Segments, Hayaa). No config files to edit.

| Setting | Default | Description |
|---|---|---|
| Plex Server URL | — | e.g. `http://192.168.1.10:32400` |
| Plex Token | — | Your Plex authentication token |
| Hayaa service URL | — | Hayaa sync API, https (plain http only to this machine, for Hayaa's dev server) |
| Hayaa sign-in URL | — | Hayaa's sign-in (OAuth) host, https |
| Stream check interval | 5s | Longest wait between checks of Plex's active streams: stops filtered streams that bypassed the front door, updates the Dashboard, and drives fallback skipping (checks tighten near a segment) |
| Confidence Threshold | 0.6 | NudeNet score threshold (0–1). Lower = more sensitive. |
| NudeNet Model | 320n | `320n` is bundled and fastest; `640m` is larger and more accurate, downloaded once on first use (or pre-download it in the same section) |
| Scan Frame Interval | 5000 ms | How often a frame is sampled while scanning. Lower catches shorter scenes but scans slower |
| Hardware video decoding | on | Use the machine's video decoder for scans when it has one (much faster on 4K HEVC); a file it cannot decode falls back to software |
| Per-Label Thresholds | blank | Optional per-label NudeNet threshold (NudeNet Detection section). A blank label uses the Confidence Threshold. Stored in `label_thresholds` |
| Seek before / Seek after | 3000 ms each | Fallback skipping only: how far a segment is widened before its start and after its end (`pre_buffer_ms`, `post_buffer_ms`), so the flagged content is never briefly shown and the filter does not re-trigger on the tail. Streams the front door edits are cut at the exact segment edges |
| Profanity Wordlist | built-in | JSON array of words the subtitle scanner mutes. Empty uses the bundled list |
| Skip Event Retention | 90 days | How long skip history is kept before being pruned at startup |
| Scan Window | 23:00–06:00 | Time window when background scanning runs; *Scan now* ignores it |
| Library Exclusions | none | Libraries that are not scanned or synced |
| Hover Previews | on | Render a short silent loop of each active segment, played when you hover its thumbnail (~20–40 KB each, stored in `~/.cleanplex/hover`) |
| Hover previews when idle | on | Also render hover previews as soon as the scan queue is empty and nothing is scanning, not only inside the window below; stops when a scan starts |
| Hover Preview Window | 00:00–07:00 | When the hover preview job runs (Settings → Schedule). It renders one segment at a time, picks up segments it has not done yet, and removes files of deleted segments |
| Scanner Workers | 2 | Number of parallel scans (higher = more CPU/memory usage) |
| Detector Labels | — | Select which nudity types to detect (and skip). Only selected labels trigger skips |
| SigLIP Fallback | off | Second, whole-frame check that runs only on frames NudeNet did not flag, to catch scenes it misses. Follows Detector Labels. Downloads ~815 MB of model files on first use. Its segments have source `siglip`, and their severity follows the label they are tagged with |
| SigLIP Threshold | 0.3 | Share (0–1) of a frame's match that must go to enabled-label descriptions. Separate from Confidence Threshold. Higher = fewer, surer hits |
| Per-label SigLIP thresholds | blank | Optional threshold per label; a SigLIP hit is judged by the label it is tagged with. Blank uses the SigLIP Threshold. Stored in `siglip_label_thresholds` |
| False-positive verifier | off | Filters detections that look like ones you rejected; train it and pick a level (5/10/20% of good segments you accept losing) in Settings → SigLIP Fallback. Stored in `verifier_loss_pct`, model in `models/verifier.npz`. See [Detection Pipeline](#detection-pipeline-and-accuracy) |
| Ignore end credits | on | SigLIP is not run on frames at or after the title's Plex credits marker, since text-heavy credit screens are read as nudity. NudeNet still runs. No marker, no effect |
| Inference threads | 0 (auto) | onnxruntime threads per scan worker (`inference_threads`, API only). Auto leaves about one worker's share of CPU free for Plex and the web UI |
| SigLIP Consecutive Frames | 2 | SigLIP-only hits must repeat on this many consecutive sampled frames before a segment is created |
| Segment Merge Gap | 12000 ms | Flagged frames closer than this are merged into one segment |
| Minimum Hits Per Segment | 1 | Flagged frames a cluster needs to be kept. Raise it to cut false positives |
| Refinement Step | 500 ms | After a segment is found, its window is re-sampled this finely so the thumbnail is the highest-scoring frame and the segment also covers scene frames the coarse scan stepped over. Segments only ever grow; 0 turns it off. The card shows the time the thumbnail frame was taken at (Settings → Segments). Segments found before this existed are refined by a background job inside the scan window, one at a time (no rescan needed); Settings → Segments shows how many are still waiting |
| Content Ratings | — | Only scan and filter titles matching your selected ratings. `NR` also covers titles Plex reports as "Not Rated" or "Unrated" |

## Detection Pipeline and Accuracy

**NudeNet** finds body parts and gives each a confidence. A frame is flagged when a label you enabled under *Detector Labels* reaches its threshold (the global *Confidence Threshold*, or a per-label override). Covered labels are not scanned by default.

**SigLIP fallback** (off by default; downloads ~815 MB on first use) catches scenes NudeNet misses: wide, dim or partly hidden frames. It only runs on frames NudeNet did *not* flag, and, unless *Ignore end credits* is turned off, never after the title's Plex credits marker (credit rolls read as nudity). It embeds the whole frame once and compares it with short text descriptions ("bare buttocks", "people having sex", but also counter-examples like "a couple fully clothed in bed", "end credits scrolling text", "a landscape"). The frame's score is the share of probability that lands on descriptions of labels you enabled, so clothed or ordinary scenes pull it down. A hit is tagged with the existing label whose own descriptions scored highest, is judged by that label's SigLIP threshold, and must repeat on *SigLIP Consecutive Frames* sampled frames before it becomes a segment. SigLIP segments are marked as such (source `siglip`) so you can tell them apart and threshold them separately.

**Segments** group flagged frames closer together than the *Segment Merge Gap* (12 s). A cluster needs *Minimum Hits* flagged frames to be kept. Severity follows the labels (see [Categories, severity and actions](#categories-severity-and-actions)).

**False-positive verifier** (off by default) is a small filter trained on *your* verdicts. Frames you marked as false positives are compared with frames of segments you kept, using SigLIP's image embedding, and a separate classifier per detector **and label** (for example SigLIP hits on `BUTTOCKS_EXPOSED`) learns what your rejects look like. A label that does not yet have at least 30 rejected and 30 kept examples has no model, so its detections are never rejected; rejections you made under one label cannot affect another. At scan time a flagged segment whose peak frame looks like them is **rejected**: stored inactive, with its frame, under Analytics → False positives (filter *Rejected by the verifier*), where the undo button restores a wrong rejection. Setup: mark false positives as you review, then Settings → SigLIP Fallback → *Train* (needs at least 30 false positives and 30 kept segments for a label, and SigLIP's models), pick a level, save, and rescan (*Rescan Titles → scanned by an older scanner*). The level is how many good segments you accept losing: **5% Gentle, 10% Balanced, 20% Strict**. Retrain after you have marked more.

### Tuning accuracy

1. Review segments and mark wrong ones as false positives (the thumbs-down on a segment); correct wrong labels with the tag button. Both are kept as feedback.
2. Open **Analytics → Accuracy** to see, per detector and label, how often detections were marked false and the mean scores of kept versus marked ones.
3. Raise the threshold of labels with a high false-positive rate (Settings → NudeNet Detection / SigLIP Fallback → per-label thresholds). Raising a threshold also drops some real detections, so move it a little at a time. SigLIP is weakest on the genital labels, where it reacts to the scene rather than the body part; give those a high threshold or leave the label out of Detector Labels, which stops both detectors flagging it.
4. Train the verifier (above) to catch what thresholds cannot, such as clothed bed scenes, swimming, credits and skin close-ups.
5. Rescan to apply new settings to titles already scanned: Settings → *Rescan Titles* with *Scanned by an older scanner* after an update, or *Titles that have segments*.

Detection is statistical: expect some misses and some false positives, and review what matters to you. Imported skip files are human-graded and are the most accurate source where they exist.

## Segment Expansion & Skip Logic

**Default behavior:** When a segment is detected (e.g., 30s–60s), Cleanplex automatically:
1. **Widens the segment** by the Pre and Post Buffer settings → 27s–63s at the 3000ms defaults
2. **Monitors with a lookahead** of one poll interval — the filter triggers before the widened start to absorb polling latency
3. **Skips to 27s** and holds the block until playback reaches 63s

Polling tightens automatically as a stream approaches a segment, so skips land accurately without
polling Plex hard the rest of the time. Every skip is verified on the next tick: a client that
accepts the command without actually moving is recorded as a failure and re-probed.

### Categories, severity and actions

Segments carry a **category** (nudity, sex, violence, language, drugs, fear, commercial, …), a
**severity** (low/medium/high) and an **action**:

| Action | Behaviour |
|---|---|
| `skip` | Seeks past the segment |
| `mute` | Drops the volume for the segment and restores it after — used for profanity |

Each user gets a 0–4 level per category (Off, Severe only, Moderate and up, Mild and up,
Everything). A segment fires when *level + severity* exceeds 4 (minimal 1, low 2, medium 3,
high 4), so level 4 filters everything in that category and level 0 filters nothing. Every
category starts at level 4 when filtering is on; lowering or turning one off is always an explicit choice, and
changing one category never affects the others.

**Scanner segments get their severity from the detected label**, so a level has a concrete meaning
for nudity:

| Detected | Severity | Skipped at level |
|---|---|---|
| Exposed genitalia, exposed female breast | high | 1, 2, 3, 4 |
| Exposed buttocks, anus, belly | medium | 2, 3, 4 |
| Covered genitalia, covered female breast | low | 3, 4 |
| Everything else: other covered parts, feet, armpits, faces, male chest | minimal | 4 |

Imported skip files use MovieContentFilter's low, medium and high, which keep their order.

A segment with several labels takes the highest. Covered labels are not scanned by default; enable
them under Detector Labels if level 3 should skip them. Imported segments keep the severity their
author set. If the detector names the wrong thing, use the tag button on a segment to correct its
labels: the severity follows, the correction is kept as feedback next to the detector's original
labels, and a rescan of the same window keeps it.

`blank` and `blur` appear in some imported files but are **not supported**: Plex exposes playback
position and volume, not the video stream. Those segments are logged and ignored rather than
misapplied as skips.

## Importing Skip Files

Cleanplex reads skip files produced by other tools. **A title with an imported file is never
ML-scanned**, so this is by far the cheapest way to fill your library.

| Format | Extension | Notes |
|---|---|---|
| VideoSkip | `.skp` | Categories, 1–3 severity, and skip/mute/blank handling. The VideoSkip Exchange has ~563 movies and ~110 series, free and without an account |
| Kodi / MPlayer EDL | `.edl` | `start end action`; 0 cut, 1 mute, 2 scene marker, 3 commercial. |
| MovieContentFilter | `.mcf` | Spec 1.1.0, ~130 hierarchical categories. |
| Pasted list | `.txt` | Loose `00:12:30 - 00:13:05 nudity` lines, for lists that have no machine format |

Two ways in:

- **Sidecar** — drop `movie.skp` (or `.edl` / `.mcf`) next to `movie.mkv`. It is imported when the
  title is first discovered and the scan is skipped entirely.
- **Browse** — open a title's *Segments* view and use the import box to upload a file or paste a list.

Imported timings are authored against a specific cut of a film. Cleanplex checks them against the
title's runtime and warns when they do not line up, since an extended edition will throw every
timestamp out.

Cleanplex imports skip files but never exports segments, so its detections and synced segments cannot be redistributed as cutting lists.

## Profanity Filtering

Subtitles are scanned against a configurable wordlist and matching lines become **muted** segments —
no frames, no inference, seconds per title. Matching uses word boundaries with suffix handling, so
"shitting" is caught while "classic" is not.

Because the timings come from one audio track, these segments are tagged with that track's language
and only fire when it is the one playing; a dubbed track is left alone rather than muted in the
wrong places.

Violence, gore, drugs and frightening content have **no local detector** — NudeNet covers nudity
only. Those categories are populated by importing skip files, where humans have already graded them.

## Finding Your Plex Token

1. Sign in to Plex Web
2. Browse to any media item
3. Click the three-dot menu → **Get Info** → **View XML**
4. The URL will contain `?X-Plex-Token=XXXXXX`

## Security

Once saved, the token is never sent back to the browser: Settings shows "Saved. Leave blank to keep it", and saving with the field empty keeps the stored token. The web UI and API have **no login**, and anyone who can reach the port can change settings and delete segments, so on a network you do not fully trust bind to `127.0.0.1` (`CLEANPLEX_HOST`) behind a reverse proxy that adds authentication. Cross-origin API calls are refused unless you list the origin in `CLEANPLEX_CORS_ORIGINS`.

## Web UI Pages

#### Dashboard
- **Active streams** — Shows all users currently watching, with playback position and controllability status
- **Scanner status** — Number of workers active, queue size, and which titles are currently being scanned
- **Recent skips** — Every recorded skip and mute (paged, with a total), with optional from/to date filters; failed actions are flagged. History is kept for `skip_event_retention_days`

#### Browse
Titles, segments and Plex markers share one page. The sidebar lists your libraries; the main list always shows what is inside the current level (libraries, shows, seasons, movies, episodes) in the same rows, and clicking a row opens it. *Titles / Segments / Markers* choose what is listed for that level, and a movie or episode opens on its own page with its segments and markers. The level and view are part of the address, so a level can be bookmarked; the old `/library`, `/segments` and `/markers` addresses open Browse.

- **Search and filters** — the search box is always in the toolbar; *Filters* opens the rest below it, and active filters show as chips you can remove. While a filter is active the list shows the matches directly (a show whose name matches as one row, episodes with their show and season beside them). Filters: name, **actors** (the cast and the actors' photos are copied from Plex in the background; search the list, order it by most titles, name or most segments, and pick several actors, matching titles with any or all of them), rating, release-year range, scan-date range, scan status and ignored titles. The numbers in the tree count the titles that pass them
- **Sort** — date added, scan date, name, release year or segments, with the direction spelled out (*Newest first / Oldest first*); defaults to date added, newest first
- **Segments at any level** — the Segments view lists every segment of the matching titles as rows with a 240 px frame (hover plays the preview, click opens the player), with segment filters in the same panel: state (active, marked false positive, rejected by the verifier), what found it, and label. Import sits behind *Import skip files* on a title's page
- **Bulk actions** — tick titles, shows or seasons (or *select all filtered*) to scan, ignore, un-ignore or delete the segments of the selection; deleting segments of a whole title, show or selection asks first
- **Import skip files** — Upload `.skp` / `.edl` / `.mcf`, or paste a timestamp list. There is no export
- **Browse all titles** with scan status icons
- **Trigger scans** per title or entire library
- **"Scan Now"** to prioritize a title — moves it to the front of the scanning queue for immediate processing
- **Scan completion timestamps** — shows when each title finished scanning

#### Browse: segments
- **Live scanner banner** — shows which titles are currently being scanned with progress bars
- **Segment details** — Timestamp range, confidence score, a thumbnail from the detected scene, and what found it (e.g. *NudeNet 640m*, *SigLIP*, *Subtitles*, *VideoSkip (.skp)*)
- **Delete false positives** — Remove incorrectly flagged segments
- **File cleanup** — Deleting a segment (or a title's segments, a rescan, or an import that replaces them) removes its thumbnail and hover preview. A sweep at startup and every 6 hours removes thumbnails, hover previews and feedback frames nothing references (older than an hour), and the saved data of removed titles (see *Removed titles*). The preview clip and poster caches are pruned by age and size (see *Data, Backups & Maintenance*). Feedback frames kept for false-positive and relabel verdicts are never removed
- **Right-click to rescan** — On a movie, episode, season or show: *Rescan now* or *Rescan tonight* (queued for the scan window). On a show, season or library the first pair covers only its listed titles that have segments, so it rescans those listed episodes; *Rescan whole show now / tonight* and *whole season now / tonight* queue every non-ignored episode, including ones with no segments; right-clicking a library offers *Rescan whole library tonight* (finished and failed titles, scheduled only, never jumped to the front)
- **Failed scans** — At library level in Browse, *Retry failed* (the round-arrow button): every failed title of the library is queued again (inside your rating filter), except titles whose media file is still missing, which stay on the list and are counted; ignored titles are taken off the Failed list. The same choice is in Settings → *Rescan Titles*. A failed title shows the reason its scan failed. *Sync from Plex* also queues a failed title again when Plex reports a different file for it, when its file changed after the failure, or when the failure is more than a day old (a missing file stays failed)
- **Removed titles** — *Sync from Plex* drops titles Plex no longer reports. Their segments disappear from every list and count but are kept as marks; 30 days after removal everything else saved for the title (segment thumbnails and hover previews, cast, Plex markers, skip history, keyframe index) is deleted. A title that returns gets its marks back
- **Rescan many titles** — Settings → Scan Scope & Schedule → *Rescan Titles*: pick titles scanned by an older scanner (the right choice after an update), titles with segments, every scanned title, or titles scanned before a date, optionally for one library. A live count shows what will be queued and how many your rating filters skip; they go through the normal scan queue (scan window, usual order) and keep their current segments until the new scan replaces them
- **Live updates** — The open title's segments and the tree counts refresh as a rescan runs and when it finishes
- **Hover preview** — Hovering a segment's thumbnail plays a 5 s silent loop of it (sped up for long segments), in Browse and Analytics. Rendered overnight by the hover-preview job, so new segments get one the next night
- **Preview video** — In-app player; the server cuts a short H.264/AAC clip (5 s either side) so it plays in any browser, reading the file as the service account. *Open in VLC* starts and stops at the segment
- **Jump to segment** — Send Plex seek command to that spot in the video
- **Scan completion info** — Timestamp when the title finished scanning

#### Browse: markers
- Lists the titles of a library with their **Plex intro and credits markers**, and lets you create, edit and delete them (and refresh them from Plex). A title's credits marker is what stops SigLIP from reading end credits as nudity

#### Analytics
Tabs (linkable with `?tab=`), and sections that can be minimised (remembered per browser). The segment lists of *Detections* and *False positives* switch between a list and a **gallery** of large frames (remembered per browser):
- **Detections** — Segments per label (click a bar for a per-rating breakdown), a label filter, and the matching segments with preview, jump, false-positive and delete actions
- **Skips** — Skip and mute history by category, client and title
- **Accuracy** — How often each detector and label was marked a false positive by you (verifier rejections are counted separately and left out of the rate), with mean scores of kept vs. marked segments
- **False positives** — Every inactive segment, newest first, with an undo button: the ones you marked and the ones the verifier rejected (filter *Flagged by you / Rejected by the verifier*, each tagged)

#### Users (under Settings)
- **Toggle filtering per Plex account** — Turn skipping on/off for specific user accounts
- **Category preferences** — Expand a user to set a 0–4 strictness level per content category, with an optional skip/mute override

#### Settings
- **Plex Connection** — Server URL and token, with a connection test
- **Users** — Per-account filtering and category levels (above)
- **Playback & Enforcement** — front door status and Plex settings, stream check interval, and the Seek before / Seek after buffers for fallback skipping
- **Scan Scope & Schedule** — Scan window, *Rescan Titles*, hover previews, scanner workers and hardware decoding, content-rating filter, library exclusions
- **NudeNet Detection** — Model (320n / 640m), confidence threshold, scan frame interval, detector labels, per-label thresholds
- **SigLIP Fallback** — Enable it, threshold, per-label thresholds, consecutive frames, end-credits handling, the false-positive verifier
- **Segments** — Merge gap, refinement step (with how many older segments still wait for refinement), minimum hits
- **Hayaa** — Share segments with other Cleanplex servers through the Hayaa service (below)
- Profanity wordlist and log level are stored as settings (`profanity_wordlist`, `log_level`)

## Hayaa

Hayaa is a shared segment service: Cleanplex servers share what their scans found, and what their users confirmed or rejected, so a title scanned once helps everyone with the same file. Only time ranges, labels and votes are sent, keyed by a fingerprint of the file; titles, file names, frames, subtitle words and your preferences (category levels, thresholds) never leave your server, and neither do segments imported from skip files.

**Signing in.** Settings → Hayaa takes the service and sign-in URLs (https), then **Sign in to Hayaa** shows a short code: open the link on any phone or computer, sign in with a passkey or an emailed code, confirm your Plex account once (it must own a Plex server and be at least 90 days old; one Hayaa account per Plex account and per server), and confirm the code. New accounts beyond Hayaa's daily limit join a waitlist and get in on a later day by signing in again. Cleanplex registers its own signing key with your account. The key and sign-in token stay in `hayaa/` in the data directory, never in the database or its backups. **Unlink** revokes this server; you can also unlink it from the Hayaa account page.

**Syncing.** **Sync with Hayaa** sends what changed since the last sync and fetches updated titles. Turn on **Sync automatically** to do this every few hours (3 to 48); automatic syncs use at most four fifths of Hayaa's daily quota, so a manual sync always fits. When Hayaa asks to wait (busy or over quota), syncing pauses until the time it gives.

**Deleted files.** A finished scan remembers the file's fingerprint, so you can delete a file you only downloaded to scan and its scan is still shared on the next sync. Such titles are upload-only: nothing comes back for a file you no longer have. Files deleted before this change have no fingerprint and cannot be shared.

**What you get back, and when.** Hayaa guards its data against people who would use it to cut scenes out of films. It returns segments only for files this server has scanned itself, after Hayaa has checked that the file is really here (Cleanplex answers by reading a few small parts of the file), and only once your account is 14 days old with 25 titles scanned. Each day's titles count against a viewing allowance (about 8 hours of runtime a day), long films arrive a part at a time at about playback speed, and times are rounded outward by a few seconds. The sync summary says how many titles were held back and why; they come on later syncs.

**What your actions share.** Marking a segment a false positive is a vote that nothing is there; to stop skipping a kind of scene, lower the viewer's category level instead (nothing is shared). Deleting your own segment withdraws your support; a relabel is a label vote. Deleting a segment that came from Hayaa only hides it here, for good.

**How shared segments are applied.** Nothing from Hayaa acts until it passes these checks:

| Preset | Applies |
|---|---|
| Balanced (default) | Segments other users agree on; unconfirmed ones wait for a re-check on this server |
| Cautious | Also unconfirmed segments; ignores segments the community rejected |
| Verify first | Only what this server re-checked and found itself |

Re-checks run in the scan window and look only at the shared segment's few seconds, with your own detectors and thresholds. Shared segments that only a detector found also have to pass your thresholds. A segment of yours the community rejected is hidden (low and medium severity) and listed under **Community false positives**, with Restore. Unconfirmed ones wait under **Waiting for review**: Keep applies one and vouches for it, Dismiss hides it for good. If this server's re-checks keep disagreeing with shared segments, the preset switches itself to Verify first and says so.

Only titles made of a single file are synced.

## AI Agent (MCP) Endpoint

`POST /mcp` speaks the Model Context Protocol (Streamable HTTP). Point an MCP client at `http://your-server:7979/mcp` to list libraries and titles, read segments, check scanner status, queue a scan, delete segments and read settings. Like the rest of the API it has no login (see [Security](#security)).

## Stream Filtering for Every Client (front door)

Plex's 2026 apps (iPhone, iPad, Apple TV) and Plex Web accept no remote commands, so live skipping cannot reach them. The **front door** filters every client instead, in or out of the house: Cleanplex sits in front of Plex's port and removes the filtered ranges from the stream itself.

How it works:
- A firewall redirect sends Plex's port 32400 to an nginx front on port 32480. nginx uses Plex's own `plex.direct` certificate, so encrypted connections keep working, and streams video straight from Plex. Plex stays on `127.0.0.1:32400`, untouched.
- For a viewer whose filtering is on, playing a title with segments their levels skip, Cleanplex refuses direct play, asks Plex for an HLS stream and removes the segments covering those ranges from the playlist (and re-times subtitles). When the file's keyframes are indexed (a background job does this for flagged titles) the video is **copied, not re-encoded**, so 4K, HDR and Dolby Vision are kept; cuts then widen to whole segments (up to ~10 s). Otherwise Plex re-encodes, with ~1 s precision.
- **Fail closed**: the original file, downloads and DASH/progressive streams of such a title are refused for that viewer.
- **Backstop**: a filtered viewer's stream of a flagged title that did not pass the front door (Plex Relay, DLNA) is stopped with a message.
- Everyone else, and unflagged titles, play exactly as before (direct play included).
- Each cut a viewer plays across is recorded as a skip in the skip history.

Install (macOS with pf, or Linux with nftables; needs nginx with the stream module):

```bash
sudo bash scripts/frontdoor/install.sh            # macOS default account: server; Linux: plex (--user to change)
sudo bash scripts/frontdoor/install.sh --uninstall
```

- **Docker**: see [Docker](#docker-nas-unraid-synology-qnap-truenas-any-docker-host); the image sets this up itself.
- **Windows** (Mode B, untested): `scripts/frontdoor/install-windows.ps1` runs the front door on 32480, prints the URL to set as Plex's *Custom server access URL*, and blocks other machines from 32400 so clients fail over to it.

Settings → **Stream Filtering**:

| Setting | Default | Notes |
|---|---|---|
| Mute segments in filtered streams | Leave | A stream cannot be muted by editing it: profanity is either cut like a skip or left in (`enforcement_mute_mode`: `cut`/`leave`) |
| Stop streams that bypassed the front door | On | Acts only once the front door is installed (`enforcement_enabled`, set by the installer) |
| Plex Relay | — | **Recommended off**: relayed streams never reach the front door. Remote viewers then need Plex's direct remote access (Settings → Remote Access, "mapped"); behind CGNAT without a port forward they lose remote access |
| Plex DLNA | — | Recommended off: DLNA serves files on its own port |
| Simultaneous transcodes | Plex's | Filtered streams of flagged titles are copied or re-encoded by Plex; raise it for several viewers at once |

Limits: the viewer sees a shortened timeline (progress and resume are mapped back for Plex). Scrub-bar preview thumbnails are not filtered (a deliberate choice: a small leak, and previews keep working). Offline downloads made before the front door are unfiltered.

## Data, Backups & Maintenance

Everything lives in the data folder (`~/.cleanplex`, or `CLEANPLEX_DATA`):

| Path | What | Housekeeping |
|---|---|---|
| `cleanplex.db` | The database (SQLite, WAL) | A consistent backup is written to `backups/` once a day (5 minutes after startup if none is under a day old); the newest 7 are kept. Manual `cleanplex.db.bak-*` copies are trimmed to the newest 3 |
| `thumbnails/`, `hover/`, `feedback/` | Segment thumbnails, hover previews, false-positive frames | Removed with their segment; a sweep at startup and every 6 hours removes unreferenced files |
| `posters/` | Cached Plex posters | Refreshed after 7 days; the sweep deletes ones untouched for 30 days |
| `previews/` | On-demand segment preview clips | The sweep deletes clips older than 14 days and the oldest first once the folder passes 500 MB; they are re-rendered when played |
| `models/` | NudeNet 640m, SigLIP and the verifier | Kept |
| `cleanplex.log` | Log, only when `CLEANPLEX_LOG_FILE` is set | Rotated at 10 MB, 4 backups kept |

New titles are discovered by a library pass every 5 minutes (one chunked lookup per library, not one per title). Restore a backup by stopping the service and copying a file from `backups/` over `cleanplex.db`.

## Client Compatibility

The seek command is sent via the Plex Player Control API and works with most modern Plex clients:

| Client | Supported |
|---|---|
| Plex Web (browser) | ❌ for live skipping (tested 2026-11-05: Plex answers its seek with 404). ✅ with the front door, see [Stream Filtering](#stream-filtering-for-every-client-front-door) |
| Plex for iOS / Android | ❌ for live skipping on Plex for iOS 2026.20 (no remote-control port). Use the front door |
| Plex HTPC | ✅ Fully supported |
| Plex Media Player (desktop) | ✅ Fully supported |
| Apple TV | ❌ for live skipping on Plex for Apple TV 2026.20 (no remote-control port). Use the front door |
| Roku | ⚠️ Limited support |
| Some Smart TV apps | ⚠️ Limited support |

The Dashboard shows a **Controllable** badge per stream so you can see which clients support seeking.

**Remote Device Access:** The app proxies Plex images through its own server, so posters and artwork load correctly on remote clients (not just localhost).

## Scanner Queue & Priority

The queue is stored in the database, so the Dashboard, the Browse page and the scan workers all see the same list, and it survives restarts. Each worker claims the next title from the database in one step, so a title is never scanned twice and a title being scanned is never picked up again.

- **Order** — Movies newest first, then TV shows grouped so a show's episodes run together
- **Force priority** — Titles you "Scan Now" always run before everything else, and may run outside the scan window
- **Self-healing** — Any pending title the scanner is allowed to scan (not ignored, library not excluded, rating in your filter) is queued automatically, whichever way it reached the database

`GET /api/scan/queue` returns the unfinished jobs (finished ones are left out unless you pass `status=`), with `limit`/`offset` paging and per-status `counts`.

When you click **"Scan Now"**:
1. The title gets the force-scan flag and moves to the front of the queue
2. A scanner worker claims it immediately (force titles are claimed first)
3. It scans regardless of the configured scan window time restrictions

## Ignored Titles

Titles can be marked as **ignored** to skip them during background scans:
- Ignored titles are never queued; ignoring a queued title removes it from the queue, and un-ignoring it queues it again
- You can still run a "Scan Now" on an ignored title — it will be scanned regardless
- Browse hides ignored titles and titles whose rating is not in Scan ratings, at every status; titles being scanned or queued stay listed (toggle with "Show ignored and out-of-scope titles" under Filters)

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CLEANPLEX_DATA` | `~/.cleanplex` | Data directory for DB and thumbnails |
| `CLEANPLEX_PORT` | `7979` | Web UI port |
| `CLEANPLEX_HOST` | `0.0.0.0` | Bind address. The API has no login, so use `127.0.0.1` behind a reverse proxy if the network is not trusted |
| `CLEANPLEX_LOG_FILE` | none | Write the log to this file, rotated at 10 MB with 4 backups, instead of stdout. The macOS install script sets it to `~/.cleanplex/cleanplex.log` |
| `CLEANPLEX_NICE` | `10` | How much to lower the service's CPU priority at startup, so Plex's transcoder wins when the scanner is busy. `0` leaves the priority alone |
| `CLEANPLEX_CORS_ORIGINS` | none | Comma-separated origins allowed to call the API from another site; same-origin use needs nothing |
| `CLEANPLEX_PROXY_PORT` | `7981` | Port of the stream proxy the front door talks to; `0` turns it off |
| `CLEANPLEX_PROXY_HOST` | `CLEANPLEX_HOST` | Bind address of the stream proxy. The service installers set `127.0.0.1`: only nginx needs it |
| `CLEANPLEX_PLEX_DATA` | per OS | The folder that contains `Library/Application Support/Plex Media Server` (a Plex Docker volume's `/config`), for a layout the front door cannot find itself |
| `CLEANPLEX_PLEX_UPSTREAM` | `127.0.0.1:32400` | `host:port` the front door and Cleanplex reach Plex at (Plex in another container) |
| `CLEANPLEX_PLEX_URL` / `CLEANPLEX_PLEX_TOKEN` | from Plex's folder | Docker: set the Plex URL and token without the web UI; they override saved values on every start |
| `CLEANPLEX_FRONTDOOR` | `1` | Docker: `0` runs without the front door (no nginx, no stream filtering) |
| `CLEANPLEX_CONFIGURE_PLEX` | `1` | Docker: switch Plex Relay and DLNA off on the first start |
| `CLEANPLEX_REDIRECT` | — | Docker host-network mode: `nft` adds the 32400 to 32480 redirect |
| `PUID` / `PGID` | owner of Plex's folder | Docker: user and group the container runs as |
| `CLEANPLEX_PLEX_MACHINE_ID` | from Plex | Plex's `ProcessedMachineIdentifier`, if it cannot be read from Plex's preferences |
| `CLEANPLEX_NGINX` | `nginx` | nginx binary used to reload the front door when Plex renews its certificate |

## Troubleshooting

**Posters not loading on remote devices?**
- The app automatically proxies all Plex images to work around localhost URL issues. Try hard-refreshing the browser (Ctrl+F5).

**"Scan Now" not prioritizing titles?**
- Run the latest version.

**Detector labels not filtering correctly?**
- Ensure the labels are selected in Settings → Detector Labels. Previously stored segments are filtered at API response time to respect your current settings.

**Too many false positives?**
- See [Tuning accuracy](#tuning-accuracy): per-label thresholds, then the verifier.

**Scans are slow or the machine is busy?**
- Raise the Scan Frame Interval, lower Scanner Workers, keep hardware decoding on, and keep SigLIP off if you do not need it. The service already runs at lower CPU priority (`CLEANPLEX_NICE`).

**Plex says the server is unavailable after installing the front door?**
- Remove it at once: `sudo bash scripts/frontdoor/install.sh --uninstall` (Windows: `install-windows.ps1 -Uninstall`). Clients reach Plex directly again within seconds.
- Then check that nginx answers on `http://<server>:32480/identity` *from another device*, and look at `~/.cleanplex/frontdoor/cleanplex-nginx-error.log`. Requests from the server itself take a different path and can succeed while other devices fail.

**A filtered title shows an error instead of playing?**
- The front door refuses what it cannot filter: the original file, downloads and non-HLS streams of a title with segments for that viewer. Playing it again from the app gets the filtered stream.

**Some rated titles still appear in the library?**
- Check Settings → Content Ratings. The filter uses exact matching (e.g., "PG" ≠ "PG-13"). "Unrated" is an explicit checkbox option, not the same as empty rating.

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
