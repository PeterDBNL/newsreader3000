#!/bin/sh
# Run as PUID:PGID (Unraid default 99:100 = nobody:users) so downloaded files have the right owner.
set -e
umask "${UMASK:-002}"
PUID="${PUID:-99}"
PGID="${PGID:-100}"
mkdir -p "$NR_CONFIG_DIR" "$NR_DOWNLOAD_DIR"
if [ "$(id -u)" = "0" ]; then
  chown "$PUID:$PGID" "$NR_CONFIG_DIR" "$NR_DOWNLOAD_DIR" 2>/dev/null || true
  chown -R "$PUID:$PGID" "$NR_CONFIG_DIR" 2>/dev/null || true
  exec gosu "$PUID:$PGID" "$@"
fi
exec "$@"
