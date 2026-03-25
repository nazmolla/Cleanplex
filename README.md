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

All configuration is done via the **web UI** at `http://your-server:7979/settings`. No config files to edit.

| Setting | Default | Description |
|---|---|---|
| Plex Server URL | — | e.g. `http://192.168.1.10:32400` |
| Plex Token | — | Your Plex authentication token |
| Poll Interval | 5s | How often to check active streams |
| Confidence Threshold | 0.6 | NudeNet score threshold (0–1). Lower = more sensitive. |
| Skip Buffer | 3000ms | Extra milliseconds to seek past the end of segments (in addition to the 5-second post-segment buffer) |
| Scan Window | 23:00–06:00 | Time window when background scanning runs |
| Scanner Workers | 2 | Number of parallel scans (higher = more CPU/memory usage) |
| Detector Labels | — | Select which nudity types to detect (and skip). Only selected labels trigger skips |
| Content Ratings | — | Only scan and filter titles matching your selected ratings |

## Segment Expansion & Skip Logic

**Default behavior:** When a nudity segment is detected (e.g., 30s–60s), Cleanplex automatically:
1. **Expands the segment by ±5 seconds** in the database → 25s–65s (catches any missed leading/trailing nudity)
2. **Monitors with 5s lookahead** — the filter triggers at 20s (5s before the expanded start) to account for polling latency
3. **Skips to 25s** and holds the block until playback reaches 65s

This ensures no inappropriate content is shown, even if the detector's boundaries were slightly off.

## Finding Your Plex Token

1. Sign in to Plex Web
2. Browse to any media item
3. Click the three-dot menu → **Get Info** → **View XML**
4. The URL will contain `?X-Plex-Token=XXXXXX`

## Web UI Pages

#### Dashboard
- **Active streams** — Shows all users currently watching, with playback position and controllability status
- **Scanner status** — Number of workers active, queue size, and which titles are currently being scanned
- **Recent skip events** — Log of the last 50 skips with timestamp, title, user, and client

#### Library
- **Browse all titles** with scan status icons
- **Trigger scans** per title or entire library
- **"Scan Now"** to prioritize a title — moves it to the front of the scanning queue for immediate processing
- **Sort & filter** — filters by ratings and segment count; defaults to hiding ignored titles and sorting by date added (newest first)
- **Scan completion timestamps** — shows when each title finished scanning

#### Segments
- **Three-panel browser**: Library tree → Select title → Browse its segments
- **Live scanner banner** — shows which titles are currently being scanned with progress bars
- **Segment details** — Timestamp range, confidence score, and a thumbnail from the detected scene
- **Delete false positives** — Remove incorrectly flagged segments
- **Preview video** — In-app player to review the segment before deletion
- **Jump to segment** — Send Plex seek command to that spot in the video
- **Scan completion info** — Timestamp when the title finished scanning

#### Users
- **Toggle filtering per Plex account** — Turn skipping on/off for specific user accounts

#### Settings
- **Plex connection** — Server URL and authentication token
- **Scanner tuning** — Frame extraction interval, confidence threshold, parallel workers
- **Rating filter** — Only scan titles matching your selected content ratings (exact match, "Unrated" is explicit)
- **Detector labels** — Checkboxes to select which nudity types trigger skips (e.g., "female genitalia", "male genitalia", "breast", "butt", "anus")
- **Skip behavior** — Extra buffer to add after segments, scan window, segment merge gap, minimum hits per segment

## Client Compatibility

The seek command is sent via the Plex Player Control API and works with most modern Plex clients:

| Client | Supported |
|---|---|
| Plex Web | ✅ Fully supported |
| Plex for iOS / Android | ✅ Fully supported |
| Plex HTPC | ✅ Fully supported |
| Plex Media Player (desktop) | ✅ Fully supported |
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
| `CLEANPLEX_HOST` | `0.0.0.0` | Bind address |

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
