# Telegram bot runtime

The bot runs as UID/GID `10001`, mounts the shared database at
`/data/assambleya.db`, and exposes only port `8080` to the internal Compose
network. Production requires `BOT_TOKEN`, a public HTTPS `PLATFORM_URL`, and a
`BOT_NOTIFY_SECRET` of at least 32 characters. `PLATFORM_API` points to
`http://backend:3000`; no bot port is published on the VPS.
