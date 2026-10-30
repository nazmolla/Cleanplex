# Cleanplex

A home server service that monitors your Plex instance for active streams and automatically skips past inappropriate (nudity/sexual) content for configured user accounts.

## How It Works

### 1. Pre-scan (Background Analysis)
When videos are added to your Plex library, Cleanplex queues them for background analysis:
- Extracts frames from the video every 10 seconds using `ffmpeg`
- Runs each frame through [NudeNet](https://github.com/notAI-tech/NudeNet) (lightweight, CPU-friendly ONNX model running fully locally)
- Each detected scene is **expanded by 5 seconds before and after** to catch any leading/trailing content the detector might have missed
- Flagged frames are clustered into skip segments and stored in a local SQLite database
- A 2-hour movie takes ~20–30 minutes to scan on a Raspberry Pi 4

### 2. Real-time Playback Monitoring
While someone watches, the service polls Plex every few seconds:
- Checks if a filtered user's playback position enters a flagged segment
- The monitor uses a **5-second lookahead** before each segment start to compensate for polling latency
- When triggered, automatically sends a seek command to jump **past the entire segment** (including the 5-second buffers)
- This is just a database lookup — no ML at playback time

### 3. Web UI Dashboard

Every page updates live: the server pushes changes (scans starting, progressing and finishing, new segments, titles synced from Plex, skips, active streams, user and marker changes) over Server-Sent Events, so nothing needs a manual refresh.
Full browser interface at `http://your-server:7979` for:
- Configuring Plex connection and scan settings
- Reviewing and editing detected segments
- Monitoring active scans with live progress indicators
- Viewing recent skip events

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

### 5. Install as a systemd service (Linux)

```bash
sudo cp cleanplex.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cleanplex
sudo journalctl -u cleanplex -f   # View logs
```

## Configuration

All configuration is done via the **web UI** at `http://your-server:7979/settings`, grouped into sections (Plex Connection, Playback, Scan Scope & Schedule, NudeNet Detection, SigLIP Fallback, Segments, Sharing). No config files to edit.

| Setting | Default | Description |
|---|---|---|
| Plex Server URL | — | e.g. `http://192.168.1.10:32400` |
| Plex Token | — | Your Plex authentication token |
| Poll Interval | 5s | How often to check active streams |
| Confidence Threshold | 0.6 | NudeNet score threshold (0–1). Lower = more sensitive. |
| Per-Label Thresholds | blank | Optional per-label NudeNet threshold (NudeNet Detection section). A blank label uses the Confidence Threshold. Stored in `label_thresholds` |
| Pre Buffer | 3000ms | Milliseconds to start the skip *before* a segment, so the flagged content is never briefly shown |
| Post Buffer | 3000ms | Milliseconds to hold past the end of a segment before the filter can re-trigger |
| Profanity Wordlist | built-in | JSON array of words the subtitle scanner mutes. Empty uses the bundled list |
| Skip Event Retention | 90 days | How long skip history is kept before being pruned at startup |
| Scan Window | 23:00–06:00 | Time window when background scanning runs |
| Hover Previews | on | Render a short silent loop of each active segment, played when you hover its thumbnail (~20–40 KB each, stored in `~/.cleanplex/hover`) |
| Hover previews when idle | on | Also render hover previews as soon as the scan queue is empty and nothing is scanning, not only inside the window below; stops when a scan starts |
| Hover Preview Window | 00:00–07:00 | When the hover preview job runs (Settings → Schedule). It renders one segment at a time, picks up segments it has not done yet, and removes files of deleted segments |
| Scanner Workers | 2 | Number of parallel scans (higher = more CPU/memory usage) |
| Detector Labels | — | Select which nudity types to detect (and skip). Only selected labels trigger skips |
| SigLIP Fallback | off | Second, whole-frame check that runs only on frames NudeNet did not flag, to catch scenes it misses. Follows Detector Labels. Downloads ~815 MB of model files on first use. Its segments have source `siglip`, and their severity follows the label they are tagged with |
| SigLIP Threshold | 0.3 | Share (0–1) of a frame's match that must go to enabled-label descriptions. Separate from Confidence Threshold |
| Per-label SigLIP thresholds | blank | Optional threshold per label; a SigLIP hit is judged by the label it is tagged with. Blank uses the SigLIP Threshold. Stored in `siglip_label_thresholds` |
| False-positive verifier | off | Learns from the segments you marked as false positives and drops new detections whose frame looks like them. Train it in Settings → SigLIP Fallback (needs SigLIP's models and at least 30 false positives and 30 kept segments per detector), then choose how many good segments you accept losing (5, 10 or 20%). Stored in `verifier_loss_pct`; the model file is `models/verifier.npz` in the data folder. Rescan to apply it to titles already scanned. Rejected detections are kept, inactive, under Analytics → False positives (filter: Rejected by the verifier) with their frame, so a wrong rejection can be restored |
| Ignore end credits | on | SigLIP is not run on frames at or after the title's Plex credits marker, since text-heavy credit screens are read as nudity. NudeNet still runs. No marker, no effect |
| `CLEANPLEX_NICE` (environment) | 10 | How much to lower the service's CPU priority at startup, so Plex's transcoder wins when the scanner is busy. `0` leaves the priority alone |
| Inference threads | 0 (auto) | onnxruntime threads per scan worker (`inference_threads`, API only). Auto leaves about one worker's share of CPU free for Plex and the web UI |
| SigLIP Consecutive Frames | 2 | SigLIP-only hits must repeat on this many consecutive sampled frames before a segment is created |
| Refinement Step | 500 ms | After a segment is found, its window is re-sampled this finely so the thumbnail is the highest-scoring frame and the segment also covers scene frames the coarse scan stepped over. Segments only ever grow; 0 turns it off. The card shows the time the thumbnail frame was taken at (Settings → Segments). Segments found before this existed are refined by a background job inside the scan window, one at a time (no rescan needed); Settings → Segments shows how many are still waiting |
| Content Ratings | — | Only scan and filter titles matching your selected ratings. `NR` also covers titles Plex reports as "Not Rated" or "Unrated" |

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
- **File cleanup** — Deleting a segment (or a title's segments, a rescan, or an import that replaces them) removes its thumbnail and hover preview. A sweep at startup and every 6 hours removes thumbnails, hover previews and feedback frames nothing references (older than an hour), and segments whose title no longer exists once they are 30 days old. Feedback frames kept for false-positive and relabel verdicts are never removed
- **Right-click to rescan** — On a movie, episode, season or show: *Rescan now* or *Rescan tonight* (queued for the scan window). The Segments page lists only titles that have segments, so on a show the first pair rescans those listed episodes; *Rescan whole show now / tonight* and *whole season now / tonight* queue every non-ignored episode, including ones with no segments; right-clicking a library offers *Rescan whole library tonight* (finished and failed titles, scheduled only, never jumped to the front)
- **Failed scans** — On the Library page, the *Failed* tab has *Retry failed*: every failed title of the library is queued again (inside your rating filter) and ignored titles are taken off the Failed list. The same choice is in Settings → *Rescan Titles*
- **Rescan many titles** — Settings → Scan Scope & Schedule → *Rescan Titles*: pick titles scanned by an older scanner (the right choice after an update), titles with segments, every scanned title, or titles scanned before a date, optionally for one library. A live count shows what will be queued and how many your rating filters skip; they go through the normal scan queue (scan window, usual order) and keep their current segments until the new scan replaces them
- **Live updates** — The open title's segments and the tree counts refresh as a rescan runs and when it finishes
- **Hover preview** — Hovering a segment's thumbnail plays a 5 s silent loop of it (sped up for long segments), in Library, Segments and Analytics. Rendered overnight by the hover-preview job, so new segments get one the next night
- **Preview video** — In-app player; the server cuts a short H.264/AAC clip (5 s either side) so it plays in any browser, reading the file as the service account. *Open in VLC* starts and stops at the segment
- **Jump to segment** — Send Plex seek command to that spot in the video
- **Scan completion info** — Timestamp when the title finished scanning

#### Analytics
Tabs (linkable with `?tab=`), and sections that can be minimised (remembered per browser):
- **Detections** — Segments per label (click a bar for a per-rating breakdown), a label filter, and the matching segments with preview, jump, false-positive and delete actions
- **Skips** — Skip and mute history by category, client and title
- **Accuracy** — How often each detector and label was marked a false positive, with mean scores of kept vs. marked segments
- **False positives** — Every segment marked as a false positive, newest mark first, with an undo button

#### Users (under Settings)
- **Toggle filtering per Plex account** — Turn skipping on/off for specific user accounts
- **Category preferences** — Expand a user to set a 0–3 strictness level per content category, with an optional skip/mute override

#### Settings
- **Plex connection** — Server URL and authentication token
- **Scanner tuning** — Frame extraction interval, confidence threshold, parallel workers
- **Rating filter** — Only scan titles matching your selected content ratings (exact match, "Unrated" is explicit)
- **Detector labels** — Checkboxes to select which nudity types trigger skips (e.g., "female genitalia", "male genitalia", "breast", "butt", "anus")
- **Skip behavior** — Pre/post buffers around segments, scan window, segment merge gap, minimum hits per segment
- **Profanity wordlist** — Words the subtitle scanner mutes

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
| `CLEANPLEX_CORS_ORIGINS` | none | Comma-separated origins allowed to call the API from another site; same-origin use needs nothing |

## Troubleshooting

**Posters not loading on remote devices?**
- The app automatically proxies all Plex images to work around localhost URL issues. Try hard-refreshing the browser (Ctrl+F5).

**"Scan Now" not prioritizing titles?**
- Run the latest version. Recent fixes ensure force-scanned titles are actually moved to the priority queue.

**Detector labels not filtering correctly?**
- Ensure the labels are selected in Settings → Detector Labels. Previously stored segments are filtered at API response time to respect your current settings.

**Some rated titles still appear in the library?**
- Check Settings → Content Ratings. The filter uses exact matching (e.g., "PG" ≠ "PG-13"). "Unrated" is an explicit checkbox option, not the same as empty rating.

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
