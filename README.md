# Unit3Dup-G3MINI Stack

Docker stack for [Unit3Dup-G3MINI](https://github.com/lantiumBot/Unit3Dup-G3MINI), ready for local Docker use and easy deployment on NAS, Portainer, or Dockhand.

This repository focuses on containerized deployment:

- `Dockerfile`
- `docker-compose.yml`
- persistent `/config`, `/watch`, `/done`, `/data` volumes
- non-root runtime support
- container-safe HTTP cache handling

## What This Repo Is For

This repo is meant for users who want to run Unit3Dup-G3MINI with Docker instead of installing Python and dependencies manually.

It keeps the original application code and adds the Docker pieces needed to:

- build the container locally
- run the watcher in a persistent stack
- deploy more easily on a NAS

## Included Files

- `Dockerfile`
- `docker-compose.yml`
- `.dockerignore`

The container stores its config in `/config` through `UNIT3DUP_CONFIG_ROOT=/config`.

## Default Volumes

The default compose file uses:

- `./docker-data/config` -> `/config`
- `./docker-data/watch` -> `/watch`
- `./docker-data/done` -> `/done`
- `./docker-data/media` -> `/data`

For a NAS, replace them with your real shared folders.

Example:

```yaml
volumes:
  - /volume1/docker/unit3dup/config:/config
  - /volume1/torrents/watch:/watch
  - /volume1/torrents/done:/done
  - /volume1/media:/data
```

## Quick Start

### 1. Build the image

```bash
docker compose build
```

### 2. Generate the initial config

```bash
docker compose run --rm unit3dup --help
```

This creates:

- `/config/Unit3Dbot.json`
- cache and archive folders inside `/config`

### 3. Edit `Unit3Dbot.json`

Minimum required fields:

- `Gemini_URL`
- `Gemini_APIKEY`
- `Gemini_PID`
- `TMDB_APIKEY`
- `IMGBB_KEY`
- `WATCHER_PATH`
- `WATCHER_DESTINATION_PATH`

For Docker, the usual values are:

```json
"WATCHER_PATH": "/watch",
"WATCHER_DESTINATION_PATH": "/done"
```

If you use an external torrent client, also adjust the client section:

- `QBIT_HOST` / `QBIT_PORT`
- or `TRASM_HOST` / `TRASM_PORT`
- or `RTORR_HOST` / `RTORR_PORT`

### 4. Start the watcher

```bash
docker compose up -d
```

### 5. Read logs

```bash
docker compose logs -f
```

## Manual Commands

Scan a folder:

```bash
docker compose run --rm unit3dup -scan /data/my_folder
```

Upload a file:

```bash
docker compose run --rm unit3dup -u /data/my_file.mkv
```

Generate config only:

```bash
docker compose run --rm unit3dup --help
```

## Portainer / Dockhand

This repository currently ships with a compose file using `build:`.

That is the simplest setup for:

- local Docker
- Portainer environments that support building from a project folder
- NAS testing before publishing a prebuilt image

If you prefer an `image:`-only stack later, you can publish the built image and replace `build:` with your registry image tag.

## Permissions

The compose file uses:

```yaml
user: "${PUID:-1000}:${PGID:-1000}"
```

If your NAS uses a different user or group ID, set `PUID` and `PGID` accordingly.

## Security Notes

- Do not commit your `Unit3Dbot.json`
- Do not put API keys in `docker-compose.yml`
- Do not commit `.env` files with secrets
- Keep personal paths in local overrides only

This repository ignores local config and test overrides by default.

## Upstream Project

Original project:

- [Unit3Dup-G3MINI](https://github.com/lantiumBot/Unit3Dup-G3MINI)

Base project:

- [Unit3Dup](https://github.com/31December99/Unit3Dup)
