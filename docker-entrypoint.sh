#!/bin/sh
# Keep extractors fresh on container start — sites (Instagram, Facebook, etc.) break
# them frequently, and the usual fix is simply updating. Skip with RECLIP_NO_UPDATE=1.
if [ -z "$RECLIP_NO_UPDATE" ]; then
    echo "Updating yt-dlp..."
    pip install --user --no-cache-dir -q -U yt-dlp || \
        echo "  (couldn't update yt-dlp — continuing with the installed version)"
    echo "Updating gallery-dl..."
    pip install --user --no-cache-dir -q -U gallery-dl || \
        echo "  (couldn't update gallery-dl — continuing with the installed version)"
fi

exec "$@"
