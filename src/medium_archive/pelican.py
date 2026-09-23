"""The pelican step: build a Pelican site in site-pelican/ from
the converted archive. Same reproducibility contract as the myst step
(see sites.py); render with `pelican -l` (serve) or `pelican` (build)
inside site-pelican/ (https://getpelican.com, `pip install
pelican markdown-it-py mdit-py-plugins pyyaml`).

What the site says about itself and how it is built are separate
files. site.toml beside the config is the data -- the archive's own
site/site.toml resolved for this site, with the images as the copies
the site carries and what the exporter works out from several keys at
once (the profile list, the masthead link's label, the newsletter
band's defaults) written out as what it came to. pelicanconf.py reads
it, one key at a time, into the settings the theme and the plugins
render from; nothing else in that file is site data, so it is copied
from templates/pelican/pelicanconf.py rather than filled in, and it is
the same bytes for every archive. A checked-in copy of this site edits
site.toml and the two data/*.yaml name maps and nothing else. The
hugo site splits the same two apart in its own config directory (see
hugo.py).

The site reads CommonMark rather than the python-markdown dialect
Pelican reads by default: the generated config carries a reader built
on markdown-it-py, which replaces Pelican's own (see the config
template). That is where everything this site needs from the Markdown
layer hangs -- heading ids, Pygments on the class the shared theme
styles, Pelican's {attach} placeholders, the body-image marking, and
the figure directive below.

Each post becomes content/posts/<year>/<stem>/index.md with its images
beside it, filed under the year it was published in; image references
are rewritten to Pelican's `{attach}` form so the files publish next to
the article at /posts/<year>/<stem>/. Twelve directories of about
thirty beat one of several hundred, and an address carries the year, so
a reader can date a post before opening it. The year in the address is
the year of the post's own `date:` (ARTICLE_URL), not of the directory
it sits in, so an address states when a post was published even if its
file is filed under the wrong year; the site plugin reports a post
whose two disagree. /posts/<year>/ is an address a reader reaches by
trimming a post's, so it answers with that year's posts
(YEAR_ARCHIVE_SAVE_AS, period_archives.html) rather than a 404.

The stem is the page's URL slug, and the directory is the whole of
where that comes from: the generated config reads a post's directory
name as its slug (PATH_METADATA), the way hugo reads a page bundle's,
so neither site restates in a post what its directory already says. A
post that does carry a `slug:` is one deliberately served elsewhere --
what keeps a URL through a rename -- and the site plugin reports it;
two posts that would write the same page stop the build. Body images load lazily (the reader marks every image in an article's body) and are
served responsively: after each build the embedded plugin
encodes webp variants of every still body image at the same widths as
the hugo theme's render hook (480/736/1104, never upscaled,
mtime-cached) and rewrites the article's img tags with srcset/sizes
and real width/height. Metadata is YAML front matter between `---`
fences, the shape the hugo site writes too (see
sites.front_matter_yaml) rather than Pelican's own `Key: value`
headers, which nothing else reads; tags and authors are first-class in
Pelican, so tag/author listing pages and Atom feeds (site-wide and per
tag/author) come out of the box. Tags
reach Pelican as slugs, so tag.slug and every /tags/<slug>/ URL are
exactly the archive's tag rather than whatever Pelican's slugify would
make of a name like "C++"; the names tags are shown under (tags.json's
`display`) arrive instead as one data file beside the config,
data/tags.yaml, which pelicanconf.py reads into TAG_DISPLAY and the
site plugin names the Tag objects from once the tags are collected --
Pelican renders a tag from the object everywhere, the per-tag feed
title included, and that one it builds in Python out of reach of a
template. Authors take the same route, and need it more: a byline is a
person's name, so left as the term it would reach each generator raw --
hugo keeping its accents and punctuation in the path, Pelican folding
them away -- and one author would sit at two addresses. Both exporters
write sites.author_slug's slug instead, and data/authors.yaml carries
one entry per slug: the name, which the site plugin puts on the Author
objects the way it does the tags, and the profile the structured data
reads (AUTHOR_DISPLAY and AUTHOR_LINKS, both keyed by the slug). Both
files are what the hugo site is built from too, with the same names and
contents (see sites.write_data_files): they are generated, but a name
or a profile is exactly what a checked-in copy of a site corrects by
hand, and the config that reads them is not.

The exporter writes its own theme, from the package's templates/pelican/
and templates/shared/ files -- the card-grid blog shared with
the hugo step, light and dark palettes and the header's
light/dark/system picker included: paginated home of cover-image
cards, article pages (with click-to-zoom body images: an image whose
original holds more detail than the column shows opens full size in a
modal, like Medium's), tag/author card listings, chip indexes
(sortable by name or by post count), an
archives timeline, and a /search/ page wired to Pagefind
(run `pagefind --site output` after `pelican` for full-text search with
highlighted, in-context excerpts). Card covers are 640x360 thumbnails
generated at export time when Pillow is installed (`pip install
pillow`), center-cropped or letterboxed by aspect ratio (see
sites.make_cover_thumbnail); without it, cards use the full-size image.

An animation is stored as a master, beside the clip's poster: the
smaller of its frame-rate-capped gif and lossless WebP, or the AV1
4:4:4 master of its clip where that saves enough (see
sites.CLIP_MASTER and MASTER_MAX_SHARE). The site plugin makes the
h264 a browser is served from it on every build, and the page shows
that as a <video>: the site keeps one file per animation that every later
format can be made from, and a format change is a change to the plugin
rather than a new copy of every clip in the site's history. Building
the site therefore needs ffmpeg with libx264. The h264 is cached by the
master's content hash under cache/clips/ in the site, or under
$CLIP_CACHE (the archive's pixi task points it into .image-cache/).
site.toml's [images] clip_master = "none" stores the h264 clips the
other sites carry instead, which the plugin serves as they are.

Pelican has no built-in equivalent of Hugo's aliases, so the
generated config embeds a small plugin (templates/pelican/site_plugin.py,
appended verbatim): after each build it reads the exported redirects.csv
-- the map of every old inbound path (Medium slug+id, /p/<id>,
Ghost-era) to the page this site serves -- and renders it as whichever
mechanism site.toml's "redirects" asks for (see sites.REDIRECT_MODES): a
meta-refresh stub at each old path, the same page Hugo renders for an
alias and working on any static host; a `_redirects` file for the hosts
that turn one into HTTP 301s; or both.
The plugin also writes what Pelican has no built-in for and Hugo emits
on its own: a sitemap.xml of the site's pages (post lastmod from the
updated date) and a robots.txt naming it, and the "More posts" block
each post page closes with (by shared tags, then author, then date);
the theme's pages carry the metadata search engines and share targets read
(see templates/README.md): the structured data's author and publisher
profiles come from AUTHOR_LINKS (data/authors.yaml: the Medium profile
of every byline) and site.toml's "profiles"/"twitter", the og:image of a page with no
cover from its "share_image", and a post that declared a canonical on
another host (Medium's "originally published at") carries it as a
Canonical: header; the Medium copy is never a page's canonical.
"""

import re
import sys
from pathlib import Path

from .paths import (DEFAULT_IMAGE_CACHE, DEFAULT_SITE_INPUTS,
                    default_out)
from .sites import (COVER_SIZE, Covers, ImagePlacer, author_slug,
                    canonical_for, caption_text, clean_out,
                    copy_site_asset, export_content,
                    front_matter_yaml, image_size, load_site_inputs,
                    masthead_link, newsletter_params, page_stems,
                    page_paths, post_year,
                    quote_arg, redirect_mode, report_stale_pages,
                    rewrite_figures,
                    site_profiles, template_text, write_data_files,
                    write_redirects_csv, write_site_config, write_templates)

# The files the exporter copies in: file in the site -> its templates/
# source (see templates/README.md). The theme is most of them, and its
# stylesheet is the card look shared with the hugo theme; the README
# and the .gitignore are what make the directory a repository of its
# own rather than a build output, which is what it becomes once the
# archive is done with it. (The .gitignore's source is named without
# the dot, so that git does not read it as an ignore file for
# templates/pelican/ itself.)
TEMPLATES = {
    "README.md": "pelican/README.md",
    ".gitignore": "pelican/gitignore",
    "theme/templates/base.html": "pelican/theme/templates/base.html",
    "theme/templates/jsonld.html": "pelican/theme/templates/jsonld.html",
    "theme/templates/macros.html": "pelican/theme/templates/macros.html",
    "theme/templates/pagination.html":
        "pelican/theme/templates/pagination.html",
    "theme/templates/index.html": "pelican/theme/templates/index.html",
    "theme/templates/article.html": "pelican/theme/templates/article.html",
    "theme/templates/term.html": "pelican/theme/templates/term.html",
    "theme/templates/tag.html": "pelican/theme/templates/tag.html",
    "theme/templates/author.html": "pelican/theme/templates/author.html",
    "theme/templates/terms.html": "pelican/theme/templates/terms.html",
    "theme/templates/tags.html": "pelican/theme/templates/tags.html",
    "theme/templates/authors.html": "pelican/theme/templates/authors.html",
    "theme/templates/archives.html": "pelican/theme/templates/archives.html",
    "theme/templates/period_archives.html":
        "pelican/theme/templates/period_archives.html",
    "theme/templates/search.html": "pelican/theme/templates/search.html",
    "theme/static/css/style.css": "shared/card.css",
}

IMAGE_RE = re.compile(r"\]\((images/[^)\s]+)\)")


def attach_images(line: str) -> str:
    """Colocated image references become {attach} links, so Pelican
    copies each file next to its article and rewrites the URL."""
    return IMAGE_RE.sub(r"]({attach}\1)", line)


def figure_directives(markdown: str) -> str:
    """Convert's figure shells as calls to the figure directive the
    generated config's reader renders, the caption as the directive's
    body -- the counterpart of the hugo exporter's figure shortcode,
    and for the same reasons. An attribute could not carry the
    caption's Markdown (links, emphasis), and rendering the shell as
    raw HTML instead, as this exporter did while pelican rendered with
    python-markdown, needs that library's md_in_html extension to
    render the caption at all: CommonMark says the contents of an HTML
    block are raw. Rendering through the directive also keeps the img
    and the caption out of <p> wrappers, marks the img as a body image
    for the site plugin's post-build pass, and renders the caption with
    the site's own parser, so it picks up whatever extensions the
    config enables.

    A shell around anything else (the link an embed became, an inlined
    gist) stays raw HTML, which the reader passes through: its lines
    are blank-line separated, which ends the HTML block and leaves the
    Markdown between them to be parsed as Markdown.

    The image reference already carries the {attach} prefix: escape
    runs first. It is left exactly as it is here -- the reader writes
    it into an attribute, which no parser touches, so pelican's
    intra-site link pass resolves it on the rendered page."""
    def directive(alt, src, link, caption):
        args = f'src="{src}"'
        # plain text, like the hugo exporter's: an alt is prose for a
        # screen reader, and the two sites should read it out the same
        alt = caption_text(alt or caption)    # the caption describes it
        if alt:
            args += f' alt="{quote_arg(alt)}"'
        if link:
            args += f' link="{quote_arg(link)}"'
        return f"::: figure {args}\n{caption}\n:::"
    return rewrite_figures(markdown, directive)


def _one_line(value: str) -> str:
    return " ".join(value.split())    # front matter holds no newlines


def build_site(archive: Path, out=None, inputs=DEFAULT_SITE_INPUTS,
               cache=DEFAULT_IMAGE_CACHE, clean=False) -> Path:
    """The pelican site in `out`, from the archive and the hand-written
    site inputs. The site is self-contained, so it builds as happily
    inside a repository of its own as beside the archive it came from:
    each file is written where it belongs and everything else in `out`
    is left alone, unless --clean sweeps it first (see clean_out)."""
    archive, inputs = Path(archive), Path(inputs)
    site = Path(out) if out is not None else default_out("pelican")
    manifest, config = load_site_inputs(archive, inputs)
    stems = page_stems(manifest)
    mode = redirect_mode(config)        # site.toml "redirects"
    if clean:
        clean_out(site, build_dirs=("output", "cache"))
    (site / "content").mkdir(parents=True, exist_ok=True)
    covers = Covers(archive, manifest)

    def front_matter(url, post):
        fields = {"title": _one_line(post["title"] or url)}
        if post.get("date"):
            fields["date"] = post["date"][:16].replace("T", " ")
        if post.get("updated"):
            fields["modified"] = post["updated"][:16].replace("T", " ")
        if post.get("authors"):
            # slugs, as the tags are, so author.slug and every
            # /authors/<slug>/ URL are exactly what the hugo site
            # serves rather than whatever each engine would make of a
            # byline (see sites.author_slug); the site plugin names the
            # Author objects from AUTHOR_DISPLAY once they are collected.
            fields["authors"] = [author_slug(a["name"])
                                 for a in post["authors"]]
        if post.get("tags"):
            fields["tags"] = list(post["tags"])
        if covers.path(url):
            fields["cover"] = covers.path(url)
        # the page's subtitle line; a FORMATTED_FIELD in the generated
        # config, so the reader renders its Markdown (article.html)
        if post.get("subtitle"):
            fields["subtitle"] = _one_line(post["subtitle"])
        if post.get("description"):
            fields["summary"] = _one_line(post["description"])
        if canonical_for(post):       # a copy of that page, and says so
            fields["canonical"] = canonical_for(post)
        return front_matter_yaml(fields)

    pages = export_content(archive, site, manifest, stems, front_matter,
                           escape=attach_images,
                           placer=ImagePlacer(cache, config, masters=True),
                           transform=figure_directives, covers=covers)

    # the header logo and the tab icon, shipped through the theme's
    # static dir
    avatar = copy_site_asset(inputs, config.get("avatar"),
                             site / "theme" / "static" / "img", "avatar")
    favicon = copy_site_asset(inputs, config.get("favicon"),
                              site / "theme" / "static", "favicon")
    # a masthead logo that stands in for the site's name -- a wordmark,
    # the way jupyter.org's navbar carries its rectangle logo -- and the
    # same mark drawn for the dark palette
    logo = copy_site_asset(inputs, config.get("logo"),
                           site / "theme" / "static" / "img", "logo")
    logo_dark = (copy_site_asset(inputs, config.get("logo_dark"),
                                 site / "theme" / "static" / "img",
                                 "logo-dark") if logo else None)
    # the og:image of every page without a cover of its own, with its
    # dimensions read here (the theme has no image pipeline)
    share = copy_site_asset(inputs, config.get("share_image"),
                            site / "theme" / "static" / "img", "share")
    share_size = (image_size(site / "theme" / "static" / "img" / share)
                  if share else None)
    # site.toml beside the config: this site's own data, the archive's
    # site/site.toml resolved for this site -- the images as the copies
    # placed above, and what is worked out from several keys at once
    # (the profile list, the masthead link's label, the newsletter
    # band's defaults) as what it came to. Every key the config reads
    # is written under its own documentation, unset ones commented out
    # beside an example, so the file is also the list of what there is
    # to set and what each one would look like set (see siteconf). It,
    # and data/*.yaml below, are the whole of what a checked-in copy of
    # this site edits by hand; pelicanconf.py is machinery and says so,
    # holding not one line about what any of these keys mean.
    write_site_config(site, {
        "title": config["title"],
        "description": config.get("description") or None,
        "base_url": config.get("base_url") or None,
        "locale": config.get("locale") or "en",
        "intro": config.get("intro") or None,
        "footer": config.get("footer") or None,
        "avatar": avatar and f"theme/img/{avatar}",
        "favicon": favicon and f"theme/{favicon}",
        "logo": logo and f"theme/img/{logo}",
        "logo_dark": logo_dark and f"theme/img/{logo_dark}",
        # read, like the dark mark, only where there is a logo to
        # carry it (see sites.masthead_link)
        "logo_link": (logo and masthead_link(config)) or None,
        "announcement": config.get("announcement") or None,
        # see sites.newsletter_params
        "newsletter": newsletter_params(config),
        "twitter": config.get("twitter") or None,
        "plausible": config.get("plausible") or None,
        "profiles": site_profiles(config) or None,
        "share_image": share and f"theme/img/{share}",
        "share_image_size": list(share_size) if share_size else None,
        "cover_size": list(COVER_SIZE) if covers.pillow else None,
        "noindex": bool(config.get("noindex")),
        # which mechanism the embedded plugin renders redirects.csv
        # as (see sites.REDIRECT_MODES)
        "redirects": mode,
    })
    # the config itself carries no site data, so it is copied rather
    # than filled in: the same bytes for every archive, with the site
    # plugin appended verbatim
    (site / "pelicanconf.py").write_text(
        template_text("pelican/pelicanconf.py") + "\n\n"
        + template_text("pelican/site_plugin.py"), encoding="utf-8")

    # data/tags.yaml and data/authors.yaml: the maps the site plugin
    # names the Tag and Author objects from, each author's entry
    # carrying the profile the structured data names as their sameAs
    # too. The config reads both at build time, so a name or a profile
    # is corrected by editing one small file instead of a generated
    # config; they are the same two files, with the same contents, the
    # hugo site reads through hugo.Data (see sites.write_data_files).
    write_data_files(site, manifest, archive)
    write_templates(site, TEMPLATES)
    write_redirects_csv(site, manifest, stems,
                        page_paths(manifest, stems).__getitem__)
    report_stale_pages(site / "content" / "posts",
                       {f"{post_year(p)}/{stems[url]}"
                        for url, p in manifest.items()}, nested=True)
    print(f"pelican done: {pages}/{len(manifest)} pages -> {site}",
          file=sys.stderr)
    print(f"render it with: cd {site} && pelican && pagefind --site output"
          "   (or serve with: pelican -l)", file=sys.stderr)
    return site


def cmd_pelican(args):
    build_site(args.archive, args.out, args.site_inputs,
               args.image_cache, args.clean)
