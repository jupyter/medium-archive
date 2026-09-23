# Animated gifs in the sites: findings and decisions

Status (2026-09-23): the experiment is complete, and its result is
implemented in `ImagePlacer` (`src/medium_archive/sites.py`). A second
round chose what the Pelican site stores as each animation's master
(see [The Pelican site's masters](#the-pelican-sites-masters)); it is
also complete, with its decisions recorded under
[Decisions](#decisions-2026-09-23). This page summarizes the
measurements behind both. The per-run tables are in this
file's git history. The measurement tools in `docs/gif-intermediates/`
are a record of how the numbers were produced; the build, the tests
and CI do not use them.

## What the exporters do now

Each animated gif in `archive/raw/` stays the master. Every site gets:

- **An H.264 clip for every animation**, in a `<video>` a reader can
  pause, even where the clip is larger than the gif. An animated gif
  that cannot become a clip stops the build: `AnimationError` names
  every such gif (a missing ffmpeg, libwebp or Pillow, real
  transparency, an unreadable gif, ffmpeg failing), and the command
  exits with an error. Only `animated_format = "gif"` in `site.toml`
  keeps gifs. A single-frame gif is a still and is placed as a PNG
  would be: line art as lossless webp, a photograph down the photo
  path.
- **Settings:** 4:2:0, High profile, `-crf 24 -preset slower`, no
  B-frames, one thread per encode (`warm()` encodes gifs in parallel).
- **Full resolution:** `animated_max_edge` defaults to 0. Odd sizes
  are padded by a pixel rather than rescaled.
- **A frame-rate cap near 30 fps:** `kept_frames` drops frames inside
  faster bursts, and every kept frame starts exactly when the gif
  shows it.
- **Exact timing:** timestamps use a millisecond time base, taken
  from the gif's stored delays.
- **A poster:** the clip's first frame as webp, at the clip's size.

Clips are encoded on the fly into `.image-cache/`, keyed by the gif's
hash and the settings (`CACHE_SCHEME` v7). CI restores that cache
between builds, so nothing encoded is committed.

**The Pelican site stores masters (2026-09, experimental).** It
stores one frame-rate-capped master of each animation and makes the
served H.264 from it when it is built. See
[The Pelican site's masters](#the-pelican-sites-masters).

### Full-archive build (2026-09-23)

The Pelican site, built with a cold cache on four cores with Ubuntu's
ffmpeg 6.1, against `main`'s old settings (CRF 20, `-preset fast`,
1,104 px cap):

| | old settings | new settings |
|---|---|---|
| clips | 177.5 MB | 152.3 MB |
| posters | 9.7 MB | 17.8 MB (full size) |
| gifs kept as gifs | 2 (0.1 MB) | 1 still under a `.gif` name |
| animations in total | 187.3 MB (32% of 586.6 MB of gifs) | 170.2 MB (29%) |
| clip vs its gif | median 38% | median 27%; 11 larger than their gif |
| all display images | 232 MB | 215 MB |
| cold build | 967 s | 1,026 s |
| clips shorter than their gif | 23 | 0 |

Every new clip has its gif's size (plus any padding pixel) and length.
The 11 clips larger than their gifs run from 103% to 237% of them.
Two are the `cda20dc15a21` particle recordings (finding 7); the other
nine, from posts `edb3f80dc1c0` (four), `789fcb1a5857` (three),
`a4ad73a1a718` and `f6e2e41ab3fa`, have not been looked at.

## The gifs

- **Count:** 216 gifs in 76 posts, 587 MB, all unique. The 47 over
  5 MB hold 59% of the bytes.
- **Content:** screen recordings, some with embedded video or
  dithered backgrounds. All are opaque and all loop forever.
- **Length:** a median of 17 s. 15 run under 5 s (18 MB in total).
- **Size:** longest edge median 1,200 px, maximum 3,340 px. 129 are
  wider or taller than 1,104 px.
- **Short delays:** two gifs store 10 ms delays that browsers play as
  100 ms, so they ran 3-5 times longer on Medium than their stored
  timing. `bd2524b247c2/005` is stored as 14.2 s and played as
  39.7 s; `11e5dab7c54/006` as 12.9 s and 63.7 s.
- **Fast recordings:** one post (`cda20dc15a21`) has nine 40 fps
  recordings.

`gif-intermediates/inventory.tsv` has the per-file survey.

## Findings

**1. Lossless storage does not pay on the large gifs.** On the four
largest, no lossless format beat the gif:

| format | size vs the gif |
|---|---|
| WebP lossless | 73-150% |
| JPEG XL | 82-582% |
| AV1 lossless | 257-597% |

A gif stores per-frame palettes compressed with LZW, and only the
rectangle that changed in each frame. That is hard to beat without
loss on screen content. No single lossless format wins; it depends on
the content. On a plain text screencast (`9549c5dcf551/003`, 1000x600,
72 frames, 821 KB):

| lossless encode | vs gif | encode | exact |
|---|---|---|---|
| libx264rgb `-qp 0 -preset placebo` | 51% | 7.6 s | yes |
| libx265 lossless `placebo` | 58% | 74 s | yes |
| JPEG XL (from a Pillow APNG), effort 9 | 58% | 19 s | yes |
| gif2webp `-m 6 -q 100` | 62% | 124 s | yes |
| gifsicle `-O3` | 96% | 0.5 s | yes |
| libvpx-vp9 lossless | 150% | 35 s | yes |
| FFV1 | 407% | 1.2 s | yes |
| libaom `-crf 0`, `-cpu-used` 1 / 4 | 68% / 74% | 92 s / 21 s | no (1 / 10 pixels off by 1) |

On a screencast of a notebook playing video (`2e432df402c8/013`,
1836x970, 17.6 MB), the dithered video area costs every video codec
more than the gif's palette indices do: WebP lossless 73% (1,057 s),
gifsicle `-O3` 98%, x264rgb 190%, x265 235%, libaom lossless 255-257%.
Only formats that can code a palette beat the gif there. APNG allows
one palette per file, so it loses colors on a gif with per-frame
palettes, and RGB APNG was 196% of the gif. ffmpeg's APNG muxer also
rounds delays. Libaom's lossless mode was also not reliably
exact: single pixels came out off by one at `-cpu-used` 1, 2 and 4.

**2. Browsers set the quality ceiling, through 4:2:0 color.** Video
stores brightness at full resolution. 4:4:4 keeps color at full
resolution too; 4:2:0, the only format every browser plays, keeps one
color sample per 2x2 block of pixels. On screen content that makes
red code strings pinkish and soft, dulls 1-pixel saturated lines, and
erases dither dots, whatever the codec or CRF. Every 4:2:0 encode sat
at 36-38 dB median PSNR against the gif; 4:4:4 encodes reached
41-48 dB. The site's earlier clips had this loss too.

**3. Codecs compared.** Seven difficult files (68 MB of gifs), all
frame-rate capped:

| encode | plays in | size vs gifs | median PSNR |
|---|---|---|---|
| H.264 4:2:0 CRF 20 | all browsers | 40% | 37.3 dB |
| H.264 4:2:0 CRF 24 (chosen) | all browsers | 28% | 36.6 dB |
| AV1 4:2:0 CRF 24 (libaom `-cpu-used 6`) | not Safari before Apple M3 | 20% | 36.6 dB |
| AV1 4:2:0 CRF 28 (libaom) | not Safari before Apple M3 | 17% | 36.2 dB |
| AV1 4:4:4 CRF 28 (libaom) | Chrome and Firefox | 27% | 47.6 dB |
| H.264 4:4:4 CRF 20 | no browser | 48% | 41.6 dB |
| SVT-AV1 4:2:0 CRF 30, preset 6 | not Safari before Apple M3 | 38% | 36.5 dB |

- **Two-step chain:** making H.264 from an AV1 master was slightly
  smaller than H.264 straight from the gif, but lower quality on every
  file (0.2-1.2 dB). So nothing is stored between the gif and the clip.
- **SVT-AV1** is fast, but it lost badly on 1-pixel line content, and
  it does not keep the last frame's duration.

**4. CRF.**
- **H.264 4:2:0:** CRF 16, 20 and 24 looked alike in crops of text at
  3x zoom. What differed from the gif was the 4:2:0 color, the same at
  each CRF. CRF 24 is about a third smaller than CRF 20.
- **AV1:** a ladder from CRF 28 to 52 found where small text breaks.
  First artifacts appear at CRF 34, damage is clear at 40, and CRF 52
  shows text copied from the wrong place.

**5. Resolution.** H.264 CRF 24 at a limit on the longest edge,
against full resolution:

| limit | size vs gifs | vs full resolution |
|---|---|---|
| none (full resolution) | 28% | 100% |
| 1,472 px (twice the body column) | 22% | 79% |
| 1,104 px (the old default) | 17% | 61% |

Scaling saves most on dithered or video content and least on screen
text, which it blurs. Full resolution keeps code readable when a
reader takes a clip full screen. That is the same reasoning by which
`sites.py` keeps still line art at full resolution.

**6. Frame-rate cap.** It affects 11 of the 216 gifs (3,749 of 61,048
frames). Kept frames are at least 29.5 ms apart, so up to about 33 fps:
gif delays are whole hundredths of a second, so the steps near 30 fps
are 30 ms (33 fps) and 40 ms (25 fps), and 29.5 ms keeps 30 ms frames.
A strict 30 fps limit (33.4 ms) would mean 25 fps in the fast parts and
would change 38 gifs, not 11, among them every recording made at a
30 ms delay (`3ee42dfdc54f/002`: 2,190 frames down to 1,461). The
limit stays at 29.5 ms.
- **Delays:** the stored delays are used, as the author made them,
  not the 100 ms browsers substituted.
- **ffmpeg's own options don't fit:** `-r` and `-fpsmax` require
  constant-rate output, and `fps=30` moves frame boundaries onto its
  grid.
- **Filter form:** the `select` expression is nested as a balanced
  tree, because ffmpeg's parser fails on a flat sum of a few hundred
  terms.
- **Savings:** a clip of `11e5dab7c54/006` came out 43% of the gif
  capped against 60% uncapped (AV1 CRF 20, 4:4:4).

**7. Clips that are larger than their gifs.** The 40 fps
`cda20dc15a21` recordings are particle simulations: hundreds of
triangles drawn with aliased 1-pixel pure-green lines, each moving on
its own. A gif codes these almost for free. A video codec gets little
from motion prediction here and pays heavily to transform-code
1-pixel edges, and 4:2:0 cannot represent the thin green lines. H.264
comes out at 155-211% of the gif. These are placed as clips anyway,
for the pause control.

**8. Timing traps** (all avoided now):
- **ffmpeg's guessed time base:** left alone, ffmpeg gives the encoder
  a time base from a guessed frame rate and rounds every timestamp to
  it; one 25.3 s gif played 25.5 s. `-enc_time_base 1:1000` fixes it.
- **x264 threading:** splitting one encode across threads made one
  clip 39% larger.
- **B-frames in mp4:** with frames reordered, x264 gives the mp4
  muxer no packet durations, and the file ends at the last frame's
  decode time. In the first full build, 27 clips came out short, one
  of them by 1.55 s of a 2.64 s hold near its end; `main`'s clips had
  23. `-bf 0` fixes it, for 4% more bytes across the archive.
- **gif2webp** stores delays of 10 ms or less as 100 ms.
- **ffmpeg's WebP decoder** plays stored delays of 10 ms or less as
  100 ms.
- **ffmpeg's APNG muxer** rounds delays.
- **cjxl** rejects gifs whose partial frames dispose to background.
- **gifsicle** cannot drop frames from gifs with per-frame color
  tables.

**9. Single-frame gifs.** One image in the archive
(`3b3dfb877664/004-0_U5H7uyoSLf0pZm6q`, 1,515x651, stored as `.bin`
and typed as a gif by convert) is a gif of one frame. It is placed as
a PNG would be; as lossless webp it is 32,156 bytes against 52,546,
61%, pixel for pixel.

## The Pelican site's masters

The Pelican site is meant to become a repository of its own. Every
media file it commits stays in its history for good, so what it
stores should be small, should not lose much, and should not need
committing again when the served format changes. It therefore stores
one **master** of each animation, and its build makes the H.264 a
browser is served from that master (`_serve_clips` in the site
plugin). A later change of served format is a change to the plugin,
not a new copy of every animation. The other sites are unchanged.

### What it stores

Every candidate is frame-rate capped (`kept_frames`) and at full size.
All three are built into `.image-cache/` and the choice is made on
each lookup, so a new threshold re-encodes nothing:

| candidate | cache name | kind |
|---|---|---|
| the gif, re-optimized by gifsicle `-O3` | `<hash>.capped.gif` | lossless |
| animated WebP (Pillow, `lossless`, `quality=75`, `method=4`: cwebp's defaults) | `<hash>.capped.webp` | lossless |
| AV1 4:4:4, libaom CRF 28, `-cpu-used 6`, not padded | `<hash>.av1444-28.mp4` | lossy |

The site stores the smaller lossless copy, unless the AV1 master is at
most 75% of it (`[images] master_max_share`, default 0.75). The
clip's poster goes beside whichever is stored, and marks a stored gif
or WebP as an animation for the build: the build makes `<name>.mp4`
beside it and the page shows that as a `<video>`. The gif or WebP
stays in the output for the feeds, which carry the page as written.
`[images] clip_master = "none"` stores the H.264 clip instead, as the
other sites do.

Two candidates can be missing:

- **No capped gif when a kept frame has more than 256 colors.** A gif
  frame holds at most 256 colors, but most frames only redraw a
  rectangle over what is already on screen, each with its own palette,
  so the picture a reader sees can hold far more
  (`11e5dab7c54/006` shows up to 6,141 at once). Dropping a frame
  breaks that layering: the frames after it were drawn on top of the
  dropped one. So where the cap drops frames, each kept frame is
  written whole (composited by Pillow, with a palette of exactly its
  own colors, numpy), and a frame over 256 colors cannot be written
  exactly. This affects only the 11 gifs the cap changes; for the
  rest the capped gif is the gif itself through gifsicle `-O3`,
  never larger than the gif. A scan of those 11 found one:
  `11e5dab7c54/006` (6.0 MB), with 287 of its 288 kept frames over
  256 colors (up to 6,141). The other ten peak at 151-255 colors
  (`bd2524b247c2/005` 151, the nine `cda20dc15a21` recordings
  232-255). The missing candidate costs nothing there: its AV1 4:4:4
  CRF 28 master was 30% of the gif in the ladder (about 1.8 MB), so a
  lossless copy would have to be under about 2.4 MB (40% of the gif)
  to be stored instead.
- **No WebP when a kept frame would last 10 ms or less.** The cap
  spaces kept frames at least 29.5 ms apart, but the first and last
  frames are always kept, and a frame just before a long one can
  still be short. Browsers and ffmpeg's WebP decoder play a delay of
  10 ms or less as 100 ms, so such a WebP would play at the wrong
  speed, and the H.264 made from it would be mistimed. (ffmpeg's gif
  decoder keeps the stored delays, so a gif has no such problem at
  build time.) Also none where `animated_max_edge` shrinks the gif:
  a resampled frame gains the colors lossless pays for.

  A scan of all 215 animated gifs found seven with such a frame, in
  every case the **last** frame, stored as 10 ms; none of the seven
  loses a frame to the cap. The two gifs with runs of 10 ms frames
  (`bd2524b247c2/005`, `11e5dab7c54/006`) are not among them: the cap
  merges those runs, and `11e5dab7c54/006` keeps 288 of its 691
  frames, 30-180 ms apart (22.4 fps on average), with a 70 ms last
  frame.

  | gif | frames | size |
  |---|---|---|
  | `11e5dab7c54/002` | 440 | 6.0 MB |
  | `52f9657fa7a/007` | 130 | 5.0 MB |
  | `fe9b54227d92/011` | 78 | 0.9 MB |
  | `9549c5dcf551/003` | 72 | 0.8 MB |
  | `ae191bc6fb8e/005` | 174 | 0.6 MB |
  | `a35ce050f7f7/001` | 108 | 0.3 MB |
  | `81f2eaad5706/002` | 94 | 0.2 MB |

  Together 13.8 MB of gifs. In the full export this cost nothing:
  six of the seven store their AV1 master anyway, and
  `81f2eaad5706/002` stores its 0.13 MB capped gif. So their last
  frame is left as it is.

**The WebP is only made where it could win, at cwebp's default
effort.** Lossless
WebP at `method=6` is by far the slowest candidate: the first full export
spent hours on it, where the capped gif takes seconds a gif. In that
export's first 113 WebPs, none came in under 44% of its capped gif
(`4f58385e25bb/005`: 41% of the original gif, against 93% for the
capped gif). So the exporter builds the capped gif first, and skips
the WebP where the AV1 master is at most `master_max_share` x 0.4
(`WEBP_MIN_SHARE`) of the capped gif: there the master is stored
whatever the WebP would weigh. On that export's data the rule changes
no stored choice (the closest, `2e432df402c8/007`, has AV1 at 3% of
the gif against 0.3 x 6% = 1.8%) and skips the WebP for most gifs.
Nothing is cached for a skipped WebP, so a larger share makes it on
the next export. The WebP is also encoded at cwebp's default effort
(`method=4`, `quality=75`) rather than `method=6`, `quality=100`,
libwebp's slowest: it decides what is stored for few gifs, and at
the halfway point of the first export (14 WebPs stored) it saved
about 1.1 MB net of 126 MB. The size and speed of `method=4` against
`method=6` were not measured (a benchmark was started and stopped
before its `method=6` encodes finished); the choice rests on how
little the WebP decides. WebPs already cached at `method=6` are kept;
the cache name does not carry the method.

### What the full export stored (2026-09-23)

A Pelican export of the whole archive, every candidate made (the
WebP skipped for 55 gifs by the rule above; no capped gif for
`11e5dab7c54/006`), read with `master_sizes.py`:

| | gifs | MB | of 586.6 MB of gifs |
|---|---|---|---|
| **stored under the rule (share 0.75)** | 216 | **128.9** | **22.0%** |
| as AV1 4:4:4 CRF 28 | 179 | 76.5 | |
| as lossless WebP | 25 | 40.3 | |
| as the capped gif | 12 | 12.2 | |
| AV1 everywhere | 216 | 125.1 | 21.3% |
| H.264 everywhere (what the other sites carry) | 216 | 146.4 | 25.0% |
| the capped gif everywhere | 215 | 538.1 | 91.7% |

- **Preferring lossless costs 3.8 MB net** (3%) against AV1
  everywhere, for 37 lossless masters. 15 of them are also smaller
  than their AV1 master (2.9 MB less in all): the clean UI recordings
  of `edb3f80dc1c0`, `789fcb1a5857/001` and `/003`, and the particle
  recording `cda20dc15a21/005`, where AV1 does badly. The other 22 are
  larger than their AV1 master (6.8 MB more), kept because the master
  saved less than 25%.
- **Two gifs carry most of that cost.** `bd2524b247c2/005`, the
  dithered chart: WebP 12.31 MB against AV1 9.25 MB (+3.06 MB), its
  master at 75.1% of the WebP, just over the line (`--share 0.76`
  stores AV1). `f8151c2cc6e8/003`: WebP 8.09 MB against AV1 6.60 MB
  (+1.49 MB; AV1 at 81%). Both are where AV1 visibly changes the
  content: `bd2524`'s dither (43.0 dB, 0.63% of pixels visibly
  changed at CRF 28) and `f8151c/003`'s worst frames (33.9 dB at
  CRF 24). The served H.264 is about the same either way (4:2:0 loses
  `bd2524`'s dither whatever it is made from); the lossless master is
  for later formats.
- **gifsicle `-O3`** mostly saves little (85-100% of the gif), except
  on two badly optimized gifs (`2e432df402c8/007` and `/008`, 6%).
- **WebP** ranged from 4% to 468% of the gif and won on clean UI
  recordings (`edb3f80dc1c0`, `f6e2e41ab3fa`, `8096b8b223d0`,
  `789fcb1a5857`) and on `bd2524` and `f8151c/003`.
- **The capped gif of `cda20dc15a21/010`** came out at 248% of the
  gif: the cap drops 9 of its 105 frames, and the whole frames written
  in their place lose the gif's partial-frame coding. It is never
  chosen.
- **Time:** the WebP at `method=6` took hours of that export, where
  the capped gif takes seconds a gif; hence the skip rule and
  `method=4` above.

### Decisions (2026-09-23)

- The Pelican site stores one master of each animation, frame-rate
  capped at 29.5 ms (up to ~33 fps, the nearest gif step to 30 fps)
  and at full size: the smaller of the capped gif (gifsicle `-O3`) and
  lossless WebP, unless the AV1 4:4:4 CRF 28 master is at most 75% of
  it (`master_max_share = 0.75`, kept after the full export: the
  lossless copies it keeps cost 3.8 MB of 128.9 MB). Its build makes
  the H.264 it serves from the master.
- AV1 at CRF 28, not 24: 27% smaller masters for about 1.5 dB, and
  H.264 made from either within 0.3 dB. `tune-content=screen` is not
  used.
- AV1 4:4:4 is stored, not served (software decoding).
- The WebP is encoded at cwebp's defaults (`method=4`,
  `quality=75`), and only where it could be stored
  (`WEBP_MIN_SHARE = 0.4`).
- Not candidates: lossless H.264 in RGB (179-624% of the gif on the
  gifs AV1 does badly), JPEG XL, APNG (finding 1).
- The seven gifs whose last frame is 10 ms keep it; they get no WebP,
  which changed nothing.
- A single-frame gif is placed as a PNG would be (finding 9).

`docs/gif-intermediates/master_sizes.py` lists, from the image cache
after a Pelican export, every candidate of every gif as a share of
the gif, which one the site stores, and the totals by winner.

### Why AV1 4:4:4, and why it is not served

AV1 4:4:4 keeps the colored text, 1-pixel lines and dither that 4:2:0
loses (finding 2), at a size near 4:2:0 H.264. It is not served:
the hardware AV1 decoders in current devices implement the Main
profile (4:2:0), as far as is known here, so Chrome and Firefox decode
4:4:4 in software, and Safari, which decodes AV1 only in hardware,
presumably not at all (not checked). The clips autoplay and loop while on screen, so that is a
steady CPU load on a phone, where 4:2:0 H.264 has a hardware decoder
everywhere.

### Full-archive measurement (2026-09-23, stopped)

`docs/gif-intermediates/archive_measure.py` encodes every animated gif
(largest first) as H.264 straight from the gif (`direct`, exactly as
`sites.py`), as AV1 4:4:4 masters at CRF 24 and 28, H.264 from each
master (`.h264`), and AV1 4:2:0 CRF 24 from the CRF 24 master (the
hardware-decodable AV1 a later site could serve), and scores each
against the gif with `quality.py`. Toolchain: ffmpeg 9.0.2, libaom
3.14.1, x264 164.3095 (the `docs/gif-intermediates` pixi
environment).

- **direct H.264, 214 of the 215 animated gifs:** 144.8 MB, 25.3% of
  572.3 MB of gifs (the Ubuntu ffmpeg 6.1 build above: 152.3 MB).
- **The 14 largest gifs, every output** (203.5 MB of gifs):

| output | size | vs gifs | median PSNR | worst frame (median) | visibly changed (median) |
|---|---|---|---|---|---|
| direct H.264 CRF 24 | 26.3 MB | 12.9% | 38.6 dB | 35.5 dB | 1.07% |
| AV1 4:4:4 CRF 24 master | 42.2 MB | 20.7% | 44.5 dB | 39.2 dB | 0.05% |
| AV1 4:4:4 CRF 28 master | 30.8 MB | 15.2% | 42.9 dB | 38.2 dB | 0.11% |
| H.264 from the CRF 24 master | 22.8 MB | 11.2% | 38.1 dB | 35.0 dB | 1.27% |
| H.264 from the CRF 28 master | 22.2 MB | 10.9% | 37.8 dB | 34.6 dB | 1.36% |
| AV1 4:2:0 CRF 24 from the CRF 24 master | 23.1 MB | 11.4% | 40.0 dB | 36.4 dB | 0.64% |

Every output ran exactly as long as its gif.

- **CRF 28 against CRF 24 for the master:** 27% smaller for about
  1.5 dB. The H.264 made from either is within 0.3 dB: the 4:2:0
  encode loses far more than the master does. Hence CRF 28. The 4:4:4
  ladder had small text clean at 28 and first artifacts at 34
  (finding 4).
- **H.264 from a master against H.264 from the gif:** 0.5-0.8 dB
  lower median PSNR and a little smaller, the cost of a second lossy
  step. A master stored losslessly has no such cost.
- **`tune-content=screen`** (CRF 24, the first 11 gifs): 2% smaller in
  total, but about twice the encode time on the files it changes.
  Unchanged (within 0.1%) on four, where libaom had already detected
  screen content; 15-19% smaller and 0.3-0.6 dB better on three; 12% larger
  and 0.6 dB better on one. Not used.

### Masters larger than their gifs

Clean UI recordings with a few changing pixels a frame are what gif
codes best. Two examples, `edb3f80dc1c0/006` (2000x1200, 150 frames,
765 KB) and `789fcb1a5857/001` (1918x968, 169 frames, 849 KB), about
150 colors a frame: AV1 CRF 28 was 113% and 130% of the gif, and
larger than at CRF 24. Size does not fall steadily with CRF there,
while quality does. `edb3f80dc1c0/006`, AV1 4:4:4, `-cpu-used 6`:

| CRF | 20 | 24 | 26 | 28 | 30 | 32 | 36 |
|---|---|---|---|---|---|---|---|
| KB | 877 | 836 | 802 | 868 | 757 | 1,011 | 879 |
| PSNR, dB | 50.2 | 50.0 | 49.6 | 49.2 | 48.8 | 47.9 | 47.0 |

Almost all the bytes are one keyframe of flat UI, whose coding flips
between CRFs. `tune-content=screen` produced byte-identical files at
every CRF. These are the files the lossless candidates are for.

Lossless H.264 in RGB, best on the plain-screencast pilot (finding 1),
does not help on these: exact, but 286-287% of the gif on
`edb3f80dc1c0/006`, 607-624% on `789fcb1a5857/001` and 179-362% on
the particle recording `cda20dc15a21/005` (`-preset placebo` /
`veryslow`).

## Later: re-encoding to AV1

AV1 is not served now because Safari plays it only on Apple M3 or
later hardware (2026-09). Once it plays broadly, re-encoding from the
gif masters is a change to the codec arguments and `CACHE_SCHEME`,
plus a longer cold cache. For the Pelican site, it is a change to its
plugin's encode, made from the masters the site stores.

What AV1 gains, from the seven-file sample:

- **About 30% smaller in 4:2:0 at equal quality:** libaom CRF 24 was
  20% of the gifs against H.264 CRF 24's 28%, both at 36.6 dB median.
  CRF 28 is 17% for a small drop.
- **Much smaller on line-drawing content:** libaom 4:2:0 was 70-74% of
  the particle gifs against 155% for H.264. Its screen-content tools
  (palette mode and intra block copy) work in 4:2:0 too.
- **Full color (4:4:4), if browsers decode it:** Chrome and Firefox do
  today. AV1 4:4:4 CRF 28 was 27% of the gifs at 47.6 dB median, and
  it keeps colored text, 1-pixel lines and dither that 4:2:0 loses.
  CRF 28 was clean on small text; CRF 34 showed the first artifacts.

What it costs:

- **Encode time:** libaom at `-cpu-used 6` encodes about 2.5 times
  slower than H.264 `-preset slower`, and `-cpu-used 4` is slower
  still with no gain in size. The image cache absorbs this, as it does
  H.264.
- **SVT-AV1** is fast, but it is not the encoder to use (finding 3).

A possible intermediate step: a `<video>` can list several
`<source>` elements, and the browser plays the first one it
supports. The site could serve AV1 with an H.264 fallback before AV1
is universal, at the cost of encoding and storing both.

## Tools (`docs/gif-intermediates/`)

These tools were for research only. The build does not import or run
them; the frame-rate cap it uses is its own implementation
(`kept_frames` and `select_frames` in `sites.py`). The tools need the
pixi environment below: `bench.py` passes filter scripts with ffmpeg's
`-/vf` option, which older ffmpeg versions do not have.

- `pixi.toml`, `pixi.lock`: the pinned conda-forge toolchain (ffmpeg
  9.0.2, libaom 3.14.1, SVT-AV1 4.2.0, x264, libjxl 0.12, libwebp 1.6,
  gifsicle 1.96). Run tools with `pixi run --manifest-path
  docs/gif-intermediates/pixi.toml ...`. In a Claude Code cloud
  session, pixi.sh and GitHub downloads are blocked, but
  conda.anaconda.org is reachable, so pixi was installed by extracting
  it from its conda-forge package.
- `inventory.py`, `inventory.tsv`: the survey of all 216 gifs.
- `bench.py`: encodes gif × encoder jobs in parallel and checks each
  against the gif (timeline of frame hashes and display time). It
  calls `quality.py` for inexact encodes. Encoders are named by their
  settings, for example `x264_420_crf24_cap`, `aom420_c6_crf28_cap`
  or `svt420_p6_crf30_cap`.
- `quality.py`: PSNR (overall and worst frame), largest error and the
  share of visibly changed pixels. It pairs each encoded frame with
  the gif frame on screen at the same moment, and saves the worst
  frame for inspection.
- `fpscap.py`: the frame-rate cap as an ffmpeg filter script, and
  `--gif` for an exact capped gif (Pillow, then gifsicle).
- `pil_encode.py`: APNG and WebP with exact delays (Pillow and
  img2webp).
- `archive_measure.py`: the full-archive measurement of the Pelican
  site's masters above (resumable; one JSON line per output).
- `master_sizes.py`: after a Pelican export, each gif's candidate
  masters from the image cache as a share of the gif, the one the
  site stores, and the totals. Needs only Python; `--share` re-runs
  the choice at another threshold, `--crf` compares other AV1
  masters.

## Not done

- **Browser check.** No clip from a built site has been watched in a
  browser yet. Worth checking: the largest (3,340x1,517) and a
  `cda20dc15a21` particle clip.
- **Nine larger clips.** Of the 11 clips larger than their gifs, the
  nine outside `cda20dc15a21`: two (`edb3f80dc1c0/006`,
  `789fcb1a5857/001`) are clean UI recordings (see
  [Masters larger than their gifs](#masters-larger-than-their-gifs));
  the other seven have not been looked at.
- **The full-archive master measurement** (`archive_measure.py`) was
  stopped once the Pelican export answered the storage question:
  direct H.264 for 214 of the 215 animated gifs, and every output
  (masters, H.264 and AV1 4:2:0 made from them) for the 29 largest.
  It resumes from its results file.
- **WebP effort.** `method=4` against `method=6` was not measured on
  this archive.
- **Visual check of the masters.** CRF 28 4:4:4 was inspected on the
  seven-file ladder only; nobody has looked at a CRF 28 master of the
  archive's other gifs, or at the H.264 made from one.
