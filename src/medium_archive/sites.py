"""Machinery shared by the site exporters (myst, hugo, pelican).

Each exporter derives a ready-to-render site from the converted archive --
archive/posts.json and archive/posts/ -- so every site is as reproducible
as the posts are: raw/ + fixups/ -> convert -> posts/ -> exporter -> site dir.
None of them touch the network; rendering is the site generator's job.

Common to all of them: page URL slugs chosen from the Medium slug,
folded to ASCII and date-prefixed only when several posts share one,
links between posts of
the publication rewritten from Medium URLs to site pages, images placed
from posts/ (hard-linked as they are when nothing is to be gained,
else display copies -- see ImagePlacer), a redirect map from every old
inbound path to its page URL, tag names from the archive's tags.json
`display` map (the tags themselves stay slugs -- spaces and capitals are
a display concern, so nothing a URL is built from moves), and site-wide
text (title, description, landing-page intro, optional base_url) from the
hand-written site/site.toml.
"""

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import sys
import unicodedata
from importlib import resources
from pathlib import Path
from string import Template
from urllib.parse import unquote, urlsplit

import yaml

from .lint import split_post
from .paths import site_config
from .pages import markdown_text
from .siteconf import documented_toml, load_toml
from .tags import display_name, load_tag_display
from .urls import medium_id

# The site scaffolding -- generator configs, themes, CSS, and the JS
# snippets shared between generators -- lives as real files under
# templates/ (see templates/README.md), copied into each site as it is
# built. An `@include <path>` marker line (HTML- or CSS-comment form)
# splices a templates/-relative file into the one that carries it, which
# is how the hugo and pelican themes share their snippets. *.tmpl files
# take config values through string.Template, whose $placeholders cannot
# collide with the braces the generators' own template languages use.
TEMPLATE_DIR = resources.files(__package__) / "templates"
_INCLUDE_RE = re.compile(r"(?:<!--|/\*) @include ([\w./-]+) (?:-->|\*/)\n")


def template_text(rel: str) -> str:
    """templates/<rel>, with @include markers expanded."""
    text = (TEMPLATE_DIR / rel).read_text(encoding="utf-8")
    return _INCLUDE_RE.sub(lambda m: template_text(m.group(1)), text)


def fill_template(rel: str, **values) -> str:
    """templates/<rel> (a .tmpl) with its $placeholders substituted;
    every value must arrive already serialized for the config's format."""
    return Template(template_text(rel)).substitute(values)


def write_templates(site: Path, templates: dict):
    """A theme into the site: file in the site -> its templates/ source."""
    for rel, src in templates.items():
        path = site / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template_text(src), encoding="utf-8")


def copy_site_asset(inputs: Path, rel, dst_dir: Path, stem: str):
    """An image site.toml names (the header avatar, the tab icon),
    resolved beside site.toml itself so the site inputs move as one
    directory, copied into the site as dst_dir/<stem><its extension> so
    the site stays self-contained; the file name written, or None when
    rel is unset or the file is missing (noted)."""
    if not rel:
        return None
    src = inputs / rel
    if not src.is_file():
        print(f"{stem} not found, skipped: {src}", file=sys.stderr)
        return None
    dst = dst_dir / (stem + src.suffix)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst.name


# The <figure> shell convert writes around a captioned image
# (link-wrapped or not), in the exact shape _Converter emits it: tag
# lines and the single image and caption lines between them, all
# blank-line separated. Groups: link-open marker, alt, src, link,
# caption.
# The player line convert writes for a kept embed (convert.embed_iframe):
# src, then title, then the fixed attributes
IFRAME_RE = re.compile(r'^<iframe src="([^"]+)" title="([^"]*)"[^\n]*></iframe>$', re.M)
# The looping clip convert writes for a Giphy mp4 embed (_Converter.convert_video)
VIDEO_RE = re.compile(r'^<video src="([^"]+)"[^\n]*></video>$', re.M)

FIGURE_SHELL_RE = re.compile(
    r"<figure>\n\n(\[)?!\[([^\]\n]*)\]\(([^)\s]+)\)(?(1)\]\(([^)\s]+)\))\n\n"
    r"<figcaption>\n\n([^\n]+)\n\n</figcaption>\n\n</figure>")


def rewrite_figures(markdown: str, render) -> str:
    """Each captioned-image shell as render(alt, src, link, caption)
    returns it -- link is None when the image is not wrapped in one --
    or left as it is when render returns None. A shell around anything
    else (the link an embed became, an inlined gist) is not touched."""
    def sub(m):
        _, alt, src, link, caption = m.groups()
        out = render(alt, src, link, caption)
        return m.group(0) if out is None else out
    return FIGURE_SHELL_RE.sub(sub, markdown)

# Covers above this are skipped in favor of the post's next image: themes
# and exporters thumbnail or encode each cover, and Medium archives carry
# the odd 25-megapixel screenshot, which is slow to process (or, past an
# encoder's memory, fails the build).
MAX_COVER_PIXELS = 12_000_000

P_PATH_RE = re.compile(r"^/p/([0-9a-f]{8,12})$")   # Medium's short post URL
LINK_RE = re.compile(r"\]\((https?://[^)\s]+)\)")  # inline [text](url)
AUTOLINK_RE = re.compile(r"<(https?://[^>\s]+)>")  # autolink <url>


def load_site_inputs(archive: Path, inputs: Path):
    """(manifest, site.toml config) for an exporter, or exit. The two
    inputs are named apart because they are: the archive is generated
    (convert writes posts.json), the site inputs are hand-written and
    keep no copy of the archive."""
    manifest_path = archive / "posts.json"
    if not manifest_path.exists():
        sys.exit(f"nothing to build: {manifest_path} missing (run convert first)")
    manifest = json.loads(manifest_path.read_text())
    if not manifest:
        sys.exit("nothing to build: posts.json is empty (run convert first)")
    config = {"title": "Blog archive", "description": "", "intro": ""}
    if site_config(inputs).exists():
        config.update(load_toml(site_config(inputs)))
    elif (legacy := site_config(inputs).with_suffix(".json")).exists():
        # The file was JSON until each key was given its own
        # documentation, which JSON has nowhere to put. Say so rather
        # than build a site named "Blog archive" from defaults.
        sys.exit(f"{legacy} is no longer read: the site's own data is now "
                 f"{site_config(inputs).name}, TOML so that every key can "
                 "carry what it is for beside it. Convert it (the keys "
                 "are unchanged) and delete the old file.")
    # Optional keys both card themes read: "noindex" (true keeps search
    # engines off the whole site -- a preview deployment, which would
    # otherwise be indexed as a copy of the real one -- through a
    # robots meta tag on every page and a robots.txt that disallows
    # all), "twitter" (the publication's @handle, for twitter:site),
    # "profiles" (its addresses elsewhere, for the Organization's
    # sameAs -- see site_profiles) and "share_image" (a raster beside
    # site.toml, the og:image of every page without a cover of its own).
    # Absolute links -- feed URLs, redirect stubs, the Open Graph tags
    # and the share links a reader hands to LinkedIn or Facebook -- are
    # built from base_url. Without it each exporter falls back to a
    # placeholder, which every one of those silently points at someone
    # else's domain, so say so once here rather than let the build look
    # clean until a share link is clicked in the wild.
    if not config.get("base_url"):
        print("site.toml has no base_url: absolute links (feeds, redirect "
              "stubs, Open Graph tags, share links) will not point at this "
              "site. Set it to the domain the site is served from and "
              "re-run.", file=sys.stderr)
    return manifest, config


def tag_names(manifest: dict, archive: Path) -> dict:
    """Every tag the archive uses -> the name a site shows it under.
    Tags stay slugs through posts.json and into each site's tag URLs;
    tags.json's `display` map is what gives them their spaces and
    capitals at the point they are rendered."""
    display = load_tag_display(archive)
    return {tag: display_name(tag, display) for p in manifest.values()
            for tag in p.get("tags") or []}


def page_name(post: dict) -> str:
    """One post's page name before duplicates are told apart: its Medium
    slug folded to ASCII (see ascii_slug). Medium leaves a title's
    accents in the path it builds -- voilà-0-5-0-homecoming -- and a URL
    is no place for them: the address is only ever seen percent-encoded
    (/posts/voil%C3%A0-0-5-0-homecoming/), each generator folds what it
    is given its own way, and a filesystem is free to hand the name back
    decomposed. So the page is served at voila-0-5-0-homecoming. Medium's
    own spelling is kept where it is a fact rather than a choice: in
    posts.json, and on the old-path side of the redirect map.

    A slug with no ASCII in it at all -- a publication that writes its
    titles in another script -- folds away to nothing, and the Medium id
    its URL ends in names the page instead.

    Cut to SLUG_MAX at a word boundary (see truncate_slug): the page is
    filed and served under its year, so the slug carries the title and
    not the date, and it need not be long enough to be unique on its
    own."""
    return truncate_slug(ascii_slug(post["slug"])
                         or ascii_slug(post.get("medium_id") or "") or "post")


def page_dir_name(post: dict) -> str:
    """The name of the directory a post's page sits in, where a site
    keeps the archive's own <YYYY-MM-DD>-<slug> grouping rather than
    naming the directory for the page (myst does; hugo and pelican name
    theirs page_stems' way). Folded to ASCII like the page name beside
    it: Medium's spelling of a slug is the archive's business, and a
    site built from it should carry no accent anywhere -- least of all
    in a path a checked-in site would hold."""
    return ascii_slug(Path(post["dir"]).name)


def page_stems(manifest: dict) -> dict:
    """url -> page name, which becomes the page's URL slug: page_name's,
    unless several posts of one year share it.

    A post is filed and served under its publish year, so a name has
    only to be unique within that year -- which is what makes the
    archive's two yearly series (the distinguished-contributor and
    community-workshop announcements, six and three posts sharing one
    Medium slug apiece) need nothing at all: no two of them fall in the
    same year. A collision inside one year keeps the rest of its date,
    so the pages stay distinct and each address stays stable -- with
    room made for it, so that no name outgrows SLUG_MAX."""
    names = {url: page_name(p) for url, p in manifest.items()}
    counts = {}
    for url, name in names.items():
        key = (post_year(manifest[url]), name)
        counts[key] = counts.get(key, 0) + 1
    return {url: name if counts[(post_year(manifest[url]), name)] == 1
            else (truncate_slug(name, SLUG_MAX - len("-01-01")) + "-"
                  + ((manifest[url]["date"] or "")[5:10] or "undated"))
            for url, name in names.items()}


def page_paths(manifest: dict, stems: dict) -> dict:
    """url -> the site-relative address the hugo and pelican sites serve
    the post at: /posts/<year>/<stem>/. The year is the publish year
    (post_year), so a reader seeing an address can date the post before
    opening it, and the years an archive spans are that many
    directories rather than one of several hundred."""
    return {url: f"/posts/{post_year(p)}/{stems[url]}/"
            for url, p in manifest.items()}


class LinkMap:
    """Resolve URLs that point at posts of this publication -- by exact
    host+path (Medium, Ghost-era, or /p/<id> form, http or https, with or
    without a trailing slash or percent-encoding) or by the Medium id a
    slug ends in -- to the post's site page."""

    def __init__(self, manifest: dict, stems: dict):
        self.by_path, self.by_id = {}, {}
        for url, p in manifest.items():
            # (post dir, page name, publish year)
            page = (page_dir_name(p), stems[url], post_year(p))
            for u in (p["original_url"], p.get("ghost_url"),
                      p.get("canonical_url")):
                if u:
                    parts = urlsplit(u)
                    self.by_path[(parts.netloc.lower(),
                                  unquote(parts.path).rstrip("/"))] = page
            if p.get("medium_id"):
                self.by_id[p["medium_id"]] = page

    def page_for(self, url: str):
        """(post dir, page name, publish year, fragment) or None."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return None
        path = unquote(parts.path).rstrip("/")
        hit = self.by_path.get((parts.netloc.lower(), path))
        if hit is None:
            m = P_PATH_RE.match(path)
            mid = m.group(1) if m else medium_id(url)
            hit = self.by_id.get(mid) if mid else None
        return (*hit, parts.fragment) if hit else None


def rewrite_body(markdown: str, target_for, escape=None) -> str:
    """Rewrite inline links and autolinks whose URL target_for() resolves
    (a post of this publication) to the returned site target, and run
    each prose line through escape() when given. Fenced code is left
    alone: a URL there is content."""
    def inline(m):
        return f"]({target_for(m.group(1)) or m.group(1)})"

    def auto(m):
        target = target_for(m.group(1))
        return f"[{m.group(1)}]({target})" if target else m.group(0)

    out, fence = [], False
    for line in markdown.split("\n"):
        if re.match(r"^`{3,}", line):
            fence = not fence
        elif not fence:
            line = LINK_RE.sub(inline, line)
            line = AUTOLINK_RE.sub(auto, line)
            if escape is not None:
                line = escape(line)
        out.append(line)
    return "\n".join(out)


def retarget_images(text: str, renames: dict) -> str:
    """A page's text with its images/<name> references pointed at the
    names actually placed beside it -- a display copy that changed
    format (see ImagePlacer.place) lands under a new extension."""
    if not renames:
        return text
    pattern = re.compile(r"images/(%s)\b"
                         % "|".join(re.escape(n) for n in renames))
    return pattern.sub(lambda m: "images/" + renames[m.group(1)], text)


def image_size(path):
    """(width, height) read from a PNG/GIF/JPEG header, or None (an
    unknown format, or a header cut short)."""
    with open(path, "rb") as fh:
        head = fh.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
            return struct.unpack(">II", head[16:24])
        if head[:3] == b"GIF" and len(head) >= 10:
            return struct.unpack("<HH", head[6:10])
        if head[:2] == b"\xff\xd8":              # JPEG: find an SOF marker
            fh.seek(2)
            while True:
                marker = fh.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return None
                length = struct.unpack(">H", fh.read(2))[0]
                if 0xC0 <= marker[1] <= 0xCF and marker[1] not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", fh.read(5)[1:])
                    return w, h
                fh.seek(length - 2, 1)
    return None


# What a summary-card cover may be: the raster formats every card
# template and Pillow decode. Stills first; a gif only when the post has
# no still, since the bake keeps just its first frame (a still cover.jpg
# like any other, and og:image must be a still anyway) and a real still
# composes a card better than frame one of an animation. Not svg (a
# badge from shields.io re-hosted by Medium is the usual one), not the
# .bin of unrecognized bytes. Names are trusted: convert already typed
# the extensionless downloads by their bytes.
COVER_EXTS = (".png", ".jpg", ".jpeg", ".webp")
COVER_FALLBACK_EXTS = (".gif",)


def pick_cover(post: dict, post_dir) -> str | None:
    """The post's first raster still of sane size, for its summary card
    -- see COVER_EXTS -- else its first gif (COVER_FALLBACK_EXTS: the
    cover is the gif's first frame); an enormous one is passed over
    either way (slow or worse, see MAX_COVER_PIXELS). Only decodable
    formats qualify: the baked cover is served as cover.jpg whatever it
    was, and Hugo's card template rasterizes it (.Fill), which aborts
    the whole build on an svg it cannot decode."""
    for exts in (COVER_EXTS, COVER_FALLBACK_EXTS):
        for image in post.get("images", ()):
            if not image.lower().endswith(exts):
                continue
            try:
                size = image_size(post_dir / image)
            except OSError:
                continue
            if size is None or size[0] * size[1] <= MAX_COVER_PIXELS:
                return image
    return None


def link_or_copy(src: Path, dst: Path):
    """src at dst, as a hard link when the two share a filesystem --
    posts/ and the image cache already hold the bytes, so a site costs
    no second copy of them -- and as a copy when they do not.

    dst is replaced when it exists: a rebuild writes over the site it
    built last time, and git breaks the link whenever it writes the
    file itself (checkout, stash, merge), so relinking is the common
    case rather than the exception. Removing dst first also keeps the
    write off the inode the cache shares: an in-place write would
    change the cached copy under its content-addressed name."""
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


# Card covers render in a 640x360 (16:9) frame on both card themes (hugo,
# pelican). A center crop composes photos and screenshots well, but the
# archives are full of logo covers -- wordmarks up to 7:1, square project
# logos -- whose meaning spans edge to edge, and a crop guts those. So
# sources near 16:9 are cropped and sources far from it are letterboxed
# instead: scaled to fit the frame (tiny logos at most 2x, not pixelated
# to fill) over the image's own border color when the border is uniform
# (a logo on white pads invisibly), else over a blurred cover-crop of the
# image itself.
COVER_SIZE = (640, 360)
COVER_CROP_ASPECTS = (1.3, 2.4)   # crop within this band, letterbox outside
COVER_MAX_UPSCALE = 2.0


def _flatten_rgb(im):
    """RGB, any transparency composited onto white: convert()'s default
    is black, which turns a dark-on-transparent logo into an illegible
    dark-on-dark card."""
    from PIL import Image
    if im.mode == "P":
        im = im.convert("RGBA" if "transparency" in im.info else "RGB")
    if im.mode in ("RGBA", "LA"):
        flat = Image.new("RGB", im.size, "white")
        flat.paste(im, mask=im.getchannel("A"))
        return flat
    return im.convert("RGB")


def _border_color(im):
    """The single color the image's 1px border is close to, or None.
    Uniformity tolerates compression noise and antialiased content
    touching the edge."""
    w, h = im.size
    raw = b"".join(im.crop(box).tobytes() for box in
                   ((0, 0, w, 1), (0, h - 1, w, h),
                    (0, 0, 1, h), (w - 1, 0, w, h)))
    edges = [raw[i:i + 3] for i in range(0, len(raw), 3)]
    n = len(edges)
    median = tuple(sorted(p[c] for p in edges)[n // 2] for c in range(3))
    near = sum(all(abs(p[c] - median[c]) <= 16 for c in range(3))
               for p in edges)
    return median if near >= n * 0.9 else None


def _letterbox(im):
    """im centered in the 640x360 frame: on its border color when the
    border is uniform, else on a blurred cover-crop of itself."""
    from PIL import Image, ImageFilter, ImageOps
    tw, th = COVER_SIZE
    color = _border_color(im)
    if color is None:
        canvas = ImageOps.fit(im, COVER_SIZE, Image.Resampling.LANCZOS)
        canvas = canvas.filter(ImageFilter.GaussianBlur(20))
    else:
        canvas = Image.new("RGB", COVER_SIZE, color)
    scale = min(tw / im.width, th / im.height, COVER_MAX_UPSCALE)
    fg = im.resize((max(1, round(im.width * scale)),
                    max(1, round(im.height * scale))),
                   Image.Resampling.LANCZOS)
    canvas.paste(fg, ((tw - fg.width) // 2, (th - fg.height) // 2))
    return canvas


def make_cover_thumbnail(src, dst) -> bool:
    """640x360 JPEG for a post's summary card, a fraction of the archived
    full-resolution original: center-cropped when the source is near
    16:9, letterboxed when far from it (see COVER_SIZE and friends)."""
    from PIL import Image, ImageOps
    try:
        with Image.open(src) as im:
            im = _flatten_rgb(im)
            lo, hi = COVER_CROP_ASPECTS
            if lo <= im.width / im.height <= hi:
                thumb = ImageOps.fit(im, COVER_SIZE, Image.Resampling.LANCZOS)
            else:
                thumb = _letterbox(im)
            thumb.save(dst, "JPEG", quality=80, optimize=True,
                       progressive=True)
        return True
    except OSError as e:
        print(f"cover thumbnail failed ({e}); using original: {src}",
              file=sys.stderr)
        return False


class Covers:
    """The summary-card cover of every post that has one -- its first
    raster still of sane size, else its first gif (pick_cover) -- as the
    pages reference it and as it is baked beside them. With Pillow the
    reference is the 640x360 images/cover.jpg bake() writes (a gif's
    first frame); without it (noted once) the card uses the full-size
    image under its own name, an animated gif included."""

    def __init__(self, archive: Path, manifest: dict, shown_as="card covers"):
        self.archive, self.manifest = archive, manifest
        try:
            from PIL import Image                      # noqa: F401
            self.pillow = True
        except ImportError:
            self.pillow = False
            print(f"pillow not installed: {shown_as} keep full-size images "
                  "(`pip install pillow` and re-run for 640x360 thumbnails)",
                  file=sys.stderr)
        self.picked = {url: cover for url, p in manifest.items()
                       if (cover := pick_cover(p, archive / p["dir"]))}

    def path(self, url: str) -> str | None:
        """The page-relative cover path a post's front matter carries."""
        cover = self.picked.get(url)
        if cover and self.pillow:
            return "images/cover.jpg"
        return cover

    def bake(self, url: str, page_dir: Path):
        """The post's images/cover.jpg beside its page, once the page's
        images are placed. An image that defeats Pillow is copied in
        unchanged -- the extension is cosmetic."""
        if not (self.pillow and url in self.picked):
            return
        src = self.archive / self.manifest[url]["dir"] / self.picked[url]
        dst = page_dir / "images" / "cover.jpg"
        dst.parent.mkdir(exist_ok=True)
        if not make_cover_thumbnail(src, dst):
            shutil.copy2(src, dst)


# Sites carry display copies of the archive's images, not the archival
# originals -- raw/ and posts/ keep those at full resolution -- so
# photographs past these caps are resized down to them (longest edge) as
# they are placed into a site. 1600 px keeps them sharp past the card
# themes' widest srcset variant (1104 px). Animations are not capped:
# they are screencasts, line art that moves, and a reader who takes a
# clip full screen to read its code has only the pixels it was encoded
# with -- a 1104 px cap shrank 129 of this archive's 216 gifs, to a
# median 74% of their width and the largest, 3340 px wide, to a third.
# It costs bytes where scaling would have smoothed dither or video away
# (on a sample of seven, the clips came out 27% larger than at a
# 1472 px cap and 64% larger than at 1104 px); plain screen text
# barely shrinks scaled.
# site.toml overrides either cap ([images] with still_max_edge /
# animated_max_edge, 0 = leave that kind untouched).
#
# Line art -- the charts, screenshots and diagrams that most of this
# archive's PNGs are -- is exempt from the still cap and never encoded
# lossily. Its meaning sits in 9 px text and single-pixel strokes, which
# downscaling destroys: on a survey chart, ink contrast measured 3.4:1
# at the source's 1430 px but 2.3:1 at the 736 px variant a phone picks,
# well under the 3:1 that small text needs. Nor does downscaling buy
# much, since flat color compresses by run length rather than by pixel
# count -- lossless encodes of most of this archive's line art come out
# *larger* downscaled, as antialiasing invents intermediate colors.
# Lossless webp of the full-resolution original instead is pixel-exact
# and still ~60% smaller than the source PNG.
STILL_MAX_EDGE = 1600
ANIMATED_MAX_EDGE = 0
STILL_EXTS = (".png", ".jpg", ".jpeg", ".webp")

# An animation is placed as h264 video rather than as a gif, every one
# of them. The same frames mostly cost a fraction of the bytes, but the
# reason is that a <video> is something a reader can stop. An animated
# gif cannot be paused, and one that runs past five seconds fails WCAG
# 2.2.2 (Pause, Stop, Hide) in that format no matter what the theme
# does; a clip can be paused, replayed, and left unplayed for a reader
# who asks for less motion. So a clip is placed even where it comes out
# larger than its gif -- as it does for a few animations of sparse
# 1-pixel lines, which gif's palette and LZW code for almost nothing and
# a transform codec cannot. What that costs is carried deliberately
# elsewhere: a <video> has no alt attribute, so the exporters put the
# image's text alternative on it as an aria-label (SC 1.2.1), and the
# poster below is what stands in for the movement.
#
# The encode was chosen by measurement against the gifs (2026-09; see
# docs/gif-intermediates.md): yuv420p and the high profile are what
# every browser decodes -- 4:2:0 color is what softens colored text,
# and no CRF buys that back -- and at -crf 24 -preset slower text
# looked the same as at crf 16 and 20 while the clips came out a third
# smaller than at 20. Frame timestamps pass through untouched, on a
# millisecond time base (left to itself, ffmpeg rounds every one to a
# guessed frame rate), and odd dimensions are padded by a pixel rather
# than scaled (see video_size). Frames inside a burst faster than
# ~30 fps are dropped first (see kept_frames).
#
# site.toml's [images] table tunes all of it: animated_format = "gif"
# keeps gifsicle's resized gifs instead, and video_crf / video_preset
# trade bytes against detail. animated_max_edge caps the longest edge;
# 0 leaves a clip at its own size rather than turning video off.
ANIMATED_FORMAT = "mp4"
VIDEO_CRF = 24
VIDEO_PRESET = "slower"
# A site that encodes its own clips when it is built -- the pelican
# site, whose plugin makes the served h264 from what the site stores --
# stores a master instead: AV1 in 4:4:4 color, frame-rate capped like
# a clip, at the gif's own size. A site checked in as a repository of
# its own keeps its media for good, and a format change later would
# otherwise mean committing a second copy of every clip. The master is
# what every later format is made from: 4:4:4 keeps the colored text,
# 1-pixel lines and dither that 4:2:0 loses, at about the bytes of
# today's h264 (docs/gif-intermediates.md: libaom 4:4:4 CRF 28 came to
# 27% of the gifs against h264 CRF 24's 28%, at 47.6 dB against 36.6).
# It is not served itself: browsers decode AV1 4:4:4 in software, if at
# all, where 4:2:0 h264 has a hardware decoder everywhere, and a clip
# loops for as long as it is on screen. CRF 28 is the step under the
# CRF 34 at which small text first showed artifacts in 4:4:4. Against
# CRF 24 on the archive's ten largest gifs it stored 29% less for about
# 1.5 dB, and the h264 made from either came out within 0.3 dB: the
# 4:2:0 encode loses far more than the master does. -cpu-used 6 was as
# small as 4 and far faster. site.toml's clip_master = "none" stores
# the h264 clip instead, and master_crf moves the CRF.
CLIP_MASTER = "av1"
CLIP_MASTERS = ("av1", "none")
MASTER_CRF = 28
# The master is lossy, so it is stored only where it pays: where it is
# at most this share of the smallest lossless copy of the animation,
# frame-rate capped the way a clip is -- the gif re-optimized by
# gifsicle -O3, or lossless WebP, which codes a screencast with video
# in it better than gif can (73-150% of the gif on the largest four in
# docs/gif-intermediates.md). Elsewhere that lossless copy is what the
# site stores, pixel for pixel: clean screen recordings that change a
# few pixels a frame are what gif codes best, and a master of one can
# come out larger than the gif. Either way the site serves h264 made
# from what it stores, and the poster travels beside it.
# site.toml's master_max_share moves the line (1 stores the smallest,
# 0 always a lossless copy); every candidate is cached, so a new line
# re-encodes nothing.
MASTER_MAX_SHARE = 0.75
CAPPED_GIF_SUFFIX = ".capped.gif"
CAPPED_WEBP_SUFFIX = ".capped.webp"
# Lossless WebP through Pillow, the writer that keeps each delay as
# given (gif2webp plays 10 ms as 100), at cwebp's default effort
# (method 4, quality 75; Pillow's own default for an animation is
# method 0) rather than the method 6 and quality 100
# docs/gif-intermediates.md measured: those are libwebp's slowest, and
# the WebP only decides what is stored for a handful of gifs, where
# the extra effort saved little.
# It is by far the slowest candidate -- hours for the archive, where
# the capped gif takes seconds a gif -- so it is only built where it
# could be stored: in the first full export's 113 WebPs, none came in
# under 44% of its capped gif, so where the master is at most master_max_share
# of this share of the capped gif, the master wins whatever the WebP
# would weigh, and the WebP is not made.
WEBP_LOSSLESS = {"lossless": True, "quality": 75, "method": 4}
WEBP_MIN_SHARE = 0.4
# The still a clip carries, beside it under this suffix. It is what a
# gif showed at rest (the clip's own first frame, so nothing jumps when
# playback starts), what a reduced-motion reader sees instead of
# movement, what a feed reader shows where it drops the <video>, and
# what the page paints while preload="none" fetches nothing -- which is
# what keeps a page from pulling several megabytes of clip nobody
# scrolls to. Named after the clip so a template finds it without being
# told, and webp because that is what the sites already ask a browser
# to decode for line art. It is written in the same ffmpeg pass as the
# clip -- one decode of the gif, and the still costs nothing measurable
# beside the encode -- and it is lossy, at a quality that leaves a
# screencast's text crisp: keeping a frame pixel-exact is effort spent
# on the one frame of a clip that is lossy from the next frame on, and
# through Pillow it measured longer than encoding the whole clip.
POSTER_SUFFIX = "-poster.webp"
POSTER_QUALITY = 90
# A clip drops the frames of a burst faster than a reader can follow:
# one is kept when it is the first or the last, when it stays on screen
# at least this long, or when it starts at least this long after the
# last frame kept (see kept_frames). That thins recordings made at 40,
# 50 or 100 fps to at most ~34 while every kept frame starts exactly
# when the gif shows it -- where ffmpeg's own fps=30 would move every
# frame boundary onto its grid -- and costs nothing where a gif has no
# such bursts, 205 of the reference archive's 216. The delays are the
# ones the gif stores, not the 100 ms browsers play delays of 10 ms or
# less at: the animation as its author made it.
MIN_FRAME_MS = 29.5

# A still is line art when it holds few enough distinct colors and
# enough flat runs. The two classes separate cleanly on that pair --
# this archive's line art runs 200-8000 colors at 0.55-0.98 flat, its
# photographs past 14000 colors at under 0.5 -- and it costs ~8 ms an
# image. Only PNGs are classified: a photograph saved as PNG re-encodes
# down the photo path, while a screenshot already in JPEG has taken its
# lossy hit and is left alone.
LINE_ART_MAX_COLORS = 8192
LINE_ART_MIN_FLAT = 0.55
# Full resolution is kept within this byte budget: a lossless encode
# over it retries lossy at a quality that leaves text crisp, and only an
# outlier -- a panorama tens of thousands of pixels wide -- is finally
# resized, to an edge far past what any body column asks for.
LINE_ART_MAX_BYTES = 500_000
LINE_ART_MAX_EDGE = 4000
LINE_ART_QUALITY = 90
PHOTO_QUALITY = 85
# Bumped when the copies a given cap produces change shape, so caches
# written by an older scheme are ignored rather than misread.
CACHE_SCHEME = "v7"


def poster_path(clip: Path) -> Path:
    """The poster beside a clip: <name>-poster.webp, wherever the clip
    is -- the cache, a post's images directory, a site's."""
    return clip.with_name(clip.stem + POSTER_SUFFIX)


def discard_copy(tmp: str):
    """Drop a display copy that is not going to be used, and the poster
    beside it when the copy was a clip."""
    Path(tmp).unlink(missing_ok=True)
    poster_path(Path(tmp)).unlink(missing_ok=True)


def video_size(size, cap: int):
    """The (width, height) a gif this size is scaled to: cap on its
    longest edge, or its own size under a cap of 0. yuv420p is defined
    for even dimensions only; the encode pads an odd one by a pixel
    (EVEN_PAD) rather than resampling the whole picture to lose it."""
    w, h = size
    if cap and max(w, h) > cap:
        scale = cap / max(w, h)
        w, h = max(1, round(w * scale)), max(1, round(h * scale))
    return w, h


EVEN_PAD = "pad=ceil(iw/2)*2:ceil(ih/2)*2"


def kept_frames(delays, min_ms: float = MIN_FRAME_MS) -> list[int]:
    """The indexes of the frames a clip keeps, given each frame's delay
    in ms: see MIN_FRAME_MS. A frame kept only for when it starts gives
    way to a following frame kept for its length when the two would be
    less than min_ms apart, so no kept frame is on screen for less."""
    keep, starts, t = [], [], 0
    by_start = False           # the last frame was kept only for its start
    last = len(delays) - 1
    for i, d in enumerate(delays):
        pinned = i == 0 or i == last or d >= min_ms
        if pinned or t - starts[-1] >= min_ms:
            if pinned and by_start and t - starts[-1] < min_ms:
                keep.pop()
                starts.pop()
            keep.append(i)
            starts.append(t)
            by_start = not pinned
        t += d
    return keep


def select_frames(keep: list[int]) -> str:
    """An ffmpeg select filter passing exactly the frames in keep. The
    runs of consecutive frames are summed as a balanced tree: ffmpeg's
    expression parser recurses on every +, and a flat sum of a few
    hundred terms fails ("Cannot allocate memory")."""
    runs = []
    for i in keep:
        if runs and runs[-1][1] == i - 1:
            runs[-1][1] = i
        else:
            runs.append([i, i])

    def tree(terms):
        if len(terms) == 1:
            return terms[0]
        mid = len(terms) // 2
        return f"({tree(terms[:mid])}+{tree(terms[mid:])})"
    return "select='" + tree([f"between(n,{a},{b})" for a, b in runs]) + "'"


class _TooManyColors(Exception):
    pass


class _KeptFrames:
    """The kept frames of an open gif as P images, each composited and
    given a palette of exactly its own colors, made one at a time as
    Pillow's writer asks for them (a long recording held whole runs to
    gigabytes). Iterable more than once, as the writer needs."""

    def __init__(self, im, keep):
        self.im, self.keep = im, keep

    def __iter__(self):
        import numpy as np
        from PIL import Image
        for i in self.keep:
            self.im.seek(i)
            rgb = np.asarray(self.im.convert("RGB"))
            flat = rgb.reshape(-1, 3).astype(np.uint32)
            key = flat[:, 0] << 16 | flat[:, 1] << 8 | flat[:, 2]
            colors, index = np.unique(key, return_inverse=True)
            if len(colors) > 256:
                raise _TooManyColors(i)
            frame = Image.fromarray(
                index.reshape(rgb.shape[:2]).astype(np.uint8), "P")
            palette = np.stack([colors >> 16, colors >> 8 & 255,
                                colors & 255], 1).astype(np.uint8)
            frame.putpalette(palette.ravel().tolist())
            yield frame


class _RGBFrames:
    """The kept frames of an open gif, composited to RGB one at a time
    as Pillow's writer asks for them. Iterable more than once."""

    def __init__(self, im, keep):
        self.im, self.keep = im, keep

    def __iter__(self):
        for i in self.keep:
            self.im.seek(i)
            yield self.im.convert("RGB")


def write_kept_frames(src: Path, dst: str, delays, keep) -> bool:
    """The frames of the gif src that keep lists, written to dst as a
    gif of whole frames, each shown from its own start until the next
    kept frame's. False, with nothing left at dst, when a kept frame
    has more than 256 colors and so cannot be written exactly."""
    import itertools

    from PIL import Image

    starts = [0, *itertools.accumulate(delays)]
    ends = keep[1:] + [len(delays)]
    durations = [starts[b] - starts[a] for a, b in zip(keep, ends)]
    with Image.open(src) as im:
        try:
            first = next(iter(_KeptFrames(im, keep[:1])))
            first.save(dst, save_all=True,
                       append_images=_KeptFrames(im, keep[1:]),
                       duration=durations, loop=0, optimize=False)
        except _TooManyColors:
            Path(dst).unlink(missing_ok=True)
            return False
    return True


def transparent_first_frame(im) -> bool:
    """Whether a gif is really see-through where a page shows it. Gif
    uses its transparent index to mean "unchanged since the previous
    frame" as well, so `"transparency" in im.info` says almost nothing
    about an animation -- 141 of the reference archive's 228 gifs
    declare one and not one of them has a transparent pixel in its
    composited first frame. The question a video copy turns on is alpha
    on that composited frame, which is what this asks."""
    im.seek(0)
    return im.convert("RGBA").getchannel("A").getextrema()[0] < 255


def flat_fraction(im) -> float:
    """The share of horizontally adjacent pixel pairs that are identical
    -- near 1 for flat-colored art, near 0 under a photograph's sensor
    noise."""
    from PIL import ImageChops
    w, h = im.size
    if w < 2:
        return 1.0
    grey = im.convert("L")
    diff = ImageChops.difference(grey.crop((1, 0, w, h)),
                                 grey.crop((0, 0, w - 1, h)))
    return diff.histogram()[0] / ((w - 1) * h)


def is_line_art(im) -> bool:
    """True for a chart, screenshot or diagram -- art whose meaning is in
    thin strokes and small text -- and False for a photograph. See
    LINE_ART_MAX_COLORS."""
    rgb = im.convert("RGB")
    if rgb.getcolors(maxcolors=LINE_ART_MAX_COLORS) is None:
        return False               # more colors than flat-colored art has
    return flat_fraction(rgb) >= LINE_ART_MIN_FLAT


def has_alpha(im) -> bool:
    return im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info


class AnimationError(RuntimeError):
    """An animated gif that was to be placed as a clip and could not be
    (see ImagePlacer): the build stops rather than ship the animation
    as a gif a reader cannot pause. Carries every such gif of a run,
    one per line."""


class ImagePlacer:
    """Place a post's images into a site: hard-link each unchanged when
    nothing is to be gained (or nothing available can process it), else
    place a display copy. Line-art PNGs become full-resolution lossless
    webp, photographs are capped and encoded lossily (JPEG, or webp when
    they carry alpha), animated gifs become h264 clips with a poster
    beside them (see ANIMATED_FORMAT) or, where that is turned off or
    cannot be done, go through gifsicle. Copies are built once into the
    project's .image-cache/<scheme-caps>/ and hard-linked into every
    site that wants them, so the three exporters (and re-runs) share the
    work. Stills need Pillow, clips need Pillow and ffmpeg (with
    libwebp, for the poster), and resized gifs need gifsicle. A still
    whose tool is missing is placed by whatever is left, with a note in
    the summary; an animation that cannot become a clip -- a tool
    missing, real transparency, a gif that will not read, ffmpeg
    failing -- raises AnimationError instead, unless the site asked for
    gifs (animated_format = "gif").

    place() returns the path it actually wrote, which carries a new
    extension when the copy changed format -- the exporters rewrite
    their pages' image references from it. A clip's poster is placed
    beside it under the name poster_path() gives, which is how the hugo
    and pelican themes find it.

    masters=True is for a site that encodes its served clips itself
    when it is built: an animation's .mp4 is then the AV1 4:4:4 master
    that site makes them from (see CLIP_MASTER), unless site.toml's
    clip_master turns that off. Its poster is the one the clip would
    have, at the size of the h264 served."""

    def __init__(self, cache: Path, config: dict, masters: bool = False):
        images = config.get("images", {})
        self.still_cap = images.get("still_max_edge", STILL_MAX_EDGE) or 0
        self.gif_cap = images.get("animated_max_edge", ANIMATED_MAX_EDGE) or 0
        self.animated_format = (images.get("animated_format")
                                or ANIMATED_FORMAT).lower()
        self.video_crf = images.get("video_crf", VIDEO_CRF)
        self.video_preset = images.get("video_preset", VIDEO_PRESET)
        self.master = (str(images.get("clip_master") or CLIP_MASTER).lower()
                       if masters else "none")
        if self.master not in CLIP_MASTERS:
            raise ValueError(f"site.toml [images] clip_master: "
                             f"{self.master!r} is not one of "
                             + ", ".join(map(repr, CLIP_MASTERS)))
        self.master_crf = images.get("master_crf", MASTER_CRF)
        self.master_max_share = images.get("master_max_share",
                                           MASTER_MAX_SHARE)
        # a master is cached beside the clips and the stills, which the
        # sites share, under a name carrying its own settings
        self.clip_suffix = (f".av1444-{self.master_crf}.mp4"
                            if self.master == "av1" else ".mp4")
        # the encode settings name the cache directory beside the caps:
        # they decide what a clip comes out as, the way a cap does
        video = (f"-mp4{self.video_crf}-{self.video_preset}"
                 if self.animated_format == "mp4" else "-gif")
        self.cache = (
            Path(cache)
            / f"{CACHE_SCHEME}-{self.still_cap}-{self.gif_cap}{video}")
        self.gifsicle = shutil.which("gifsicle")
        self.ffmpeg = (shutil.which("ffmpeg")
                       if self.animated_format == "mp4" else None)
        self.ffmpeg_encoders = None        # asked once, on the first clip
        try:
            from PIL import Image
            self.pillow = Image
        except ImportError:
            self.pillow = None
        self.resized = self.converted = self.unchanged = self.clips = 0
        self.bytes_in = self.bytes_out = 0
        self.notes = []

    def place(self, src: Path, dst: Path) -> Path:
        """Place src at dst -- or beside it under the display copy's own
        extension, when the copy changed format -- and return the path
        written."""
        copy = self._display_copy(src)
        if copy is None:
            self.unchanged += 1
            link_or_copy(src, dst)
            return dst
        if copy.suffix == src.suffix:
            self.resized += 1
        else:
            self.converted += 1
            dst = dst.with_suffix(copy.suffix)
        self.bytes_in += src.stat().st_size
        self.bytes_out += copy.stat().st_size
        link_or_copy(copy, dst)
        poster = poster_path(copy)
        if poster.exists():            # a clip travels with its still
            self.clips += 1
            self.bytes_out += poster.stat().st_size
            link_or_copy(poster, poster_path(dst))
        return dst

    def warm(self, archive: Path, manifest: dict):
        """Build the display copies for every post image up front, in
        parallel -- gifsicle runs and Pillow encodes hold no GIL, and
        the big animated gifs take tens of seconds each, so this is
        where a cold cache earns its build time back. place() then
        just hard-links the results."""
        from concurrent.futures import ThreadPoolExecutor
        paths = [img for p in manifest.values()
                 if (d := archive / p["dir"] / "images").is_dir()
                 for img in d.iterdir()]

        def build(path):
            try:
                self._display_copy(path)
            except AnimationError as e:
                # the path says which gif; drop the name the error repeats
                why = str(e).removeprefix(f"{path.name}: ")
                return f"{path.relative_to(archive)}: {why}"
        # every gif is tried before the build stops, so one run names
        # all that failed rather than the first
        with ThreadPoolExecutor(min(8, os.cpu_count() or 1)) as pool:
            failed = [f for f in pool.map(build, paths) if f]
        if failed:
            raise AnimationError(
                f"{len(failed)} animated gif(s) could not be placed as "
                "video:\n" + "\n".join(failed))

    def report(self):
        if self.resized or self.converted:
            mb = 1e6
            clips = f" ({self.clips} of them clips)" if self.clips else ""
            print(f"display-copy images: {self.converted} re-encoded"
                  f"{clips}, {self.resized} resized "
                  f"({self.bytes_in / mb:.0f} MB -> "
                  f"{self.bytes_out / mb:.0f} MB), "
                  f"{self.unchanged} placed as they are", file=sys.stderr)
        for note in self.notes:
            print(note, file=sys.stderr)

    def _note(self, text: str):
        if text not in self.notes:
            self.notes.append(text)

    def _display_copy(self, src: Path):
        """The cached display copy for src, built on first sight; None
        when src should be placed as it is. Cache entries are named by
        source content hash plus the extension the copy carries, so
        reuse survives regeneration and fresh checkouts (mtimes carry no
        meaning there) and identical images shared between posts are
        built once. A copy that came out no smaller than its source is
        cached as a link to the source, which reads back as that same
        None -- the verdict is remembered, not recomputed."""
        ext = src.suffix.lower()
        if (ext == ".gif" and self.master == "av1"
                and self.animated_format == "mp4"):
            copy = self._stored_animation(src)
            if copy is not NotImplemented:
                return copy
        if ext == ".gif":
            # the extensions a gif's copy can carry, newest scheme
            # first: a clip in .mp4, a still's webp or jpg, and a
            # resized gif -- or the verdict that none paid off -- under
            # .gif. Which is built is decided on a cache miss, inside
            # _place_animation: reading a gif's frames to find out
            # whether it can become a clip costs more than the lookup
            cap = self.gif_cap
            candidates = (self.clip_suffix, ".webp", ".jpg", ext)
            if self.animated_format == "mp4":
                build = self._place_animation
            elif self._resizes_gif(src, cap):
                build, candidates = self._resize_gif, (ext,)
            else:
                return None
        elif ext in STILL_EXTS:
            # the extensions a still's copy can carry, newest scheme
            # first: line art lands in webp, a photograph in jpg, and a
            # copy that did not pay off under the source's own
            cap, candidates = self.still_cap, (".webp", ".jpg", ext)
            if not cap:
                return None
            if not self.pillow:
                self._note("pillow not installed: still images keep "
                           "their full size")
                return None
            if ext == ".png":
                build = self._copy_png       # picks format and size itself
            else:
                build = self._resize_still   # kept as it is, only smaller
                size = self._probe(src)
                if size is None or max(size) <= cap:
                    return None
        else:
            return None                    # svg and friends pass through
        digest = hashlib.sha256(src.read_bytes()).hexdigest()[:16]
        for suffix in candidates:
            cached = self.cache / f"{digest}{suffix}"
            if cached.exists():
                return cached if self._worth_placing(cached, src) else None
        self.cache.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.cache, suffix=ext)
        os.close(fd)
        try:
            built = build(src, tmp, cap)
        except AnimationError:
            discard_copy(tmp)
            raise
        if not built:
            discard_copy(tmp)
            return None
        if (built != self.clip_suffix
                and os.path.getsize(tmp) >= src.stat().st_size):
            discard_copy(tmp)              # the copy did not pay off
            built = ext                    # cache the verdict all the same
            cached = self.cache / f"{digest}{built}"
            link_or_copy(src, cached)
        else:
            cached = self.cache / f"{digest}{built}"
            # the poster first: a cached clip is only found by the name
            # the clip lands under, so the still has to be there by the
            # time another thread (or run) sees it
            poster = poster_path(Path(tmp))
            if poster.exists():
                os.replace(poster, poster_path(cached))
            os.replace(tmp, cached)
        return cached if self._worth_placing(cached, src) else None

    def _worth_placing(self, copy: Path, src: Path) -> bool:
        """Whether a display copy is placed at all: it has to undercut
        what it replaces -- except a clip, which is placed for the pause
        control it gives a reader whatever it weighs (see
        ANIMATED_FORMAT), and a capped gif stored with its poster for a
        site to make its clip from (see MASTER_MAX_SHARE)."""
        return (copy.suffix == ".mp4" or poster_path(copy).exists()
                or copy.stat().st_size < src.stat().st_size)

    def _stored_animation(self, src: Path):
        """For a site that stores masters: the cached AV1 master of an
        animated gif, or the smaller of its capped gif and capped
        lossless WebP where the master does not undercut that by enough
        (see MASTER_MAX_SHARE) -- each with the clip's poster beside it.
        All three are built on first sight and cached, and the choice is
        made on every lookup. NotImplemented for a still under a .gif
        name, which takes the usual path."""
        digest = hashlib.sha256(src.read_bytes()).hexdigest()[:16]
        master = self.cache / f"{digest}{self.clip_suffix}"
        delays = None
        if not master.exists():
            delays = self._clip_delays(src)
            if not delays:
                return NotImplemented
            self._cache_build(master, lambda tmp: self._encode_video(
                src, tmp, self.gif_cap, delays))
        lossless = []
        for suffix, write in ((CAPPED_GIF_SUFFIX, self._capped_gif),
                              (CAPPED_WEBP_SUFFIX, self._capped_webp)):
            copy = self.cache / f"{digest}{suffix}"
            if (write == self._capped_webp and not copy.exists()
                    and self._webp_cannot_win(master, lossless)):
                continue
            if not copy.exists():
                delays = delays or self._clip_delays(src)

                def build(tmp, write=write):
                    write(src, tmp, delays)
                    link_or_copy(poster_path(master), poster_path(Path(tmp)))
                self._cache_build(copy, build)
            if copy.stat().st_size:     # 0: no exact copy of this kind
                lossless.append(copy)
        best = min(lossless, key=lambda p: p.stat().st_size, default=None)
        if best and (master.stat().st_size
                     > self.master_max_share * best.stat().st_size):
            return best
        return master

    def _webp_cannot_win(self, master: Path, lossless) -> bool:
        """Whether the AV1 master is already small enough that no WebP
        could be stored instead: a WebP is at least WEBP_MIN_SHARE of
        the capped gif, so where the master is at most master_max_share
        of that, the master is stored whatever the WebP weighs. The
        WebP is the slow candidate by far, and this skips most of them.
        Nothing is cached for a skipped WebP, so a larger
        master_max_share builds it on the next lookup."""
        gif = next((p for p in lossless
                    if p.name.endswith(CAPPED_GIF_SUFFIX)), None)
        return gif is not None and (
            master.stat().st_size
            <= self.master_max_share * WEBP_MIN_SHARE * gif.stat().st_size)

    def _cache_build(self, cached: Path, build):
        """Run build(tmp) into a temporary file in the cache and move
        what it wrote -- and the poster beside it, first, since the
        cached file is found by its own name -- under cached's name."""
        self.cache.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.cache, suffix=".gif")
        os.close(fd)
        try:
            build(tmp)
        except BaseException:
            discard_copy(tmp)
            raise
        poster = poster_path(Path(tmp))
        if poster.exists():
            os.replace(poster, poster_path(cached))
        os.replace(tmp, cached)

    def _capped_webp(self, src: Path, tmp: str, delays):
        """The frames kept_frames keeps as lossless WebP, each composited
        and held from its own start until the next kept frame's. An
        empty file -- the verdict that none exists -- where a kept frame
        would last 10 ms or less, which ffmpeg's WebP decoder and
        browsers play as 100 ms, or where animated_max_edge shrinks the
        gif (a resampled frame gains the colors lossless pays for)."""
        import itertools

        keep = kept_frames(delays)
        starts = [0, *itertools.accumulate(delays)]
        ends = keep[1:] + [len(delays)]
        durations = [starts[b] - starts[a] for a, b in zip(keep, ends)]
        size = self._probe(src)
        if (min(durations) <= 10 or size is None
                or video_size(size, self.gif_cap) != tuple(size)):
            Path(tmp).write_bytes(b"")
            return
        with self.pillow.open(src) as im:
            im.seek(keep[0])
            first = im.convert("RGB")
            first.save(tmp, format="WEBP", save_all=True,
                       append_images=_RGBFrames(im, keep[1:]),
                       duration=durations, loop=0, **WEBP_LOSSLESS)

    def _capped_gif(self, src: Path, tmp: str, delays):
        """The gif with the frames kept_frames keeps, each at its own
        start and holding over the frames dropped after it, re-optimized
        losslessly by gifsicle -O3 and never larger than the gif itself
        when no frame is dropped. Dropping a frame breaks the partial
        frames after it, and gifsicle cannot unoptimize a gif with
        per-frame color tables, so there each kept frame is composited
        by Pillow and written whole with a palette of exactly its own
        colors. That is exact only when no frame has more than 256
        colors; a gif that has one gets an empty file, the verdict that
        no capped gif exists. (docs/gif-intermediates/fpscap.py --gif,
        which this follows, was checked exact on the archive.)"""
        keep = kept_frames(delays)
        source, written = src, None
        if len(keep) < len(delays):
            written = tmp + ".frames.gif"
            try:
                if not write_kept_frames(src, written, delays, keep):
                    Path(tmp).write_bytes(b"")
                    return
            except ImportError as e:
                raise AnimationError(
                    f"{src.name}: numpy is not installed, and capping a "
                    "gif's frame rate needs it (pip install numpy)") from e
            source = Path(written)
        try:
            run = None
            if self.gifsicle:
                resize = (["--resize-fit", f"{self.gif_cap}x{self.gif_cap}"]
                          if self.gif_cap else [])
                run = subprocess.run(
                    [self.gifsicle, "--no-conserve-memory", "-O3", *resize,
                     str(source), "-o", tmp], capture_output=True, text=True)
            if run is None or run.returncode or not os.path.getsize(tmp):
                shutil.copyfile(source, tmp)
            if source == src and os.path.getsize(tmp) > src.stat().st_size:
                shutil.copyfile(src, tmp)
        finally:
            if written:
                Path(written).unlink(missing_ok=True)

    def _probe(self, src: Path):
        """(width, height), by header sniff or Pillow, else None."""
        try:
            size = image_size(src)
        except OSError:
            return None
        if size is None and self.pillow:
            try:
                with self.pillow.open(src) as im:
                    size = im.size
            except Exception:
                return None
        return size

    def _place_animation(self, src: Path, tmp: str, cap: int):
        """A gif's display copy where this archive places animations as
        video: the clip, or for a still under a .gif name what a PNG
        becomes -- lossless webp for line art, the photo path for a
        photograph (see _copy_png), with the still cap -- or nothing.
        An animation that cannot become a clip raises AnimationError."""
        delays = self._clip_delays(src)
        if delays:
            return self._encode_video(src, tmp, cap, delays)
        if self.still_cap:
            return self._copy_png(src, tmp, self.still_cap)
        return (self._resize_gif(src, tmp, cap)
                if self._resizes_gif(src, cap) else None)

    def _clip_delays(self, src: Path):
        """Each frame's delay in ms, for an animation to be placed as a
        clip; None for a still under a .gif name. Raises AnimationError
        for an animation that cannot become a clip: one that will not
        read, one that is see-through where it is shown (video carries
        no alpha), or a missing tool."""
        if not self.pillow:
            raise AnimationError(
                f"{src.name}: Pillow is not installed, and placing "
                "animated gifs as video needs it (pip install "
                "'medium-archive[covers]'), or set [images] "
                'animated_format = "gif" in site.toml to keep gifs')
        try:
            with self.pillow.open(src) as im:
                if getattr(im, "n_frames", 1) < 2:
                    return None            # a still under a .gif name
                if transparent_first_frame(im):
                    raise AnimationError(
                        f"{src.name} is transparent where it is shown, "
                        "which video cannot carry")
                delays = []
                for i in range(im.n_frames):
                    im.seek(i)
                    delays.append(im.info.get("duration", 0))
        except AnimationError:
            raise
        except Exception as e:
            raise AnimationError(f"unreadable gif {src.name} ({e})") from e
        if not self.ffmpeg:
            raise AnimationError(
                f"{src.name}: ffmpeg is not installed, and placing "
                "animated gifs as video needs it, or set [images] "
                'animated_format = "gif" in site.toml to keep gifs')
        if not self._writes_webp():
            raise AnimationError(
                f"{src.name}: {self.ffmpeg} was built without libwebp, "
                "which a clip's poster frame needs. Install an ffmpeg "
                "built with libwebp (the ffmpeg package on Debian, "
                "Ubuntu and Homebrew is), or set [images] "
                'animated_format = "gif" in site.toml to keep gifs')
        if self.master == "av1" and not self._has_encoder("libaom-av1"):
            raise AnimationError(
                f"{src.name}: {self.ffmpeg} was built without libaom, "
                "which the AV1 master this site stores needs. Install "
                "an ffmpeg built with libaom (pixi.toml's is), or set "
                '[images] clip_master = "none" in site.toml to store '
                "h264 clips")
        return delays

    def _writes_webp(self) -> bool:
        """Whether this ffmpeg can write a clip's poster. Most builds
        carry libwebp; one that does not fails with "Encoder not found",
        which is worth saying plainly rather than as an ffmpeg error."""
        return self._has_encoder("libwebp")

    def _has_encoder(self, name: str) -> bool:
        """Whether this ffmpeg lists the encoder `name`, asked once."""
        if self.ffmpeg_encoders is None:
            run = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel",
                                  "error", "-encoders"],
                                 capture_output=True, text=True)
            self.ffmpeg_encoders = run.stdout or ""
        return f" {name} " in self.ffmpeg_encoders

    def _encode_video(self, src: Path, tmp: str, cap: int, delays):
        """The gif's frames as h264 in mp4, and its first frame beside
        it as the poster, from one ffmpeg run: the gif is decoded once
        and feeds both outputs, so the still is free. The clip keeps the
        frames kept_frames picks, each at the gif's own timestamp, so a
        gif's per-frame delays survive as the clip's variable frame
        rate. They are kept in milliseconds, finer than a gif's
        hundredths: left to itself ffmpeg gives the encoder a time base
        from a guessed frame rate and rounds every delay to it. Both
        outputs are padded to the same even size; faststart puts the
        index first, so a clip starts playing before it has all
        arrived. One thread: x264 split across threads made one
        screencast's clip 39% larger, and warm() already encodes gifs
        in parallel. No B-frames: with frames reordered, x264 hands
        the muxer no durations, and the mp4 then ends at the last
        frame's decode time -- 27 of the archive's clips came out
        short, one by 1.55 s of the 2.64 s its gif holds near the end.

        Where the site stores a master (see CLIP_MASTER), the mp4 is
        AV1 4:4:4 from libaom instead, with the same frames and
        timestamps, and without the padding: 4:4:4 has no even-size
        rule, and the h264 the site makes from the master pads it
        then. The poster is padded all the same, to the size of that
        h264. Raises AnimationError when ffmpeg fails."""
        size = self._probe(src)
        if size is None:
            raise AnimationError(f"cannot read the size of {src.name}")
        width, height = video_size(size, cap)
        scale = ([] if (width, height) == tuple(size)
                 else [f"scale={width}:{height}:flags=lanczos"])
        shape = scale + [EVEN_PAD]
        keep = kept_frames(delays)
        select = [select_frames(keep)] if len(keep) < len(delays) else []
        poster = poster_path(Path(tmp))
        if self.master == "av1":
            frames = select + scale
            codec = ["-c:v", "libaom-av1", "-pix_fmt", "yuv444p",
                     "-crf", str(self.master_crf), "-cpu-used", "6",
                     "-g", "9999", "-row-mt", "0"]
        else:
            frames = select + shape
            codec = ["-c:v", "libx264", "-profile:v", "high",
                     "-pix_fmt", "yuv420p", "-crf", str(self.video_crf),
                     "-preset", str(self.video_preset), "-bf", "0"]
        run = subprocess.run(
            [self.ffmpeg, "-nostdin", "-loglevel", "error", "-y",
             "-i", str(src)]
            + (["-vf", ",".join(frames)] if frames else [])
            + codec +
            ["-threads", "1",
             "-fps_mode", "passthrough", "-enc_time_base", "1:1000",
             "-an", "-movflags", "+faststart", "-f", "mp4", tmp,
             "-map", "0:v", "-vf", ",".join(shape), "-frames:v", "1",
             "-c:v", "libwebp", "-q:v", str(POSTER_QUALITY),
             "-f", "webp", str(poster)],
            capture_output=True, text=True)
        if run.returncode or not os.path.getsize(tmp) or not poster.exists():
            detail = (run.stderr or "").strip().splitlines()
            raise AnimationError(f"ffmpeg failed on {src.name}"
                                 + (f": {detail[-1]}" if detail else ""))
        return self.clip_suffix

    def _resizes_gif(self, src: Path, cap: int) -> bool:
        """Whether gifsicle has anything to do for this gif."""
        if not cap:
            return False
        if not self.gifsicle:
            self._note("gifsicle not installed: animated gifs keep "
                       "their full size")
            return False
        size = self._probe(src)
        return size is not None and max(size) > cap

    def _resize_gif(self, src: Path, tmp: str, cap: int):
        # -O2 re-optimizes frames after the resize (2/3 the bytes of a
        # bare resize on the reference archive); --lossy measured slower
        # for no further gain, and --no-conserve-memory avoids a slow
        # low-memory mode that huge gifs otherwise trip.
        run = subprocess.run(
            [self.gifsicle, "--no-conserve-memory", "-O2",
             "--resize-fit", f"{cap}x{cap}", str(src), "-o", tmp],
            capture_output=True, text=True)
        if run.returncode or not os.path.getsize(tmp):
            detail = (run.stderr or "").strip().splitlines()
            self._note(f"gifsicle failed on {src.name}"
                       + (f": {detail[-1]}" if detail else "")
                       + "; placed at full size")
            return None
        return ".gif"

    def _copy_png(self, src: Path, tmp: str, cap: int):
        """A PNG is line art or a photograph in PNG clothing (see
        is_line_art): the first keeps every pixel, the second takes the
        photo path. Returns the extension written, or None to place the
        source as it is."""
        try:
            with self.pillow.open(src) as im:
                if getattr(im, "n_frames", 1) > 1:
                    return None        # animated: not ours to flatten
                im.load()
                if not is_line_art(im):
                    return self._save_photo(im, tmp, cap)
                return self._save_line_art(im, tmp)
        except Exception as e:
            self._note(f"re-encode failed on {src.name} ({e}); "
                       "placed at full size")
            return None

    def _save_line_art(self, im, tmp: str) -> str:
        """Lossless webp at the source's own resolution: every pixel of
        the text kept, for a fraction of the PNG's bytes."""
        icc = im.info.get("icc_profile")
        kwargs = {"icc_profile": icc} if icc else {}
        im = im.convert("RGBA" if has_alpha(im) else "RGB")
        im.save(tmp, "WEBP", lossless=True, quality=100, method=6, **kwargs)
        if os.path.getsize(tmp) <= LINE_ART_MAX_BYTES:
            return ".webp"
        # An intricate one -- a dense screenshot, a photographic inset --
        # costs more losslessly than a page should carry. Spend the
        # quality rather than the resolution: it is the resolution the
        # small text needs, and q90 leaves strokes crisp.
        im.save(tmp, "WEBP", quality=LINE_ART_QUALITY, method=4, **kwargs)
        if (os.path.getsize(tmp) > LINE_ART_MAX_BYTES
                and max(im.size) > LINE_ART_MAX_EDGE):
            im.thumbnail((LINE_ART_MAX_EDGE, LINE_ART_MAX_EDGE),
                         self.pillow.Resampling.LANCZOS)
            im.save(tmp, "WEBP", quality=LINE_ART_QUALITY, method=4, **kwargs)
        return ".webp"

    def _save_photo(self, im, tmp: str, cap: int) -> str:
        """Capped and lossily encoded: JPEG, or webp for the alpha JPEG
        cannot carry."""
        icc = im.info.get("icc_profile")
        kwargs = {"icc_profile": icc} if icc else {}
        alpha = has_alpha(im)
        im = im.convert("RGBA" if alpha else "RGB")
        if cap and max(im.size) > cap:
            im.thumbnail((cap, cap), self.pillow.Resampling.LANCZOS)
        if alpha:
            im.save(tmp, "WEBP", quality=PHOTO_QUALITY, method=4, **kwargs)
            return ".webp"
        im.save(tmp, "JPEG", quality=PHOTO_QUALITY, optimize=True,
                progressive=True, **kwargs)
        return ".jpg"

    def _resize_still(self, src: Path, tmp: str, cap: int):
        Image = self.pillow
        try:
            with Image.open(src) as im:
                if getattr(im, "n_frames", 1) > 1:
                    return None            # animated: not ours to flatten
                # Medium archives hold the odd mislabeled file (a PNG
                # under a .jpeg name); re-encode what the bytes are,
                # not what the name says -- the filename stays as the
                # pages reference it, and browsers sniff content anyway.
                fmt = im.format
                icc = im.info.get("icc_profile")
                if im.mode == "P":
                    im = im.convert(
                        "RGBA" if "transparency" in im.info else "RGB")
                im.thumbnail((cap, cap), Image.Resampling.LANCZOS)
                kwargs = {"icc_profile": icc} if icc else {}
                if fmt == "JPEG":
                    kwargs |= {"quality": PHOTO_QUALITY, "optimize": True,
                               "progressive": True}
                elif fmt == "WEBP":
                    kwargs |= {"quality": PHOTO_QUALITY, "method": 4}
                else:
                    kwargs |= {"optimize": True}
                im.save(tmp, format=fmt, **kwargs)
        except Exception as e:
            self._note(f"resize failed on {src.name} ({e}); "
                       "placed at full size")
            return None
        return src.suffix.lower()


def by_year(manifest: dict) -> list:
    """[(year, [(url, post), newest first]), newest year first]."""
    posts = sorted(manifest.items(),
                   key=lambda kv: (kv[1].get("date") or "", kv[0]),
                   reverse=True)
    years = {}
    for url, p in posts:
        years.setdefault((p.get("date") or "")[:4] or "undated",
                         []).append((url, p))
    return sorted(years.items(), reverse=True)


def old_paths(post: dict, url: str):
    """Every site-relative path an old inbound link to this post may
    carry, as (path, the URL it belonged to) pairs: the Medium slug+id
    path, Medium's /p/<id> short form, and the Ghost-era path when there
    is one."""
    pairs = [(post["original_path"], url)]
    if post.get("medium_id"):
        pairs.append((f"/p/{post['medium_id']}", url))
    if post.get("ghost_url"):
        ghost_path = urlsplit(post["ghost_url"]).path
        if ghost_path != post["original_path"]:
            pairs.append((ghost_path, post["ghost_url"]))
    return pairs


def redirect_rules(manifest: dict, stems: dict, new_path):
    """(old inbound path, new page URL, original URL) for every old
    path of every post, oldest post first; new_path(url) chooses the
    URL scheme. Keyed by the post's URL rather than its page name,
    since an address is built from more than the name -- the hugo and
    pelican sites file a page under its publish year, and two years may
    hold the same name."""
    for url, p in sorted(manifest.items(), key=lambda kv: kv[1].get("date") or ""):
        for old, original in old_paths(p, url):
            yield old, new_path(url), original


def write_redirects_csv(site: Path, manifest: dict, stems: dict, new_path):
    """old inbound path -> new page URL (new_path(url) chooses the URL
    scheme). The archive-root redirects.csv maps to posts/ directories;
    this one maps to the URLs the exported site actually serves."""
    def q(v):
        v = "" if v is None else str(v)
        return '"' + v.replace('"', '""') + '"' if any(c in v for c in ',"\n') else v
    rows = ["old_path,new_path,original_url"]
    for old, new, original in redirect_rules(manifest, stems, new_path):
        rows.append(",".join(q(x) for x in (old, new, original)))
    (site / "redirects.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")


def redirects_file(rules) -> str:
    """The same map as a `_redirects` file -- one `old new 301` rule per
    line, the format Netlify, Cloudflare Pages and their imitators read
    from the site root. Where a host honors it, an old link answers
    with a real HTTP 301, which search engines credit to the new page
    directly. Rules from redirect_rules() or the rows of
    redirects.csv."""
    return "".join(f"{old} {new} 301\n" for old, new, *_ in rules)


# What a site does about old inbound links, as site.toml's "redirects".
# The two mechanisms are alternatives, not layers: no host reads both,
# and on a host that reads one the other is inert weight.
#
#   stubs   a meta-refresh page at every old path. The only mechanism
#           GitHub Pages has -- it does not read `_redirects` at all --
#           and it works on any static host, at the cost of a file per
#           old path, most of them directories at the site root.
#   file    a `_redirects` file at the site root, and no stub pages:
#           one text file, and a real HTTP 301 rather than a
#           meta refresh. Netlify, Cloudflare Pages and their
#           imitators; nothing on GitHub Pages.
#   both    the default, for a host not yet chosen: whichever mechanism
#           the host reads answers. Netlify is the one host where the
#           combination is worse than either alone -- a static file
#           shadows an unforced rule, so the stub answers and the 301
#           never fires.
#   none    neither, for a site whose redirects are configured
#           elsewhere (a CDN rule set, an nginx map). redirects.csv is
#           still written, since that is the map such a rule set is
#           built from.
REDIRECT_MODES = ("both", "stubs", "file", "none")


def redirect_mode(config: dict) -> str:
    """site.toml's "redirects" (see REDIRECT_MODES), defaulting to
    "both"; an unknown value is reported and read as the default."""
    mode = config.get("redirects", "both")
    if mode not in REDIRECT_MODES:
        print(f'site.toml "redirects": {mode!r} is not one of '
              f'{", ".join(REDIRECT_MODES)}; using "both"', file=sys.stderr)
        return "both"
    return mode


def wants_redirect_stubs(mode: str) -> bool:
    return mode in ("both", "stubs")


def wants_redirects_file(mode: str) -> bool:
    return mode in ("both", "file")


def canonical_for(post: dict) -> str | None:
    """The address a post page's canonical link should name instead of
    the page itself, or None. The archive is the post's home -- the
    Medium copy is never its canonical -- except where the post itself
    declared one on another host (Medium's "originally published at"
    for a story imported from a gist, a Notion page, someone's own
    blog): that is carried through, as WordPress's SEO plugins carry a
    per-post canonical, since the page is a copy of that one and says
    so rather than competing with it. A canonical naming the
    publication's own host (a Ghost-era slug) is the same post and is
    ignored; the link map already resolves it."""
    declared = post.get("canonical_url")
    if declared and (urlsplit(declared).netloc.lower()
                     != urlsplit(post["original_url"]).netloc.lower()):
        return declared
    return None


# Latin letters that carry no combining form, so NFKD leaves them whole
# and an ASCII fold would drop them ("Michal" losing its l entirely).
_FOLD = str.maketrans({
    "\u0141": "L", "\u0142": "l", "\u00d8": "O", "\u00f8": "o",
    "\u00c6": "AE", "\u00e6": "ae", "\u0152": "OE", "\u0153": "oe",
    "\u00df": "ss", "\u0110": "D", "\u0111": "d", "\u0126": "H",
    "\u0127": "h", "\u0131": "i", "\u014a": "N", "\u014b": "n",
    "\u00de": "Th", "\u00fe": "th", "\u00d0": "D", "\u00f0": "d",
})


def ascii_slug(text: str) -> str:
    """Any term as the slug the sites key it by: pelican's own slugify,
    reproduced so that the URLs it builds do not move. Fold to ASCII,
    drop everything that is not a word character, space or hyphen, then
    collapse runs to single hyphens. Everything a URL is built from goes
    through this -- page names (page_name) and bylines (author_slug) --
    so that one thing is one address, whichever generator serves it."""
    text = unicodedata.normalize("NFKC", text).translate(_FOLD)
    text = "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"[-\s]+", "-", text.strip()).lower()


SLUG_MAX = 55


def truncate_slug(slug: str, limit: int = SLUG_MAX) -> str:
    """A slug cut to `limit` characters at a word boundary. Medium builds
    a slug from the whole of a title, so they run to 80 characters and
    the address bar shows a paragraph; the cut falls on the last hyphen
    at or before the limit, so a URL ends on a whole word rather than
    mid-syllable. A slug with no hyphen to cut at is cut hard, and a cut
    that lands on a hyphen loses it -- no URL ends in one.

    55 rather than a round 50 because 50 severs
    ...-accessibility-workshops-part-1 and -part-2 at exactly the part
    that tells them apart, and they are the archive's longest names that
    still earn their length."""
    if len(slug) <= limit:
        return slug
    cut = slug[:limit + 1]
    hyphen = cut.rfind("-")
    return (cut[:hyphen] if hyphen > 0 else slug[:limit]).rstrip("-")


def post_year(post: dict) -> str:
    """The year a post was published, as the sites file and address it
    by: the year of `date` (Medium's datePublished, or the exact
    first-publish timestamp an imported export carries), never of
    `updated`. A post whose date never resolved is filed under
    "undated" rather than dropped."""
    return (post.get("date") or "")[:4] or "undated"


def author_slug(name: str) -> str:
    """An author's name as the slug both sites key them by. Bylines are
    people's names, not slugs, so left as terms they would reach each
    generator raw: hugo keeps a name's accents and punctuation in the
    path it builds (`/authors/matt-mccormick-@thewtex@fosstodon.org/`),
    while pelican folds it to ASCII, so the two sites would serve one
    author under two addresses. Slugging here is what the tags do
    already, for the same reason -- the slug is the identity, and the
    name is what a page shows."""
    return ascii_slug(name)


def author_entries(manifest: dict) -> dict:
    """Author slug -> what a site shows for that byline: the name, and
    the profile address the archive knows for them (the Medium profile
    the bylines carry), which the Person structured data on posts and
    author pages names as their sameAs. The authors' counterpart of
    tag_names, with the profile in the same entry as the name because
    both are one byline's, and correcting one by hand means finding the
    other. The first spelling and the first address seen for a slug
    win; an author with no profile is an entry with a name alone."""
    entries = {}
    for p in manifest.values():
        for a in p.get("authors") or []:
            slug = author_slug(a["name"])
            if not slug:
                continue
            entry = entries.setdefault(slug, {"name": a["name"]})
            if a.get("url") and "url" not in entry:
                entry["url"] = a["url"]
    return dict(sorted(entries.items()))


DATA_HEADERS = {
    "tags.yaml": """\
# Tag slug -> the name shown for the tag. URLs (/tags/<slug>/) use the
# slug; a slug missing here is shown as the slug itself.
""",
    "authors.yaml": """\
# Author slug -> byline:
#
#   <slug>:
#     name: the name shown on bylines, the author page and its feed
#     url:  profile elsewhere (optional), the author's sameAs in the
#           structured data
#
# URLs (/authors/<slug>/) use the slug; a slug missing here is shown as
# the slug itself.
""",
}


def site_data(manifest: dict, archive: Path) -> dict:
    """File name -> the map it holds, for the data files both the hugo
    and the pelican site are built with. They hold what a site is
    rendered from that is neither the posts nor the hand-written
    site.toml: the names tags and authors are shown under, and the
    profile address of each byline. Derived from the archive here and
    written beside the site's config rather than baked into it, so a
    checked-in copy of a site can correct a name or add a profile by
    editing one small file -- and so a rename reaches every place the
    engine renders that term at once (cards, the term page and its
    title, the chip index, the per-term feed), which a directory per
    term could not.

    One file per taxonomy, not one per field: everything a site shows
    for a byline -- the name and the profile -- is that byline's one
    entry in authors.yaml, so a correction is made in one place rather
    than hunted across two files keyed differently. YAML, and each file
    under a header saying what its entries mean, because these are the
    files a checked-in site edits by hand; the archive's own inputs
    stay as they are.

    Tags and authors reach both engines as slugs, so a term and its
    /tags/<slug>/ or /authors/<slug>/ URL are exactly the archive's
    rather than whatever each engine's slugify would make of "C++" or
    of an accented byline (see tags.py and author_slug); these maps
    carry the spaces, capitals, accents and punctuation that only the
    rendered name needs. Both files are keyed by that slug, the term's
    identity in either engine."""
    return {"tags.yaml": tag_names(manifest, archive),
            "authors.yaml": author_entries(manifest)}


def write_data_files(site: Path, manifest: dict, archive: Path) -> dict:
    """Write site_data's maps as <site>/data/*.yaml, each under the
    header that says what its entries are, and sorted so a diff between
    two builds shows only what the archive changed."""
    data = site_data(manifest, archive)
    (site / "data").mkdir(parents=True, exist_ok=True)
    for name, mapping in data.items():
        (site / "data" / name).write_text(
            DATA_HEADERS[name] + yaml.safe_dump(
                dict(sorted(mapping.items())), allow_unicode=True,
                default_flow_style=False, sort_keys=True, width=10 ** 6),
            encoding="utf-8")
    return data


def write_site_config(site: Path, data: dict):
    """<site>/site.toml: this site's own data, in the shape its
    generated config reads. It is the archive's hand-written
    site/site.toml resolved for one built site -- the images as the
    copies that site carries, and what several keys together come to
    (the profile list, the masthead link's label) written out -- so
    that a checked-in copy of the site has one file to edit for
    everything the pages say about themselves, and none of it is buried
    in generated machinery. Every key is written under its own
    documentation, an unset one commented out beside an example (see
    siteconf), so the file is also the list of what there is to set.
    Hugo has its own place for the same data
    (config/_default/params.toml, which is that engine's file for it);
    this is the pelican site's."""
    (site / "site.toml").write_text(
        fill_template("pelican/site.toml.tmpl", site=documented_toml(data)),
        encoding="utf-8")


def site_profiles(config: dict) -> list:
    """The publication's addresses elsewhere, for the Organization's
    sameAs: site.toml "profiles" (a list of URLs) plus the X/Twitter
    profile its "twitter" handle names, deduplicated, in that order."""
    profiles = list(config.get("profiles") or [])
    handle = (config.get("twitter") or "").strip().lstrip("@")
    if handle:
        profiles.append(f"https://x.com/{handle}")
    return list(dict.fromkeys(profiles))


def masthead_link(config: dict):
    """site.toml's "logo_link" as the href and the accessible name of
    the masthead's link, or None when the masthead links to the site's
    own home, as it does by default. A logo is set as that link when it
    stands for something larger than the blog -- the Jupyter blog's
    masthead carries the Jupyter mark, which belongs to jupyter.org --
    and then the site's title is the wrong name for the link: it would
    promise a reader the blog and hand them somewhere else. So the link
    is named by where it goes, its host."""
    url = (config.get("logo_link") or "").strip()
    if not url:
        return None
    return {"url": url, "label": urlsplit(url).netloc or url}


# site.toml's "newsletter", the signup band both themes put at the foot
# of every page (jupyter.org's own, which is where the shape of it comes
# from). The form is a HubSpot embed, named the way HubSpot names its
# three parts, so the values are the ones already on hand for whoever
# owns the form; the region is optional, since HubSpot's own default is
# the one most portals are on.
NEWSLETTER_KEYS = ("heading", "hubspot_portal", "hubspot_form")


def newsletter_params(config: dict):
    """site.toml's "newsletter" as the params both themes render the
    signup band from, or None when there is none to render. A partial
    entry is a mistake worth hearing about rather than a band quietly
    missing from the built site, so what it lacks is named."""
    entry = config.get("newsletter") or {}
    if not entry:
        return None
    missing = [key for key in NEWSLETTER_KEYS if not entry.get(key)]
    if missing:
        print("newsletter band skipped: site.toml's \"newsletter\" is "
              "missing " + ", ".join(missing), file=sys.stderr)
        return None
    params = {key: str(entry[key]) for key in NEWSLETTER_KEYS}
    params["hubspot_region"] = str(entry.get("hubspot_region") or "na1")
    return params


def caption_text(caption: str) -> str:
    """A caption's plain text, for an image whose alt is empty (see
    pages.markdown_text). Most Medium images carry no alt while their
    captions describe them exactly; WordPress's SEO plugins fill an
    empty alt the same way (from the caption, else the title), and it
    is what a screen reader, and image search, would otherwise miss."""
    return markdown_text(caption)


def quote_arg(value: str) -> str:
    """A value for a double-quoted argument on an exporter's own
    directive or shortcode line (hugo's figure shortcode, the pelican
    figure directive): inner quotes escaped so they do not end the
    argument, and backslashes dropped.

    Dropping rather than escaping them is hugo's doing. Its shortcode
    lexer gives a backslash meaning only before a quote; but a value
    that carries one escaped quote then goes through
    ignoreEscapesAndEmit, which strips every backslash in it ("We don't
    send the backslash back to the client"). So `\\\\` would reach hugo as
    two literal backslashes in one value and as none in another, while
    shlex.split gives the pelican reader one either way -- and the two
    sites are meant to carry the same alt text, which a test holds them
    to. Dropping is lossy and identical; no alt or link in a real
    archive has held a backslash yet."""
    return value.replace("\\", "").replace('"', '\\"')


def first_image(markdown: str) -> str | None:
    """The images/<name> reference of the first body image, or None. The
    first image of a post is usually in the first screen, so the site
    themes load it eagerly and at high priority where every later one
    is lazy -- the treatment WordPress gives the first content image,
    since lazy-loading the largest visible image delays the page's
    largest contentful paint. Fenced code is skipped: an image
    reference there is content, not an image."""
    fence = False
    for line in markdown.split("\n"):
        if re.match(r"^`{3,}", line):
            fence = not fence
        elif not fence:
            m = re.search(r"!\[[^\]\n]*\]\((images/[^)\s]+)\)", line)
            if m:
                return m.group(1)
    return None


def git_ignored(site: Path, names) -> set:
    """Which of `names` git ignores inside site, as a set -- empty when
    the rules there say nothing about the site's contents, which is the
    caller's signal to keep its own list instead.

    They say nothing in two cases: site is not in a git working tree
    (or git is not installed), and site is a directory the enclosing
    repository ignores whole -- site-pelican/ in this project's own
    .gitignore, say, which makes git call every file under it ignored
    and would otherwise read as "keep all of it"."""
    def git(*args, **kw):
        try:
            return subprocess.run(["git", "-C", str(site), *args],
                                  capture_output=True, text=True, **kw)
        except OSError:                    # no git on this machine
            return None
    itself = git("check-ignore", "-q", ".")
    # 0: this very directory is ignored, 1: it is not, 128: not a
    # working tree
    if itself is None or itself.returncode != 1:
        return set()
    run = git("check-ignore", "--stdin", "-z", input="\0".join(names))
    if run is None or run.returncode not in (0, 1):
        return set()
    return {name for name in run.stdout.split("\0") if name}


def clean_out(site: Path, build_dirs=()):
    """Empty a site directory before it is rebuilt, keeping what is not
    the exporter's to delete: `.git/`, and every entry git ignores
    there. What is left is what a previous run wrote and what a person
    has added, so a page whose post has left the archive goes with it
    -- which is the point, and what a rebuild alone cannot do (it
    overwrites what it writes and knows nothing of the rest).

    The ignore rules do the keeping because the site carries its own:
    the .gitignore each exporter writes lists that generator's build
    output and caches (`output/`, `public/`, `resources/`, `_build/`),
    which are expensive or network-bound to rebuild. Outside a git
    working tree there are no rules to read, so `build_dirs` is kept
    instead and everything else goes."""
    if not site.exists():
        return
    children = [c for c in site.iterdir() if c.name != ".git"]
    ignored = git_ignored(site, [c.name for c in children])
    keep = ignored or set(build_dirs)
    for child in children:
        if child.name in keep:
            continue
        shutil.rmtree(child) if child.is_dir() else child.unlink()


def report_stale_pages(pages_dir: Path, stems, nested: bool = False) -> int:
    """Page directories under pages_dir that this run did not write --
    a post deleted from the archive, or one whose slug or date changed
    -- named on stderr with what removes them. A rebuild writes over
    the pages it makes and leaves everything else alone, so without
    this a site keeps serving a page the archive no longer has.

    `nested` for the hugo and pelican trees, where a page sits a year
    deep (content/posts/<year>/<stem>/) and `stems` holds
    "<year>/<stem>" names; a year directory the archive has emptied is
    itself reported, since an empty one still publishes a year page."""
    if not pages_dir.is_dir():
        return 0
    if nested:
        found = {f"{y.name}/{d.name}" for y in pages_dir.iterdir() if y.is_dir()
                 for d in y.iterdir() if d.is_dir()}
        empty = {y.name for y in pages_dir.iterdir()
                 if y.is_dir() and not any(d.is_dir() for d in y.iterdir())}
        stale = sorted((found - set(stems)) | empty)
    else:
        stale = sorted(d.name for d in pages_dir.iterdir()
                       if d.is_dir() and d.name not in stems)
    if stale:
        shown = ", ".join(stale[:5]) + (", ..." if len(stale) > 5 else "")
        print(f"{len(stale)} page(s) in {pages_dir} are not in the archive "
              f"({shown}); rebuild with --clean to remove them",
              file=sys.stderr)
    return len(stale)


def front_matter_yaml(fields: dict) -> str:
    """A page's front matter: YAML between `---` fences, what both
    site generators read natively and what nearly every other one
    reads too -- the form a hand-edited checked-in site expects, and
    the form the two exporters share so a field means the same thing
    in either site.

    Written by the yaml library rather than by hand: a title holding a
    colon, a quote, a leading `-` or something that reads as a number
    or a date is quoted the way the specification requires, which is
    exactly what hand-written front matter gets wrong. Keys keep the
    order they were built in (sort_keys off), so a page's front matter
    reads title-first rather than alphabetically."""
    body = yaml.safe_dump(fields, sort_keys=False, allow_unicode=True,
                          default_flow_style=False, width=10 ** 6)
    return f"---\n{body}---\n\n"


def read_post_body(src: Path):
    """The converted body of posts/<dir>/, without its front matter, or
    None (noted) when index.md is missing -- re-run convert."""
    if not (src / "index.md").exists():
        print(f"skipping (no index.md; re-run convert): {src}",
              file=sys.stderr)
        return None
    _, body = split_post((src / "index.md").read_text(encoding="utf-8"))
    return body


def place_images(archive: Path, post: dict, page_dir: Path, placer=None) -> dict:
    """The post's images beside its page, under page_dir/images/ --
    through placer when given (see ImagePlacer), else as they are.
    Returns the names that changed on the way (a display copy in a new
    format), for retarget_images to point the page at."""
    renames = {}
    images = archive / post["dir"] / "images"
    if not images.is_dir():
        return renames
    (page_dir / "images").mkdir(exist_ok=True)
    for img in sorted(images.iterdir()):
        dst = page_dir / "images" / img.name
        if placer:
            dst = placer.place(img, dst)
        else:
            link_or_copy(img, dst)
        if dst.name != img.name:
            renames[img.name] = dst.name
    return renames


def export_content(archive: Path, site: Path, manifest: dict, stems: dict,
                   front_matter, escape=None, placer=None,
                   transform=None, covers=None) -> int:
    """The shared page loop for the /posts/<year>/<stem>/ exporters
    (hugo, pelican): one content/posts/<year>/<stem>/index.md per post --
    front matter from front_matter(url, post), body with in-publication
    links rewritten to /posts/<year>/<stem>/ and then through
    transform() when given (a generator-specific whole-body rewrite,
    like each exporter's own figure form) -- plus the post's images
    beside it (through placer, when given -- see ImagePlacer) and its
    baked card cover (covers, when given -- see Covers). Returns the
    page count."""
    links = LinkMap(manifest, stems)
    if placer:
        placer.warm(archive, manifest)

    def target_for(url):
        hit = links.page_for(url)
        if hit is None:
            return None
        _, stem, year, frag = hit
        return f"/posts/{year}/{stem}/" + (f"#{frag}" if frag else "")

    pages = 0
    for url, p in manifest.items():
        body = read_post_body(archive / p["dir"])
        if body is None:
            continue
        body = rewrite_body(body, target_for, escape)
        # the page's first body image, for front_matter() to name when
        # its generator wants it (see first_image); found before the
        # transform, which may rewrite image references into a
        # generator's own syntax
        p = dict(p, first_image=first_image(body),
                 # the subtitle is rendered by the post template, from
                 # front matter, so its in-publication links are
                 # rewritten here rather than by the body pass above
                 subtitle=rewrite_body(p.get("subtitle") or "", target_for))
        if transform is not None:
            body = transform(body)
        page_dir = site / "content" / "posts" / post_year(p) / stems[url]
        page_dir.mkdir(parents=True, exist_ok=True)
        # images first: a display copy can change format, and the page
        # has to reference the name that was actually placed
        renames = place_images(archive, p, page_dir, placer)
        (page_dir / "index.md").write_text(
            retarget_images(front_matter(url, p) + body, renames),
            encoding="utf-8")
        if covers:
            covers.bake(url, page_dir)
        pages += 1
    if placer:
        placer.report()
    return pages
