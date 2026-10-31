# Newsreader3000

A self-hosted, web-based Usenet newsreader and downloader that runs as a Docker container (made with Unraid in mind).
Connect it to your provider (for example Eweka), then browse, search, read and download from newsgroups in the browser.

## Features

**Groups**
- Download the full list of groups your provider carries (`LIST ACTIVE`), with optional descriptions
- Filter the list by text or wildcards (`alt.binaries.*.hd*`), see article counts, subscribe or unsubscribe with one click
- About 20 popular binary groups are subscribed on first start, so search works straight away. Add any other group by name
- Download headers per group: the newest N first, then **Older…** to go further back. You can also update all subscribed groups at once or on a schedule
- Optional header retention (delete stored headers older than X days)
- **Header log**: every header download is recorded (group, article range, headers received/new, time, speed, compressed or not, errors). It is shown per group on the Browse page and for all groups on the Groups page
- **Fast header downloads**:
  - compressed transfer (`XFEATURE COMPRESS GZIP`) when the server supports it, which moves 3–5× less data
  - several connections in parallel
  - parsing and storage run in the background, so the web page stays responsive during big fetches

**Browse and read**
- Three views per group:
  - **Releases**: multi-file posts grouped together, with a completeness %, file count, and par2/nfo markers
  - **Files**: each file on its own, with its parts
  - **Articles**: raw posts, for text/discussion groups
- Click or Shift-click rows to select a range of releases
- Filter by words (`-word` excludes a word), minimum size and maximum age. Sort by date, size or name. Read/unread tracking with a "hide read" option and "mark all read"
- Article viewer with headers, text (quotes dimmed, links clickable), thread view, inline images for yEnc/MIME/UU attachments, and attachment downloads
- **Preview** files inside a release without downloading the whole thing: images, NFOs (DOS art shown correctly), text, PDF, audio, short video (up to 64 MB)
- **Post** new messages and replies to text groups

**Search**
- Full-text search over releases from all subscribed groups, or only the ones you pick. Releases and files are precomputed while headers arrive, so browsing and searching never have to regroup millions of articles
- Supports `"exact phrase"`, `-exclude`, prefix matching, size range, age, poster, and Release/File/Article views

**Downloads**
- Download selected releases, files or articles, or export them as an `.nzb`
- Add NZBs by upload (drag and drop, several at once), by URL (paste an NZBgeek download link), or through a **watch folder**. A subfolder of the watch folder sets the category
- Uses several connections and several servers: a backup or block account fills in articles missing from the primary server
- yEnc decoding with CRC check, writing straight to disk. Resumes after a container restart
- **par2** verify and repair (on missing articles, or always), and it fixes obfuscated file names
- **Automatic extraction** of rar (including multi-volume), zip and 7z. You can switch it on or off globally, per download, and when adding an NZB. Archives inside archives are extracted too. The archives can be deleted afterwards
- Archive passwords can come from the NZB metadata, from the file name (`Name{{password}}.nzb`), or be typed in. A failed job can be retried with a password
- Categories (each one is a subfolder), priorities, pause/resume per item or for everything, a speed limit, a history with retry, and NZB re-download
- If extraction fails on a download that looked complete, it runs par2 repair and tries the extraction again

**Other**
- Optional login for the web UI
- Shows free disk space, current speed and background tasks
- Works on phones and has a dark mode

## Install on Unraid

1. Get the image. Either:
   - push a tag such as `v1.0.0`. The **CI** GitHub Action then builds `ghcr.io/peterdbnl/newsreader3000:latest`. Because this repo is private, the image is private too: either make the package public on GitHub, or log Unraid in to ghcr.io with a personal access token; or
   - build it on the server: `git clone` this repo, run `docker build -t newsreader3000 .`, and put `newsreader3000` in the template's *Repository* field.
2. Copy `unraid/newsreader3000.xml` to `/boot/config/plugins/dockerMan/templates-user/` on the Unraid flash drive.
3. Go to **Docker → Add Container** and pick the **newsreader3000** template. Check the paths:
   - `/config` → `/mnt/user/appdata/newsreader3000`
   - `/downloads` → for example `/mnt/user/downloads/usenet`. It will contain `complete/<category>`, `incomplete` and `watch`
4. You can fill in your Eweka login in the template, or enter it later in the web UI.
5. Open `http://<unraid-ip>:8095`, go to **Settings → News servers**, click **Test**, then **Save**.

### Eweka settings

| Setting | Value |
|---|---|
| Host | `news.eweka.nl` (or `sslreader.eweka.nl`) |
| Port | `563` (SSL) |
| Connections | up to what your plan allows (check your Eweka account) |

Browsing and header downloads use a few connections of their own (Settings → *Connections for browsing / headers*, default 4). Downloads use the rest, so the total never goes over the server's connection limit.

## Publishing to Docker Hub

The **CI** workflow pushes to Docker Hub as well as ghcr.io once two repository secrets exist. Without them it skips Docker Hub.

1. On hub.docker.com, go to **Account settings → Personal access tokens**, then create a token with *Read & Write* access.
2. On GitHub, open this repo's **Settings → Secrets and variables → Actions** and add:
   - `DOCKERHUB_USERNAME`: your Docker Hub username
   - `DOCKERHUB_TOKEN`: the token from step 1
3. Push a tag such as `v1.0.0`. The image appears as `<your-username>/newsreader3000:latest` and `:1.0.0`. Docker Hub repos are public by default.

On Unraid, put `<your-username>/newsreader3000:latest` in the template's *Repository* field. Docker Hub images need no login when they are public.

## Docker Compose

```bash
docker compose up -d --build
# open http://localhost:8095
```

See `docker-compose.yml` for the optional environment variables.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `PUID` / `PGID` | `99` / `100` | User and group that own the files written to disk |
| `UMASK` | `002` | Permission mask for new files |
| `NR_PORT` | `8095` | Web UI port |
| `NR_SERVER_HOST`, `NR_SERVER_PORT`, `NR_SERVER_USERNAME`, `NR_SERVER_PASSWORD`, `NR_SERVER_CONNECTIONS`, `NR_SERVER_SSL` | | Pre-fill the first server **on first start only** |
| `NR_UI_USERNAME`, `NR_UI_PASSWORD` | | Protect the web UI with a login. These override the values set in the UI |
| `NR_LOG_LEVEL` | `INFO` | Log level |

## How it works

- `app/nntp.py`: an async NNTP client with SSL, AUTHINFO, OVER/XOVER, LIST, BODY, ARTICLE and POST, and a connection pool for each server
- `app/groups.py`: the group list, header fetching (several connections, newest first), and search through SQLite FTS5
- `app/subjects.py`: groups subjects like `[03/15] - "Name.part02.rar" yEnc (12/137)` into files and releases
- `app/downloader.py`: the queue, fetching from several servers, yEnc decoding, file assembly, resuming, and the watch folder
- `app/postprocess.py`: par2, unrar/7z/zip extraction, cleanup, and moving finished downloads to `complete/<category>`
- `app/reader.py`: article view, attachments, multi-part previews and posting
- `app/static/`: the web UI (plain JavaScript, no build step)

Data lives in `/config`: `settings.json`, `newsreader.db` (groups, headers, jobs) and `nzb/`.

### Tips

- Binary groups are very large. Start with the default 20,000 newest headers, then use **Older…** to go back further when you need it. Header storage takes roughly 300–500 bytes per article.
- Search only covers headers you have downloaded. To search further back, fetch older headers or add more groups.
- Indexers such as NZBgeek are still the best way to find releases that are obfuscated. Paste their NZB link on the Downloads page.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q          # includes an end-to-end test against a built-in fake NNTP server
NR_CONFIG_DIR=./config NR_DOWNLOAD_DIR=./downloads uvicorn app.main:app --port 8095
```

The par2/rar/7z tests run when `par2`, `unrar`, `rar` and `7z` are installed. Otherwise they are skipped.
