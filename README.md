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
- **Encrypted directories** - unlock/lock fscrypt-encrypted folders from the browser (if fscrypt is installed)
- **Single file, zero dependencies** - pure Python standard library, nothing to install

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

Then open http://localhost:1312 in your browser (or use your machine's hostname/IP from another device).

### Options

```
simpleparty /path/to/videos [options]

  -p, --port PORT       Port to listen on (default: 1312)
  -b, --bind ADDR       Bind address (default: 0.0.0.0)
  --no-delete           Disable the delete button
  --no-transcode        Disable ffmpeg/VLC transcoding
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

## Why not Jellyfin/Plex?

Those are full media centers with databases, metadata scraping, user accounts, and transcoding pipelines. SimpleParty is for when you just want to open a folder of videos and watch them. One command, no setup, no database.

## License

AGPL-3.0
