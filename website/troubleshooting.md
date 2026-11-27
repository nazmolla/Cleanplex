# Troubleshooting

**Posters not loading on remote devices?**
: The app automatically proxies all Plex images to work around localhost URL issues. Try hard-refreshing the browser (`Ctrl+F5`).

**"Scan Now" not prioritizing titles?**
: Run the latest version.

**Detector labels not filtering correctly?**
: Ensure the labels are selected in **Settings → Detector Labels**. Previously stored segments are filtered at API response time to respect your current settings.

**Too many false positives?**
: See [Tuning accuracy](detection/pipeline-and-accuracy.md#tuning-accuracy): per-label thresholds, then the verifier.

**Scans are slow or the machine is busy?**
: Raise the **Scan Frame Interval**, lower **Scanner Workers**, keep hardware decoding on, and keep SigLIP off if you do not need it. The service already runs at lower CPU priority (`CLEANPLEX_NICE`).

**Plex says the server is unavailable after installing the front door?**
: Remove it at once: `sudo bash scripts/frontdoor/install.sh --uninstall` (Windows: `install-windows.ps1 -Uninstall`). Clients reach Plex directly again within seconds.
: Then check that `nginx` answers on `http://<server>:32480/identity` *from another device*, and look at `~/.cleanplex/frontdoor/cleanplex-nginx-error.log`. Requests from the server itself take a different path and can succeed while other devices fail.

**A filtered title shows an error instead of playing?**
: The front door refuses what it cannot filter: the original file, downloads and non-HLS streams of a title with segments for that viewer. Playing it again from the app gets the filtered stream.

**Some rated titles still appear in the library?**
: Check **Settings → Content Ratings**. The filter uses exact matching (for example, `"PG"` ≠ `"PG-13"`). `"Unrated"` is an explicit checkbox option, not the same as empty rating.
