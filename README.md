# Cleanplex

A home server service that monitors your Plex instance for active streams and automatically skips past inappropriate (nudity/sexual) content for configured user accounts.

## How It Works

1. **Pre-scan** — When videos are added to your Plex library, Cleanplex queues them for background analysis. It extracts one frame every 10 seconds using `ffmpeg` and runs each frame through [NudeNet](https://github.com/notAI-tech/NudeNet) (a lightweight, CPU-friendly ONNX model that runs fully locally). Flagged frames are clustered into skip segments stored in a local SQLite database. A 2-hour movie takes ~20–30 minutes to scan on a Raspberry Pi 4.

2. **Real-time monitoring** — While someone watches, the service polls Plex every few seconds. If a filtered user's playback position enters a flagged segment, it sends a seek command to jump past it. This is just a database lookup — no ML at playback time.

3. **Web UI** — Full browser dashboard at `http://your-server:7979` to configure everything.

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
| Skip Buffer | 3000ms | Extra time to seek past the end of a segment |
| Scan Window | 23:00–06:00 | Time window when background scanning runs |

## Finding Your Plex Token

1. Sign in to Plex Web
2. Browse to any media item
3. Click the three-dot menu → **Get Info** → **View XML**
4. The URL will contain `?X-Plex-Token=XXXXXX`

## Web UI Pages

- **Dashboard** — Live active streams with playback position, controllability status, and recent skip events
- **Library** — Browse all Plex titles with scan status. Trigger scans per title or entire library. Schedule for tonight or run immediately.
- **Segments** — Three-panel browser: Library → Title → Segments. Each segment shows the flagged video frame thumbnail so you can delete false positives.
- **Users** — Toggle filtering on/off per Plex account
- **Settings** — Plex connection, filter tuning, scan schedule

## Client Compatibility

The seek command is sent via the Plex Player Control API and works with most modern Plex clients:

| Client | Supported |
|---|---|
| Plex Web | ✅ |
| Plex for iOS / Android | ✅ |
| Plex HTPC | ✅ |
| Plex Media Player (desktop) | ✅ |
| Roku | ⚠️ Limited |
| Some Smart TV apps | ⚠️ Limited |

The Dashboard shows a **Controllable** badge per stream so you can see which clients are covered.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CLEANPLEX_DATA` | `~/.cleanplex` | Data directory for DB and thumbnails |
| `CLEANPLEX_PORT` | `7979` | Web UI port |
| `CLEANPLEX_HOST` | `0.0.0.0` | Bind address |

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
