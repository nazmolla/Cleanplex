# Finding Your Plex Token

You need a Plex authentication token if you configure the Plex connection manually in **Settings** (the Docker setup reads it automatically from Plex's config folder).

1. Sign in to **Plex Web**
2. Browse to any media item
3. Click the three-dot menu → **Get Info** → **View XML**
4. The URL in your browser will contain `?X-Plex-Token=XXXXXX`

Copy the value after `X-Plex-Token=` and paste it into **Settings → Plex Connection → Plex Token**.

!!! tip "Token security"
    Once saved, the token is never sent back to the browser. For more details, see [Security](../reference/security.md).
