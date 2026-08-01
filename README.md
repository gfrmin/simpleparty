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
- **AI video tagging** - learns from your own tags using a local OpenCLIP model: train a classifier, suggest tags for untagged videos (with zero-shot fallback), and flag likely-mislabeled tags (opt-in `[classifier]` extra)
- **Manual tagging** - add or edit tags on any video from the player page
- **Star favourites** - star videos from the player page and filter the browse view to starred only
- **Tag summary** - see all tags in a directory at a glance, with counts
- **Encrypted directories** - locked fscrypt folders are recognised out of the box; unlock/lock them from the browser (unlocking needs fscrypt installed)
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
  --max-tags N          Max tags per video when suggesting (default: 10)
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
- **fscrypt** - If your video directories use Linux filesystem encryption (fscrypt), SimpleParty marks locked ones with a padlock and prompts for the passphrase in the browser. Detection asks the kernel directly and needs nothing installed; *unlocking* needs the fscrypt tool, so without it the padlock is still shown and the page explains what to install. Note that installing fscrypt is only half the job — `sudo fscrypt setup` must also have been run, or the tool cannot read its own config

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

Tagging is always available — you can manually add or edit tags from the video player page, no setup required. Tags are stored in a `.simpleparty/tags.json` file per directory.

For **AI-powered automatic tagging**, SimpleParty learns from *your* tags. It runs a local [OpenCLIP](https://github.com/mlfoundations/open_clip) image encoder over a handful of frames per video, caches that embedding once, then trains a tiny classifier on top of the videos you've already tagged. Everything runs entirely on your machine — no data leaves your network, which matters for a private collection.

How it works:

- **Train** — once a directory has some confirmed tags, click **Train**. SimpleParty embeds each video (cached, so it only happens once) and fits a lightweight classifier. Because the embeddings are cached, re-training after editing tags takes *seconds*.
- **Suggest** — click **Suggest tags** to tag untagged videos. Suggestions are unconfirmed until you accept them. If you haven't trained yet, it falls back to **zero-shot** tagging using your tag names directly, so you get useful suggestions with no training at all.
- **Find mislabeled tags** — training also cross-checks your existing tags. Tags it strongly disagrees with are flagged with a ⚠ badge on the player page so you can fix them in one click. These flagged labels are also automatically excluded from training, so noisy tags don't degrade the model.

Tagging runs in the background — you can close the browser and it keeps going while the server runs.

### AI tagging setup

Install the optional classifier dependencies (pulls in PyTorch + OpenCLIP):

```sh
uvx simpleparty[classifier] /path/to/videos
# or, if installed: pip install 'simpleparty[classifier]'
```

The first training run downloads the CLIP model weights (~3.9GB for the default `ViT-H-14`) once, then caches them.

### AI tagging requirements

- **A CUDA GPU** (NVIDIA, ~6GB+ VRAM) — the encoder runs in fp16 and an RTX 4060 (8GB) is comfortable. It will fall back to CPU but embedding a large library that way is very slow.
- **ffmpeg** for extracting video frames.
- Tune suggestions per video with `--max-tags N` (default 10); disable all tagging with `--no-tag`.

If these aren't available, SimpleParty still works — manual tagging always does.

## Why not Jellyfin/Plex?

Those are full media centers with databases, metadata scraping, user accounts, and transcoding pipelines. SimpleParty is for when you just want to open a folder of videos and watch them. One command, no setup, no database.

## License

AGPL-3.0
