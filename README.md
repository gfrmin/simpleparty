# SimpleParty

Easily enjoy your private video collection. Browse and play local video files from any device on your network. Zero dependencies.

## Features

- **Directory browsing** - navigate nested folders with breadcrumb navigation
- **Shuffle play** - randomize playback within any directory
- **Delete** - remove videos you don't want, right from the player
- **Keyboard shortcuts** - full control without touching the mouse
- **Dark theme** - comfortable for extended viewing
- **Mobile friendly** - responsive layout with large tap targets
- **Auto-transcoding** - MKV/AVI/MOV files are automatically transcoded via ffmpeg or VLC (if installed)
- **URL download** - paste a URL from the browse page or `/download` and yt-dlp fetches it into that directory (opt-in extra)
- **AI video tagging** - automatically tag videos using a local Ollama vision model (opt-in, requires `--tag` flag)
- **Manual tagging** - add or edit tags on any video from the player page
- **Tag summary** - see all tags in a directory at a glance, with counts
- **Encrypted directories** - unlock/lock fscrypt-encrypted folders from the browser (if fscrypt is installed)
- **Zero dependencies** - pure Python standard library, nothing to install

## Install

```sh
# With uv (recommended)
uv pip install simpleparty

# Or run directly without installing
uvx simpleparty /path/to/videos
```

## Usage

```sh
simpleparty /path/to/videos
```

With no arguments, serves the current directory:

```sh
cd ~/Videos && simpleparty
```

Then open http://localhost:1312 in your browser (or use your machine's hostname/IP from another device).

### Options

```
simpleparty [/path/to/videos] [options]

  -p, --port PORT       Port to listen on (default: 1312)
  -b, --bind ADDR       Bind address (default: 0.0.0.0)
  --no-delete           Disable the delete button
  --no-transcode        Disable ffmpeg/VLC transcoding
  --no-tag              Disable all tagging features
  --no-download         Disable URL download feature
  --yt-dlp-format FMT   yt-dlp format selector
  --tag-model MODEL     Ollama vision model (default: huihui_ai/qwen3-vl-abliterated:8b)
  --ollama-url URL      Ollama API URL (default: http://localhost:11434)
```

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| `n` / `Right` | Next video |
| `p` / `Left` | Previous video |
| `s` | Toggle shuffle |
| `d` | Delete current video |
| `f` | Toggle fullscreen |
| `Space` | Play / pause |
| `m` | Mute / unmute |
| `Esc` | Go to parent directory |
| `?` | Show shortcut help |

## Optional features

These are auto-detected at startup and require no configuration:

- **ffmpeg** or **VLC** - Enables playback of MKV, AVI, and MOV files by transcoding to browser-compatible MP4 on the fly. Install either one: `sudo apt install ffmpeg` / `sudo pacman -S ffmpeg`
- **fscrypt** - If your video directories use Linux filesystem encryption (fscrypt), SimpleParty will detect locked directories and prompt for the passphrase in the browser

## URL download

Paste a URL on any directory page (or on `/download`) to fetch a video via [yt-dlp](https://github.com/yt-dlp/yt-dlp). Downloads land in the chosen directory, run serially in a single background worker, and keep going even after you navigate away or close the browser — as long as the server stays up.

### Setup

```sh
uvx simpleparty[download] /path/to/videos
```

Any install of yt-dlp visible to Python will do; the feature is auto-detected at startup.

### Notes

- One download at a time (queue is in-memory and does not persist across restarts).
- Partial `.part` files may be left behind after an unclean shutdown — remove them manually if you don't want them.
- Running downloads cannot be cancelled mid-flight yet; only queued jobs.
- Installing `ffmpeg` lets yt-dlp merge separate video+audio streams into a single MP4.

## AI tagging

Tagging is always available — you can manually add or edit tags from the video player page, no setup required. Tags are stored in a `.simpleparty-tags.json` file per directory.

For **AI-powered automatic tagging**, SimpleParty uses a local vision language model via [Ollama](https://ollama.com). This runs entirely on your machine — no data leaves your network. If Ollama and ffmpeg are available, a "Tag" button appears in the directory browser. Click it to tag all untagged videos in that directory. Tagging runs in the background — you can close the browser and it will keep going as long as the server is running.

### AI tagging setup

1. Install [Ollama](https://ollama.com)
2. Pull a vision model: `ollama pull huihui_ai/qwen3-vl-abliterated:8b`
3. Start SimpleParty — AI tagging is auto-detected:

```sh
simpleparty /path/to/videos
```

### AI tagging requirements

- **Ollama** running locally (or specify `--ollama-url`)
- **ffmpeg** for extracting video keyframes
- A GPU with ~8GB VRAM for the default 8B model (NVIDIA recommended)

If these aren't available, SimpleParty still works — you just won't see the AI "Tag" button. Manual tagging always works.

## Why not Jellyfin/Plex?

Those are full media centers with databases, metadata scraping, user accounts, and transcoding pipelines. SimpleParty is for when you just want to open a folder of videos and watch them. One command, no setup, no database.

## License

AGPL-3.0
