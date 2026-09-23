"""Compare the masters the Pelican site can store for each animated gif.

usage: python docs/gif-intermediates/master_sizes.py
           [--crf N] [--share X] [CACHE_DIR] [ARCHIVE_RAW]
       (defaults: CRF 28, share 0.75, .image-cache, archive/raw; run
       from the repo root after `pixi run build-pelican` or
       `pixi run medium-archive pelican`)

The image cache names every copy of a gif by the first 16 hex digits
of the gif's sha256. For each gif with an AV1 master at --crf, this
prints the candidates the cache holds, as a share of the gif:

  gif     the capped gif (gifsicle -O3), lossless   (<hash>.capped.gif)
  webp    the capped lossless WebP                   (<hash>.capped.webp)
  AV1 N   the AV1 4:4:4 master at CRF N, lossy       (<hash>.av1444-N.mp4)
  h264    the h264 clip the other sites place        (<hash>.mp4)

and which one the site stores under the exporter's rule: the smaller
lossless copy, unless the AV1 master is at most --share of it. The
stored one is green (on a terminal, unless NO_COLOR is set); "-" is a
candidate that does not exist (no exact capped gif: a frame over 256
colors; no WebP: a kept frame of 10 ms or less, or skipped because the
master was already small enough that no WebP could win -- see
sites.WEBP_MIN_SHARE -- which --share cannot bring back until an
export with that share makes it). Then the totals, and
what storing each candidate everywhere would come to.
"""
import argparse
import hashlib
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--crf", type=int, default=28,
                help="the AV1 masters to compare (default 28)")
ap.add_argument("--share", type=float, default=0.75,
                help="store AV1 when at most this share of the smaller "
                     "lossless copy (default 0.75, the exporter's)")
ap.add_argument("cache", nargs="?", default=".image-cache")
ap.add_argument("raw", nargs="?", default="archive/raw")
args = ap.parse_args()
cache, raw = Path(args.cache), Path(args.raw)
AV1 = f"AV1 {args.crf}"

# every copy of a gif the cache holds, by the gif's hash
copies = defaultdict(dict)
for p in cache.glob("*/*.*"):
    digest, _, rest = p.name.partition(".")
    kind = {"capped.gif": "gif", "capped.webp": "webp", "mp4": "h264"}.get(rest)
    if kind is None and (m := re.fullmatch(r"av1444-(\d+)\.mp4", rest)):
        kind = f"AV1 {m[1]}"
    if kind:
        copies[digest][kind] = p.stat().st_size

rows = []
for gif in raw.rglob("*.gif"):
    digest = hashlib.sha256(gif.read_bytes()).hexdigest()[:16]
    have = copies.get(digest, {})
    if AV1 not in have:
        continue
    # an empty capped file is the verdict that no exact copy exists
    sizes = {k: v for k, v in have.items() if v}
    lossless = [k for k in ("gif", "webp") if k in sizes]
    best = min(lossless, key=sizes.get, default=None)
    stored = (best if best and sizes[AV1] > args.share * sizes[best]
              else AV1)
    rows.append((gif, gif.stat().st_size, sizes, stored, digest))
if not rows:
    sys.exit(f"no CRF {args.crf} masters under {cache}; export the "
             "pelican site first")

mb = 1e6
color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
columns = ["gif", "webp", AV1, "h264"]


def green(text, win):
    """text in green when it is the stored one (padding kept outside
    the escape codes, so the columns stay aligned)."""
    return f"\033[32m{text}\033[0m" if win and color else text


print(f"{'gif MB':>8} {'stored MB':>9} "
      + " ".join(f"{c:>7}" for c in columns)
      + f"  {'stored':<7} {'hash':<16}  gif")
for gif, g, sizes, stored, digest in sorted(
        rows, key=lambda r: r[2][r[3]] / r[1]):
    cells = [green(f"{sizes[c] / g:7.0%}", c == stored) if c in sizes
             else f"{'-':>7}" for c in columns]
    print(f"{g / mb:8.2f} {sizes[stored] / mb:9.2f} " + " ".join(cells)
          + f"  {stored:<7} {digest}  {gif.relative_to(raw)}")

tg = sum(r[1] for r in rows)
ts = sum(r[2][r[3]] for r in rows)
print(f"\n{len(rows)} animated gifs, {tg / mb:.1f} MB")
print(f"  stored under the rule (share {args.share:g}): {ts / mb:.1f} MB, "
      f"{ts / tg:.1%} of the gifs")
for kind in columns[:3]:
    n = sum(r[3] == kind for r in rows)
    b = sum(r[2][r[3]] for r in rows if r[3] == kind)
    print(f"    as {kind:<7} {n:4d} gifs, {b / mb:7.1f} MB")
print("  each candidate stored everywhere it exists (the gif where not):")
for kind in columns:
    have = [r for r in rows if kind in r[2]]
    total = sum(r[2][kind] if kind in r[2] else r[1] for r in rows)
    print(f"    {kind:<7} {total / mb:7.1f} MB, {total / tg:.1%} of the gifs"
          f"  ({len(have)} of {len(rows)} exist)")
