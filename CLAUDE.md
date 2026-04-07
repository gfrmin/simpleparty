# CLAUDE.md

## Project Overview

SimpleParty is a lightweight, zero-dependency Python video browser and player for private video collections. It serves a web UI for browsing directories and playing videos from any device on the local network. No external Python packages required — built entirely on the Python standard library.

**Repository:** gfrmin/simpleparty
**License:** AGPL-3.0-only
**Python:** 3.8+

## Architecture

```
src/simpleparty/
  __init__.py      # Version metadata (currently 0.4.1)
  __main__.py      # CLI entry point, delegates to server.main()
  server.py        # Core HTTP server, routing, HTML rendering (~1,240 lines)
  tagger.py        # AI video tagging via local Ollama API (~300 lines)
```

- **No framework** — uses `http.server.HTTPServer` with `ThreadingMixIn`
- **Server-rendered HTML** via f-strings, no template engine
- **HTMX 2.0.4** for dynamic UI (CDN-loaded)
- **Inline CSS** with dark theme and responsive layout
- **Zero Python dependencies** — only stdlib

### Key Components in server.py

- `VideoHandler` — HTTP request handler; `do_GET()`/`do_POST()` dispatch to route handlers
- `resolve_path()` — safe path resolution (prevents directory traversal)
- `list_directory()` — returns directory listing data structure
- `render_*()` functions — generate HTML fragments
- HTTP 206 Range request support for video seeking
- On-the-fly transcoding via subprocess (ffmpeg or VLC)
- `_config` dict at module level stores runtime configuration

### Tagging (tagger.py)

- AI tagging uses local Ollama vision models (keyframe extraction via ffmpeg)
- Manual tagging supported as fallback
- Tags stored in `.simpleparty-tags.json` per video directory (atomic writes via tempfile + `os.replace()`)
- Custom prompts via `.simpleparty-prompt.txt` per directory
- Background tagging runs in daemon threads with progress tracking

## Build & Run

```sh
# Build
make build          # or: uv build

# Run
simpleparty /path/to/videos          # default: localhost:1312
python -m simpleparty /path/to/videos
uvx simpleparty /path/to/videos      # without installing

# Publish
make publish        # builds and uploads to PyPI
```

### CLI Options

- `-p, --port PORT` — listen port (default: 1312)
- `-b, --bind ADDR` — bind address (default: 0.0.0.0)
- `--no-delete` — disable delete button
- `--no-transcode` — disable ffmpeg/VLC transcoding
- `--no-tag` — disable all tagging
- `--tag-model MODEL` — Ollama vision model
- `--ollama-url URL` — Ollama API URL
- `--tag-frames N` — keyframes per video for AI tagging

## Testing & Linting

No test suite or linting configuration exists. There are no tests to run.

## Code Conventions

- **Functional style**: pure functions for rendering (`render_*()`) and filtering (`filter_*()`)
- **HTML via f-strings**: no templating engine; HTML is built with string concatenation
- **Minimal error handling**: try/except with graceful fallbacks where needed
- **Security**: `Path.resolve()` for path safety, `html.escape()` for user content
- **No external deps**: any new functionality must use Python stdlib only
- **Version bumps**: update `__init__.py` version when adding features

## Data Storage

- **No database** — filesystem only
- `.simpleparty-tags.json` — per-directory JSON mapping filenames to tag data
- `.simpleparty-prompt.txt` — optional per-directory AI prompt override
- All transcoding is on-demand, no caching

## Git Workflow

- Default branch: `master`
- Commit messages: imperative mood, concise, describe the "what and why"
- Version bumps included in feature commits
- No CI/CD pipeline configured
