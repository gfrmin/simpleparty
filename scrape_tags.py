#!/usr/bin/env python3
"""Scrape tags from motherless.com for videos with 7-hex codes in filenames.

Uses scrapling AsyncFetcher for concurrent requests.
Resumable: skips filenames already present in the tags file.
Uses BRIGHTDATA_PROXY env var if set.

Usage: uv run python scrape_tags.py ~/yo/more/more/
"""

import asyncio
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from scrapling.fetchers import AsyncFetcher

from simpleparty.tagger import load_tags, save_tags

CONCURRENCY = 10
SAVE_EVERY = 50

log = logging.getLogger('scrape_tags')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
# Silence scrapling's own logging
logging.getLogger('scrapling').setLevel(logging.WARNING)


def extract_hex_code(filename):
    """Extract 7-char hex code from filename. Prefers [CODE] bracket form."""
    m = re.search(r'\[([0-9A-Fa-f]{7})\]', filename)
    if m:
        return m.group(1).upper()
    m = re.match(r'^([0-9A-Fa-f]{7})\b', filename)
    if m:
        return m.group(1).upper()
    return None


async def fetch_one(code, semaphore, proxy):
    """Fetch tags and title for a single motherless code."""
    async with semaphore:
        try:
            kwargs = {}
            if proxy:
                kwargs['proxy'] = proxy
            page = await AsyncFetcher.get(
                f'https://motherless.com/{code}',
                timeout=20,
                **kwargs,
            )
            tags_div = page.css('div.media-meta-tags')
            tags = [a.text.lstrip('#') for a in tags_div[0].css('a.pop')] if tags_div else []
            title_el = page.css('title')
            title = title_el[0].text.split('|')[0].strip() if title_el else ''
            return tags, title, None
        except Exception as e:
            return [], '', str(e)


async def main():
    if len(sys.argv) < 2:
        print(f'Usage: {sys.argv[0]} <directory>', file=sys.stderr)
        sys.exit(1)

    directory = sys.argv[1]
    proxy = os.environ.get('BRIGHTDATA_PROXY')
    if proxy:
        log.info('Using Brightdata proxy')
    else:
        log.info('No proxy (set BRIGHTDATA_PROXY to use one)')

    existing = load_tags(directory)

    to_scrape = {}
    skipped = 0
    for name in sorted(os.listdir(directory)):
        if name.startswith('.'):
            continue
        code = extract_hex_code(name)
        if code and name not in existing:
            to_scrape[name] = code
        elif name in existing:
            skipped += 1

    log.info(f'Found {len(to_scrape)} to scrape ({skipped} already done)')
    if not to_scrape:
        return

    semaphore = asyncio.Semaphore(CONCURRENCY)
    items = list(to_scrape.items())
    done = 0
    errors = 0
    with_tags = 0
    start_time = time.monotonic()

    for chunk_start in range(0, len(items), SAVE_EVERY):
        chunk = items[chunk_start:chunk_start + SAVE_EVERY]
        tasks = [fetch_one(code, semaphore, proxy) for _, code in chunk]
        results = await asyncio.gather(*tasks)

        chunk_errors = 0
        for (filename, code), (tags, title, err) in zip(chunk, results):
            done += 1
            if err:
                errors += 1
                chunk_errors += 1
                log.debug(f'ERR {code}: {err}')
            else:
                if tags:
                    with_tags += 1
                existing[filename] = {
                    'tags': [t.lower() for t in tags],
                    'title': title,
                    'source': f'motherless:{code}',
                    'scraped_at': datetime.now(timezone.utc).isoformat(),
                }

        save_tags(directory, existing)
        elapsed = time.monotonic() - start_time
        rate = done / elapsed if elapsed > 0 else 0
        remaining = (len(items) - done) / rate if rate > 0 else 0
        log.info(
            f'[{done}/{len(items)}] '
            f'{with_tags} tagged, {errors} errors, '
            f'{rate:.0f}/s, ~{remaining/60:.0f}m left'
            + (f' ({chunk_errors} errors this chunk)' if chunk_errors else '')
        )

        # Back off if too many errors in a chunk (rate limited)
        if chunk_errors > len(chunk) * 0.5:
            log.warning('High error rate, pausing 30s...')
            await asyncio.sleep(30)

    elapsed = time.monotonic() - start_time
    log.info(
        f'Done. {done} scraped, {with_tags} with tags, '
        f'{errors} errors in {elapsed/60:.1f}m'
    )


if __name__ == '__main__':
    asyncio.run(main())
