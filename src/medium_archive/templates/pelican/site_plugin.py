# Passed to STUB.format() as a value, never used as a format string:
# the included CSS/JS is full of braces.
THEME_HEAD = """\
<!-- @include shared/redirect-head.html -->
"""

STUB = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{target}</title>
<link rel="canonical" href="{target}">
<meta name="robots" content="noindex">
<meta http-equiv="refresh" content="0; url={target}">
{theme_head}</head><body><a href="{target}">{target}</a></body></html>
"""


def _write_redirects(pelican_obj):
    # redirects.csv (old path -> new path) as a Netlify-style _redirects
    # file and/or meta-refresh stub pages, per REDIRECT_FILE and
    # REDIRECT_STUBS.
    import csv
    import os
    if not (REDIRECT_STUBS or REDIRECT_FILE):
        return
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "redirects.csv"), newline="",
              encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if REDIRECT_FILE:
        with open(os.path.join(pelican_obj.output_path, "_redirects"), "w",
                  encoding="utf-8") as fh:
            fh.writelines(f"{row['old_path']} {row['new_path']} 301\n"
                          for row in rows)
        print(f"_redirects: {len(rows)} rules written from redirects.csv")
    if not REDIRECT_STUBS:
        return
    written = 0
    for row in rows:
        parts = [p for p in row["old_path"].split("/") if p]
        stub = os.path.join(pelican_obj.output_path, *parts, "index.html")
        if os.path.exists(stub):        # never clobber a real page
            continue
        os.makedirs(os.path.dirname(stub), exist_ok=True)
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write(STUB.format(target=SITEURL + row["new_path"],
                                 theme_head=THEME_HEAD))
        written += 1
    print(f"redirect stubs: {written} written from redirects.csv")


# Sitemap entries as (url, last-modified or None), collected once the
# articles are read and written after the build with robots.txt. Search,
# paginated listings and redirect stubs are left out, as in the hugo
# site's sitemap.
_SITEMAP = []


def _collect_sitemap(article_generator):
    g = article_generator
    _SITEMAP.clear()
    _SITEMAP.append(("", None))
    for article in g.articles:
        modified = getattr(article, "modified", None) or article.date
        _SITEMAP.append((article.url, modified))
    for tag in g.tags:
        _SITEMAP.append((tag.url, None))
    for author, _ in g.authors:
        _SITEMAP.append((author.url, None))
    # the year pages, as the hugo site's sitemap lists its year sections
    first = {}
    for article in g.articles:
        first.setdefault(article.date.strftime("%Y"), article.date)
    for year in sorted(first, reverse=True):
        _SITEMAP.append((YEAR_ARCHIVE_URL.format(date=first[year]), None))
    for url in (TAGS_URL, AUTHORS_URL, ARCHIVES_URL):
        _SITEMAP.append((url, None))


def _write_crawl_files(pelican_obj):
    import os
    from xml.sax.saxutils import escape
    out = pelican_obj.output_path
    with open(os.path.join(out, "robots.txt"), "w", encoding="utf-8") as fh:
        fh.write("User-agent: *\n")
        if NOINDEX:
            fh.write("Disallow: /\n")
        else:
            fh.write(f"Allow: /\n\nSitemap: {SITEURL}/sitemap.xml\n")
    lines = ['<?xml version="1.0" encoding="utf-8" standalone="yes"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for url, modified in _SITEMAP:
        lines.append(f"  <url><loc>{escape(SITEURL + '/' + url)}</loc>"
                     + (f"<lastmod>{modified.isoformat()}</lastmod>"
                        if modified else "") + "</url>")
    lines.append("</urlset>")
    with open(os.path.join(out, "sitemap.xml"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"sitemap: {len(_SITEMAP)} urls")


# Responsive body images: webp variants at these widths (never
# upscaled) in srcset with this sizes hint. Keep in sync with the hugo
# theme's _partials/post-image.html.
VARIANT_WIDTHS = (480, 736, 1104)
SIZES_ATTR = "(max-width: 800px) 100vw, 736px"
# An <img> tag, skipping over quoted attribute values: an alt text may
# contain ">".
IMG_TAG = r"""<img\b(?:[^>"']|"[^"]*"|'[^']*')*>"""
IMG_TAG_RE = None
# Set by the reader on body images, so the post-build passes can tell
# them from theme images (a related-post card's cover points into the
# other post's images/ directory, so a path rule cannot). Removed from
# the output by _optimize_article_images.
BODY_IMAGE_ATTR = "data-body-image"
# Only photographs get variants: png and webp images are line art whose
# small text a downscale would blur, and a gif may be animated.
VARIANT_SUFFIXES = (".jpg", ".jpeg")
# Animations are exported as .mp4 clips with a "-poster.webp" still
# beside them; a body image with this suffix is rewritten as a <video>.
VIDEO_SUFFIXES = (".mp4",)
POSTER_SUFFIX = "-poster.webp"


def _prioritize_first_images(pelican_obj):
    # The first body image on each post page loads eagerly with
    # fetchpriority="high" instead of lazily, since it is likely above
    # the fold and lazy-loading it delays the largest contentful paint.
    # Runs before _optimize_article_images, which strips the marker.
    import glob
    import os
    import re
    img_re = re.compile(IMG_TAG, re.I)
    pages = 0
    for page in glob.glob(os.path.join(pelican_obj.output_path,
                                       "posts", "*", "*", "index.html")):
        with open(page, encoding="utf-8") as fh:
            html = fh.read()
        m = next((m for m in img_re.finditer(html)
                  if BODY_IMAGE_ATTR in m.group(0)), None)
        if not m or ' loading="lazy"' not in m.group(0):
            continue
        first = m.group(0).replace(' loading="lazy"', ' fetchpriority="high"', 1)
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(html[:m.start()] + first + html[m.end():])
        pages += 1
    print(f"first images: {pages} pages load theirs eagerly")


def _clip(src, path, attrs, output_path, here):
    """The <video> for a body .mp4, with its poster, the poster's size
    and the alt text as aria-label. Attribute values come from the
    rendered tag, so they are already escaped."""
    import os

    from PIL import Image

    poster = os.path.splitext(path)[0] + POSTER_SUFFIX
    parts = poster.lstrip("/").split("/")
    still = os.path.join(here, PATH, *parts)
    if not os.path.exists(still):
        still = os.path.join(output_path, *parts)
    extra = ""
    if os.path.exists(still):
        extra = ' poster="%s"' % (os.path.splitext(src)[0] + POSTER_SUFFIX)
        try:
            with Image.open(still) as im:
                extra += ' width="%d" height="%d"' % im.size
        except OSError:
            pass
    label = attrs.get("alt", "")
    return ('<video src="%s"%s preload="none" loop muted playsinline'
            ' controls%s></video>'
            % (src, extra, ' aria-label="%s"' % label if label else ""))


# An animation is stored under content/ as one of two things, and what
# a browser is served is h264 made from it here, on every build: the
# AV1 4:4:4 master of its clip (<name>.mp4), or -- where no master
# undercut it by enough -- a lossless copy, frame-rate capped, as gif
# or as animated WebP, which the clip's poster beside it
# (<name>-poster.webp) marks as an animation to serve this way; its
# page then shows <name>.mp4 as a <video>, and the gif or WebP stays
# for the feeds, which carry the page as written. 4:2:0 h264 plays everywhere, on a hardware decoder, where
# AV1 4:4:4 plays in some browsers and in software. These are the
# settings the archive's exporter encodes the h264 clips of its other
# sites with (medium_archive's sites.ImagePlacer), less the frame
# selection, which what is stored already carries: each frame keeps
# its timestamp on a millisecond time base, an odd size is padded by a
# pixel, one thread per encode, and no B-frames, without which the mp4
# runs as long as the gif. An .mp4 that is not AV1 -- a site exported
# with clip_master = "none" -- is served as it is.
CLIP_H264 = ["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
             "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
             "-crf", "24", "-preset", "slower", "-threads", "1", "-bf", "0",
             "-fps_mode", "passthrough", "-enc_time_base", "1:1000",
             "-an", "-movflags", "+faststart", "-f", "mp4"]
# The h264 made from each stored animation is kept by that file's
# content hash, under this name for the settings above -- change it
# with them -- in $CLIP_CACHE, or cache/clips/ beside this config.
CLIP_SCHEME = "h264-crf24-slower"
# what an animation stored losslessly can be; one is an animation
# when its poster is beside it
ANIMATION_SUFFIXES = (".gif", ".webp")


def _serve_clips(pelican_obj):
    # Make the h264 of every stored animation Pelican copied into the
    # output -- over an AV1 master, beside a gif -- encoding the ones
    # not yet cached in parallel. One that cannot be made stops the
    # build, as an animation that cannot become a clip stops the
    # export: an AV1 4:4:4 file would not play in Safari at all, and a
    # gif cannot be paused.
    import glob
    import hashlib
    import os
    import shutil
    import subprocess
    import sys
    import tempfile
    from concurrent.futures import ThreadPoolExecutor

    posts = os.path.join(pelican_obj.output_path, "posts")
    gifs = [g for ext in ANIMATION_SUFFIXES
            for g in glob.glob(os.path.join(posts, "**", "*" + ext),
                               recursive=True)
            if os.path.exists(os.path.splitext(g)[0] + POSTER_SUFFIX)]
    from_gifs = {os.path.splitext(g)[0] + ".mp4" for g in gifs}
    # (what is stored, where its h264 goes)
    jobs = sorted([(m, m) for m in glob.glob(os.path.join(posts, "**", "*.mp4"),
                                             recursive=True)
                   if m not in from_gifs]
                  + [(g, os.path.splitext(g)[0] + ".mp4") for g in gifs])
    if not jobs:
        return
    here = os.path.dirname(os.path.abspath(__file__))
    cache = os.path.join(os.environ.get("CLIP_CACHE")
                         or os.path.join(here, "cache", "clips"), CLIP_SCHEME)
    os.makedirs(cache, exist_ok=True)
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")

    def serve(job):
        # what became of it: "made" (from the cache), "encoded",
        # "as stored", or why it could not be served
        src, dst = job
        with open(src, "rb") as fh:
            digest = hashlib.file_digest(fh, "sha256").hexdigest()[:16]
        made = os.path.join(cache, digest + ".mp4")
        # the verdict that an .mp4 is served as it is, remembered too
        stored = os.path.join(cache, digest + ".as-stored")
        if os.path.exists(stored):
            return "as stored"
        outcome = "made"
        if not os.path.exists(made):
            if not (ffmpeg and ffprobe):
                return "%s: ffmpeg is not installed" % src
            if src == dst:
                probe = subprocess.run(
                    [ffprobe, "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                     src], capture_output=True, text=True)
                if probe.stdout.strip() != "av1":
                    open(stored, "w").close()
                    return "as stored"
            fd, tmp = tempfile.mkstemp(dir=cache, suffix=".mp4")
            os.close(fd)
            run = subprocess.run(
                [ffmpeg, "-nostdin", "-loglevel", "error", "-y",
                 "-i", src] + CLIP_H264 + [tmp],
                capture_output=True, text=True)
            if run.returncode or not os.path.getsize(tmp):
                os.unlink(tmp)
                detail = (run.stderr or "").strip().splitlines()
                return "%s: ffmpeg failed%s" % (
                    src, ": " + detail[-1] if detail else "")
            os.replace(tmp, made)
            outcome = "encoded"
        shutil.copyfile(made, dst)
        return outcome

    with ThreadPoolExecutor(os.cpu_count() or 1) as pool:
        outcomes = list(pool.map(serve, jobs))
    failed = [o for o in outcomes if ": " in o]
    if failed:
        sys.exit("%d animation(s) could not be made into h264 clips "
                 "(the site's clips need ffmpeg with libx264):\n%s"
                 % (len(failed), "\n".join(failed)))
    served = [(src, o) for (src, _), o in zip(jobs, outcomes)
              if o != "as stored"]
    print("clips: %d served as h264 made from AV1 masters and %d from "
          "gifs and WebPs (%d encoded), %d as stored"
          % (sum(s.endswith(".mp4") for s, _ in served),
             sum(not s.endswith(".mp4") for s, _ in served),
             outcomes.count("encoded"), outcomes.count("as stored")))


def _optimize_article_images(pelican_obj):
    # Rewrite marked body images on post pages: add width/height, add
    # srcset variants for photographs, turn clips into <video>, and
    # strip the marker from every one. Variants are re-encoded only when
    # older than their source.
    try:
        from PIL import Image
    except ImportError:
        print("pillow not installed: body images keep their full-size "
              "originals (pip install pillow and rebuild)")
        return
    import glob
    import os
    import re
    tag_re = re.compile(IMG_TAG)
    attr_re = re.compile(r'([-\w]+)="([^"]*)"')
    marker_re = re.compile(r'\s*\b%s\b(?:="[^"]*")?' % BODY_IMAGE_ATTR, re.I)
    stats = {"variants": 0, "pages": 0}

    here = os.path.dirname(os.path.abspath(__file__))

    def rewrite(match):
        tag = match.group(0)
        if not marker_re.search(tag):    # a theme image
            return tag
        bare = marker_re.sub("", tag, count=1)
        attrs = dict(attr_re.findall(tag))
        src = attrs.get("src", "")
        path = src[len(SITEURL):] if SITEURL and src.startswith(SITEURL) else src
        # a remote image has no local file to measure or re-encode
        if "srcset" in attrs or "://" in path:
            return bare
        if path.lower().endswith(VIDEO_SUFFIXES):
            return _clip(src, path, attrs, pelican_obj.output_path, here)
        if path.lower().endswith(ANIMATION_SUFFIXES):
            # an animation stored as a gif or WebP: its clip is the h264
            # _serve_clips made beside it (a still has none)
            clip = os.path.splitext(path)[0] + ".mp4"
            if os.path.exists(os.path.join(pelican_obj.output_path,
                                           *clip.lstrip("/").split("/"))):
                return _clip(os.path.splitext(src)[0] + ".mp4", clip, attrs,
                             pelican_obj.output_path, here)
        parts = path.lstrip("/").split("/")
        local = os.path.join(pelican_obj.output_path, *parts)
        # encode from and cache against the content-side original:
        # Pelican refreshes the output copy's mtime on every build
        source = os.path.join(here, PATH, *parts)
        if not os.path.exists(source):
            source = local
        try:
            wants_variants = path.lower().endswith(VARIANT_SUFFIXES)
            with Image.open(source) as im:
                width, height = im.size
                srcset = []
                # other images are only measured
                if wants_variants:
                    if im.mode == "P":
                        im = im.convert("RGBA")
                    elif im.mode not in ("RGB", "RGBA"):
                        im = im.convert("RGB")
                for vw in (VARIANT_WIDTHS if wants_variants else ()):
                    if width < vw:
                        continue
                    variant = os.path.splitext(local)[0] + "-%d.webp" % vw
                    if (not os.path.exists(variant) or
                            os.path.getmtime(variant) < os.path.getmtime(source)):
                        vh = max(1, round(height * vw / width))
                        im.resize((vw, vh), Image.Resampling.LANCZOS).save(
                            variant, "WEBP", quality=75)
                        stats["variants"] += 1
                    srcset.append("%s-%d.webp %dw"
                                  % (os.path.splitext(src)[0], vw, vw))
        except OSError:
            return bare
        extra = ""
        if "width" not in attrs and "height" not in attrs:
            extra += ' width="%d" height="%d"' % (width, height)
        if srcset:
            extra += ' srcset="%s" sizes="%s"' % (", ".join(srcset), SIZES_ATTR)
        if not extra:
            return bare
        end = "/>" if bare.endswith("/>") else ">"
        return bare[:-len(end)] + extra + end

    for page in glob.glob(os.path.join(pelican_obj.output_path,
                                       "posts", "*", "*", "index.html")):
        with open(page, encoding="utf-8") as fh:
            html = fh.read()
        rewritten = tag_re.sub(rewrite, html)
        if rewritten != html:
            with open(page, "w", encoding="utf-8") as fh:
                fh.write(rewritten)
            stats["pages"] += 1
    print("responsive images: %(variants)d variants encoded, "
          "%(pages)d pages rewritten" % stats)


def _name_tags(article_generator):
    # Tags arrive as slugs; give each Tag object its display name from
    # TAG_DISPLAY, which also reaches the per-tag feed titles. Pelican
    # makes a separate Tag object per article, so every article's list
    # is pointed at one shared, named object per slug.
    canonical = {}

    def name(tag):
        got = canonical.get(tag.slug)
        if got is None:
            shown = TAG_DISPLAY.get(tag.slug)
            if shown and shown != tag.name:
                tag.slug = tag.slug     # pin it: setting a name otherwise
                tag.name = shown        # re-slugifies, "C++" -> /tags/c/
            canonical[tag.slug] = got = tag
        return got

    for tag in article_generator.tags:      # the dict's keys first, so
        name(tag)                           # its objects are the shared ones
    articles = 0
    for group in ("articles", "translations", "hidden_articles",
                  "hidden_translations", "drafts", "drafts_translations"):
        for article in getattr(article_generator, group, ()):
            if getattr(article, "tags", None):
                article.tags = [name(tag) for tag in article.tags]
                articles += 1
    print(f"tag names: {len(canonical)} tags named across {articles} articles")


def _name_authors(article_generator):
    # _name_tags for authors, from AUTHOR_DISPLAY.
    canonical = {}

    def name(author):
        got = canonical.get(author.slug)
        if got is None:
            shown = AUTHOR_DISPLAY.get(author.slug)
            if shown and shown != author.name:
                author.slug = author.slug   # pin it: setting a name
                author.name = shown         # otherwise re-slugifies
            canonical[author.slug] = got = author
        return got

    for author, _articles in article_generator.authors:
        name(author)
    articles = 0
    for group in ("articles", "translations", "hidden_articles",
                  "hidden_translations", "drafts", "drafts_translations"):
        for article in getattr(article_generator, group, ()):
            if getattr(article, "authors", None):
                article.authors = [name(a) for a in article.authors]
                articles += 1
            if getattr(article, "author", None):
                article.author = name(article.author)
    print(f"author names: {len(canonical)} authors named across "
          f"{articles} articles")


def related_posts(article, articles, limit=3):
    # Up to `limit` posts sharing a tag or author, ranked by shared tags,
    # then shared authors, then nearness in date. Keep the weights in
    # sync with the hugo site's [related] config.
    tags = {t.slug for t in getattr(article, "tags", ())}
    authors = {a.name for a in getattr(article, "authors", ())}

    def score(other):
        return (100 * len(tags & {t.slug for t in getattr(other, "tags", ())})
                + 30 * len(authors & {a.name for a in getattr(other, "authors", ())}))

    ranked = sorted(((score(o), -abs((o.date - article.date).total_seconds()), o)
                     for o in articles if o is not article),
                    key=lambda t: (t[0], t[1]), reverse=True)
    return [o for s, _, o in ranked[:limit] if s > 0]


def _relate_articles(article_generator):
    # article.html reads article.related_posts
    articles = article_generator.articles
    for article in articles:
        article.related_posts = related_posts(article, articles)
    print(f"related posts: {len(articles)} articles")


def _check_slugs(article_generator):
    # Stop the build, naming the posts, when two would write the same
    # page; Pelican's own error names neither post and is swallowed by
    # `pelican -r`. Also report, without failing, posts whose slug or
    # URL year differs from their directory.
    import collections
    import os
    import sys
    pages = collections.defaultdict(list)
    for article in article_generator.articles + article_generator.translations:
        pages[article.save_as].append(article.relative_source_path)
    renamed = sorted(
        f"{a.relative_source_path} is served at /{a.url}"
        for a in article_generator.articles
        if os.path.basename(os.path.dirname(a.relative_source_path)) != a.slug)
    for line in renamed:
        print(f"slug: {line}")
    misfiled = sorted(
        f"{a.relative_source_path} is served at /{a.url}"
        for a in article_generator.articles
        if getattr(a, "diryear", None)
        and str(a.diryear) != a.date.strftime("%Y"))
    for line in misfiled:
        print(f"year: {line}")
    clashes = sorted((page, sorted(paths))
                     for page, paths in pages.items() if len(paths) > 1)
    if clashes:
        sys.exit("\n".join(
            [f"{len(paths)} posts share a slug, and would write {page}:"
             + "".join(f"\n  {path}" for path in paths)
             for page, paths in clashes]))
    print(f"slugs: {len(pages)} posts, {len(renamed)} not under their "
          "directory name")


class _SitePlugins:
    @staticmethod
    def register():
        from pelican import signals
        signals.article_generator_finalized.connect(_check_slugs)
        signals.article_generator_finalized.connect(_name_tags)
        signals.article_generator_finalized.connect(_name_authors)
        signals.article_generator_finalized.connect(_relate_articles)
        signals.article_generator_finalized.connect(_collect_sitemap)
        signals.finalized.connect(_prioritize_first_images)
        # the clips first: a gif's page shows the clip made beside it
        signals.finalized.connect(_serve_clips)
        signals.finalized.connect(_optimize_article_images)
        signals.finalized.connect(_write_redirects)
        signals.finalized.connect(_write_crawl_files)


# _CommonMarkPlugin is defined in the config section above
PLUGINS = [_CommonMarkPlugin, _SitePlugins]
