# Configuration

All configuration is done via the **web UI** at `http://your-server:7979/settings`, grouped into sections (Plex Connection, Users, Playback, Stream Filtering, Scan Scope & Schedule, NudeNet Detection, SigLIP Fallback, Segments, Hayaa). No config files to edit.

| Setting | Default | Description |
|---|---|---|
| Plex Server URL | — | e.g. `http://192.168.1.10:32400` |
| Plex Token | — | Your Plex authentication token |
| Hayaa service URL | — | Hayaa sync API, `https` (plain `http` only to this machine, for Hayaa's dev server) |
| Hayaa sign-in URL | — | Hayaa's sign-in (OAuth) host, `https` |
| Stream check interval | `5s` | Longest wait between checks of Plex's active streams: stops filtered streams that bypassed the front door, updates the Dashboard, and drives fallback skipping (checks tighten near a segment) |
| Confidence Threshold | `0.6` | NudeNet score threshold (`0–1`). Lower = more sensitive. |
| NudeNet Model | `320n` | `320n` is bundled and fastest; `640m` is larger and more accurate, downloaded once on first use (or pre-download it in the same section) |
| Scan Frame Interval | `5000 ms` | How often a frame is sampled while scanning. Lower catches shorter scenes but scans slower |
| Hardware video decoding | `on` | Use the machine's video decoder for scans when it has one (much faster on 4K HEVC); a file it cannot decode falls back to software |
| Per-Label Thresholds | blank | Optional per-label NudeNet threshold (NudeNet Detection section). A blank label uses the Confidence Threshold. Stored in `label_thresholds` |
| Seek before / Seek after | `3000 ms` each | Fallback skipping only: how far a segment is widened before its start and after its end (`pre_buffer_ms`, `post_buffer_ms`), so the flagged content is never briefly shown and the filter does not re-trigger on the tail. Streams the front door edits are cut at the exact segment edges |
| Profanity Wordlist | built-in | JSON array of words the subtitle scanner mutes. Empty uses the bundled list |
| Skip Event Retention | `90 days` | How long skip history is kept before being pruned at startup |
| Scan Window | `23:00–06:00` | Time window when background scanning runs; *Scan now* ignores it |
| Library Exclusions | none | Libraries that are not scanned or synced |
| Hover Previews | `on` | Render a short silent loop of each active segment, played when you hover its thumbnail (~20–40 KB each, stored in `~/.cleanplex/hover`) |
| Hover previews when idle | `on` | Also render hover previews as soon as the scan queue is empty and nothing is scanning, not only inside the window below; stops when a scan starts |
| Hover Preview Window | `00:00–07:00` | When the hover preview job runs (Settings → Schedule). It renders one segment at a time, picks up segments it has not done yet, and removes files of deleted segments |
| Scanner Workers | `2` | Number of parallel scans (higher = more CPU/memory usage) |
| Detector Labels | — | Select which nudity types to detect (and skip). Only selected labels trigger skips |
| SigLIP Fallback | `off` | Second, whole-frame check that runs only on frames NudeNet did not flag, to catch scenes it misses. Follows Detector Labels. Downloads ~815 MB of model files on first use. Its segments have source `siglip`, and their severity follows the label they are tagged with |
| SigLIP Threshold | `0.3` | Share (`0–1`) of a frame's match that must go to enabled-label descriptions. Separate from Confidence Threshold. Higher = fewer, surer hits |
| Per-label SigLIP thresholds | blank | Optional threshold per label; a SigLIP hit is judged by the label it is tagged with. Blank uses the SigLIP Threshold. Stored in `siglip_label_thresholds` |
| False-positive verifier | `off` | Filters detections that look like ones you rejected; train it and pick a level (5/10/20% of good segments you accept losing) in Settings → SigLIP Fallback. Stored in `verifier_loss_pct`, model in `models/verifier.npz`. See [Detection Pipeline & Accuracy](../detection/pipeline-and-accuracy.md) |
| Ignore end credits | `on` | SigLIP is not run on frames at or after the title's Plex credits marker, since text-heavy credit screens are read as nudity. NudeNet still runs. No marker, no effect |
| Inference threads | `0` (auto) | `onnxruntime` threads per scan worker (`inference_threads`, API only). Auto leaves about one worker's share of CPU free for Plex and the web UI |
| SigLIP Consecutive Frames | `2` | SigLIP-only hits must repeat on this many consecutive sampled frames before a segment is created |
| Segment Merge Gap | `12000 ms` | Flagged frames closer than this are merged into one segment |
| Minimum Hits Per Segment | `1` | Flagged frames a cluster needs to be kept. Raise it to cut false positives |
| Refinement Step | `500 ms` | After a segment is found, its window is re-sampled this finely so the thumbnail is the highest-scoring frame and the segment also covers scene frames the coarse scan stepped over. Segments only ever grow; `0` turns it off. The card shows the time the thumbnail frame was taken at (Settings → Segments). Segments found before this existed are refined by a background job inside the scan window, one at a time (no rescan needed); Settings → Segments shows how many are still waiting |
| Content Ratings | — | Only scan and filter titles matching your selected ratings. `NR` also covers titles Plex reports as "Not Rated" or "Unrated" |

For deployment-level options (data directory, port, bind address, Docker settings), see [Environment Variables](../reference/environment-variables.md). For stream-filtering settings, see [Stream Filtering (Front Door)](../playback/front-door.md#settings).
