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

## Configuration

All configuration is done via the **web UI** at `http://your-server:7979/settings`, grouped into sections (Plex Connection, Users, Playback, Scan Scope & Schedule, NudeNet Detection, SigLIP Fallback, Segments, Sharing). No config files to edit.

| Setting | Default | Description |
|---|---|---|
| Plex Server URL | — | e.g. `http://192.168.1.10:32400` |
| Plex Token | — | Your Plex authentication token |
| Poll Interval | 5s | How often to check active streams |
| Confidence Threshold | 0.6 | NudeNet score threshold (0–1). Lower = more sensitive. |
| NudeNet Model | 320n | `320n` is bundled and fastest; `640m` is larger and more accurate, downloaded once on first use (or pre-download it in the same section) |
| Scan Frame Interval | 5000 ms | How often a frame is sampled while scanning. Lower catches shorter scenes but scans slower |
| Hardware video decoding | on | Use the machine's video decoder for scans when it has one (much faster on 4K HEVC); a file it cannot decode falls back to software |
| Per-Label Thresholds | blank | Optional per-label NudeNet threshold (NudeNet Detection section). A blank label uses the Confidence Threshold. Stored in `label_thresholds` |
| Pre / Post Buffer | 3000 ms each | How far a segment is widened before its start and after its end (`pre_buffer_ms`, `post_buffer_ms`), so the flagged content is never briefly shown and the filter does not re-trigger on the tail |
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

**False-positive verifier** (off by default) is a small filter trained on *your* verdicts. Frames you marked as false positives are compared with frames of segments you kept, using SigLIP's image embedding, and a per-detector classifier (NudeNet hits and SigLIP hits separately) learns what your rejects look like. At scan time a flagged segment whose peak frame looks like them is **rejected**: stored inactive, with its frame, under Analytics → False positives (filter *Rejected by the verifier*), where the undo button restores a wrong rejection. Setup: mark false positives as you review, then Settings → SigLIP Fallback → *Train* (needs at least 30 false positives and 30 kept segments per detector and SigLIP's models), pick a level, save, and rescan (*Rescan Titles → scanned by an older scanner*). The level is how many good segments you accept losing: **5% Gentle, 10% Balanced, 20% Strict**. Retrain after you have marked more.

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

Each user gets a 0–3 level per category. A segment fires when *level + severity* exceeds 3, so
level 3 filters everything in that category and level 0 filters nothing. Every category starts
at level 3 when filtering is on; lowering or turning one off is always an explicit choice, and
changing one category never affects the others.

**Scanner segments get their severity from the detected label**, so a level has a concrete meaning
for nudity:

| Detected | Severity | Skipped at level |
|---|---|---|
| Exposed genitalia, anus | high | 1, 2, 3 |
| Exposed breast, exposed buttocks | medium | 2, 3 |
| Covered body parts, belly, feet, faces | low | 3 |

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
| Kodi / MPlayer EDL | `.edl` | `start end action`; 0 cut, 1 mute, 2 scene marker, 3 commercial. Also exported |
| MovieContentFilter | `.mcf` | Spec 1.1.0, ~130 hierarchical categories. Also exported |
| Pasted list | `.txt` | Loose `00:12:30 - 00:13:05 nudity` lines, for lists that have no machine format |

Two ways in:

- **Sidecar** — drop `movie.skp` (or `.edl` / `.mcf`) next to `movie.mkv`. It is imported when the
  title is first discovered and the scan is skipped entirely.
- **Library page** — expand a title and use the import box to upload a file or paste a list.

Imported timings are authored against a specific cut of a film. Cleanplex checks them against the
title's runtime and warns when they do not line up, since an extended edition will throw every
timestamp out.

Plex itself does not read `.edl` files, so EDL/MCF export is for Kodi, Jellyfin and mpv.

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

#### Library
- **Import skip files** — Upload `.skp` / `.edl` / `.mcf`, paste a timestamp list, or export segments as EDL/MCF
- **Browse all titles** with scan status icons
- **Trigger scans** per title or entire library
- **"Scan Now"** to prioritize a title — moves it to the front of the scanning queue for immediate processing
- **Sort & filter** — filters by ratings and segment count; defaults to hiding ignored titles and sorting by date added (newest first)
- **Scan completion timestamps** — shows when each title finished scanning

#### Segments
- **Three-panel browser**: Library tree → Select title → Browse its segments
- **Live scanner banner** — shows which titles are currently being scanned with progress bars
- **Segment details** — Timestamp range, confidence score, a thumbnail from the detected scene, and what found it (e.g. *NudeNet 640m*, *SigLIP*, *Subtitles*, *VideoSkip (.skp)*)
- **Delete false positives** — Remove incorrectly flagged segments
- **File cleanup** — Deleting a segment (or a title's segments, a rescan, or an import that replaces them) removes its thumbnail and hover preview. A sweep at startup and every 6 hours removes thumbnails, hover previews and feedback frames nothing references (older than an hour), and segments whose title no longer exists once they are 30 days old. The preview clip and poster caches are pruned by age and size (see *Data, Backups & Maintenance*). Feedback frames kept for false-positive and relabel verdicts are never removed
- **Right-click to rescan** — On a movie, episode, season or show: *Rescan now* or *Rescan tonight* (queued for the scan window). The Segments page lists only titles that have segments, so on a show the first pair rescans those listed episodes; *Rescan whole show now / tonight* and *whole season now / tonight* queue every non-ignored episode, including ones with no segments; right-clicking a library offers *Rescan whole library tonight* (finished and failed titles, scheduled only, never jumped to the front)
- **Failed scans** — On the Library page, the *Failed* tab has *Retry failed*: every failed title of the library is queued again (inside your rating filter), except titles whose media file is still missing, which stay on the list and are counted; ignored titles are taken off the Failed list. The same choice is in Settings → *Rescan Titles*
- **Rescan many titles** — Settings → Scan Scope & Schedule → *Rescan Titles*: pick titles scanned by an older scanner (the right choice after an update), titles with segments, every scanned title, or titles scanned before a date, optionally for one library. A live count shows what will be queued and how many your rating filters skip; they go through the normal scan queue (scan window, usual order) and keep their current segments until the new scan replaces them
- **Live updates** — The open title's segments and the tree counts refresh as a rescan runs and when it finishes
- **Hover preview** — Hovering a segment's thumbnail plays a 5 s silent loop of it (sped up for long segments), in Library, Segments and Analytics. Rendered overnight by the hover-preview job, so new segments get one the next night
- **Preview video** — In-app player; the server cuts a short H.264/AAC clip (5 s either side) so it plays in any browser, reading the file as the service account. *Open in VLC* starts and stops at the segment
- **Jump to segment** — Send Plex seek command to that spot in the video
- **Scan completion info** — Timestamp when the title finished scanning

#### Markers
- Lists the titles of a library with their **Plex intro and credits markers**, and lets you create, edit and delete them (and refresh them from Plex). A title's credits marker is what stops SigLIP from reading end credits as nudity

#### Analytics
Tabs (linkable with `?tab=`), and sections that can be minimised (remembered per browser):
- **Detections** — Segments per label (click a bar for a per-rating breakdown), a label filter, and the matching segments with preview, jump, false-positive and delete actions
- **Skips** — Skip and mute history by category, client and title
- **Accuracy** — How often each detector and label was marked a false positive by you (verifier rejections are counted separately and left out of the rate), with mean scores of kept vs. marked segments
- **False positives** — Every inactive segment, newest first, with an undo button: the ones you marked and the ones the verifier rejected (filter *Flagged by you / Rejected by the verifier*, each tagged)

#### Users (under Settings)
- **Toggle filtering per Plex account** — Turn skipping on/off for specific user accounts
- **Category preferences** — Expand a user to set a 0–3 strictness level per content category, with an optional skip/mute override

#### Settings
- **Plex Connection** — Server URL and token, with a connection test
- **Users** — Per-account filtering and category levels (above)
- **Playback** — Poll interval. (The *Skip Buffer* field does not currently change playback; the pre/post buffers are `pre_buffer_ms` and `post_buffer_ms` in the settings API, see issue #170)
- **Scan Scope & Schedule** — Scan window, *Rescan Titles*, hover previews, scanner workers and hardware decoding, content-rating filter, library exclusions
- **NudeNet Detection** — Model (320n / 640m), confidence threshold, scan frame interval, detector labels, per-label thresholds
- **SigLIP Fallback** — Enable it, threshold, per-label thresholds, consecutive frames, end-credits handling, the false-positive verifier
- **Segments** — Merge gap, refinement step (with how many older segments still wait for refinement), minimum hits
- **Sharing** — Optional crowdsourced segment library (below)
- Profanity wordlist and log level are stored as settings (`profanity_wordlist`, `log_level`)

## Segment Library Sharing

Optional and **always manual**: Settings → Sharing connects to one shared GitHub repository where users publish detections keyed by a hash of the media file. *Upload* sends your segments (labelled with your instance name); *Download* fetches other instances' segments for titles you have, merging them by consensus: segments agreed by at least *verified threshold* instances (2 by default) within *timing tolerance* (2 s) count as verified, and a conflict-resolution mode decides disagreements. Nothing syncs in the background.

## AI Agent (MCP) Endpoint

`POST /mcp` speaks the Model Context Protocol (Streamable HTTP). Point an MCP client at `http://your-server:7979/mcp` to list libraries and titles, read segments, check scanner status, queue a scan, delete segments and read settings. Like the rest of the API it has no login (see [Security](#security)).

## Experimental: Stream Proxy

`cleanplex/stream_proxy.py` is a standalone prototype (`python -m cleanplex.stream_proxy`, issue #132) that sits in front of Plex and, for clients that cannot be skipped live, serves an HLS playlist with filtered ranges removed. It is not part of the service, and it has not been verified on real Plex clients.

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
| Plex Web | ✅ Fully supported |
| Plex for iOS / Android | ✅ Fully supported |
| Plex HTPC | ✅ Fully supported |
| Plex Media Player (desktop) | ✅ Fully supported |
| Apple TV | ✅ Fully supported |
| Roku | ⚠️ Limited support |
| Some Smart TV apps | ⚠️ Limited support |

The Dashboard shows a **Controllable** badge per stream so you can see which clients support seeking.

**Remote Device Access:** The app proxies Plex images through its own server, so posters and artwork load correctly on remote clients (not just localhost).

## Scanner Queue & Priority

The scanner maintains two independent queues:

- **Normal queue** — Regular background scans of newly added titles, processed in order (FIFO)
- **Force queue** — High-priority scans from "Scan Now" clicks, always processed before normal queue items

`GET /api/scan/queue` returns the unfinished jobs (finished ones are left out unless you pass `status=`), with `limit`/`offset` paging and per-status `counts`.

When you click **"Scan Now"**:
1. Title is removed from the normal queue (if present) to avoid duplicate processing
2. Added to the force queue with the force-scan flag set in the database
3. A scanner worker picks it up immediately (workers check force queue first)
4. Scans regardless of the configured scan window time restrictions

## Ignored Titles

Titles can be marked as **ignored** to skip them during background scans:
- Ignored titles are still queued but the scanner immediately skips them
- You can still run a "Scan Now" on an ignored title — it will be scanned regardless
- The Library view defaults to hiding ignored titles (toggle with the "Show Ignored" checkbox)

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CLEANPLEX_DATA` | `~/.cleanplex` | Data directory for DB and thumbnails |
| `CLEANPLEX_PORT` | `7979` | Web UI port |
| `CLEANPLEX_HOST` | `0.0.0.0` | Bind address. The API has no login, so use `127.0.0.1` behind a reverse proxy if the network is not trusted |
| `CLEANPLEX_LOG_FILE` | none | Write the log to this file, rotated at 10 MB with 4 backups, instead of stdout. The macOS install script sets it to `~/.cleanplex/cleanplex.log` |
| `CLEANPLEX_NICE` | `10` | How much to lower the service's CPU priority at startup, so Plex's transcoder wins when the scanner is busy. `0` leaves the priority alone |
| `CLEANPLEX_CORS_ORIGINS` | none | Comma-separated origins allowed to call the API from another site; same-origin use needs nothing |

## Troubleshooting

**Posters not loading on remote devices?**
- The app automatically proxies all Plex images to work around localhost URL issues. Try hard-refreshing the browser (Ctrl+F5).

**"Scan Now" not prioritizing titles?**
- Run the latest version. Recent fixes ensure force-scanned titles are actually moved to the priority queue.

**Detector labels not filtering correctly?**
- Ensure the labels are selected in Settings → Detector Labels. Previously stored segments are filtered at API response time to respect your current settings.

**Too many false positives?**
- See [Tuning accuracy](#tuning-accuracy): per-label thresholds, then the verifier.

**Scans are slow or the machine is busy?**
- Raise the Scan Frame Interval, lower Scanner Workers, keep hardware decoding on, and keep SigLIP off if you do not need it. The service already runs at lower CPU priority (`CLEANPLEX_NICE`).

**Some rated titles still appear in the library?**
- Check Settings → Content Ratings. The filter uses exact matching (e.g., "PG" ≠ "PG-13"). "Unrated" is an explicit checkbox option, not the same as empty rating.

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
