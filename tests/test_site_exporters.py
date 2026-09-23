"""The hugo and pelican steps: archive/posts/ + posts.json -> a site."""

import datetime
import json
import re
import sys

import yaml
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader

from medium_archive import hugo, pelican, sites
from _project import archive_dir, build, image_cache, site_inputs
from medium_archive.paths import site_config
from medium_archive.siteconf import load_toml, toml_document

BASE = "https://blog.example.com"


def site_toml(root: Path) -> Path:
    """The project's hand-written site/site.toml, its directory made on
    demand."""
    site_config(site_inputs(root)).parent.mkdir(parents=True, exist_ok=True)
    return site_config(site_inputs(root))


def read_site(root: Path) -> dict:
    """That file, as the exporters read it."""
    return load_toml(site_toml(root))


def write_site(root: Path, config: dict):
    """That file, written from a dict: plain TOML, since what a test
    sets is read back by an exporter and not by a person (the
    documented form each exporter writes is siteconf.documented_toml).
    A None value is a key the file does not carry, TOML having no null."""
    site_toml(root).write_text(toml_document(config), encoding="utf-8")


def site_asset(root: Path, name: str) -> Path:
    """An image site.toml names, which it names relative to itself."""
    return site_toml(root).parent / name


def hugo_config(site: Path) -> str:
    """The hugo site's configuration, its two files read as one:
    config/_default/hugo.toml is how the site is built, params.toml is
    what it says about itself."""
    default = site / "config" / "_default"
    return ((default / "hugo.toml").read_text() + "\n"
            + (default / "params.toml").read_text())


def hugo_params(site: Path) -> dict:
    """The hugo site's params, as Hugo reads them. A key the site does
    not set is absent, the file documenting it commented out beside an
    example of it set, so a "does it set this?" question is asked of
    the parsed params rather than of the text."""
    return load_toml(site / "config" / "_default" / "params.toml")


def pelican_data(site: Path) -> dict:
    """site.toml beside the pelican site's config: its own data, which
    the generated pelicanconf.py reads and holds none of. A key the
    site does not set is absent (written commented out, beside an
    example of it set), TOML having no null to write."""
    return load_toml(site / "site.toml")


def manifest_json(root: Path) -> Path:
    """The converted archive's posts.json."""
    archive_dir(root).mkdir(parents=True, exist_ok=True)
    return archive_dir(root) / "posts.json"


def make_post(root: Path, manifest: dict, slug: str, mid: str, date: str,
              body: str, **extra) -> str:
    url = f"{BASE}/{slug}-{mid}"
    d = f"posts/{date[:10]}-{slug}"
    post = {"title": slug.replace("-", " ").title(), "date": date,
            "authors": [{"name": "Ada Lovelace", "url": "https://medium.com/@ada"}],
            "updated": None, "original_url": url,
            "original_path": f"/{slug}-{mid}", "medium_id": mid, "slug": slug,
            "canonical_url": None, "ghost_url": None, "description": "About " + slug,
            "tags": ["example"], "images": [], "body_source": "page",
            "dir": d, **extra}
    manifest[url] = post
    post_dir = archive_dir(root) / d
    post_dir.mkdir(parents=True)
    (post_dir / "index.md").write_text(
        "---\n" + json.dumps({k: v for k, v in post.items() if k != "dir"})
        + "\n---\n\n" + body, encoding="utf-8")
    return url


@pytest.fixture
def project(tmp_path):
    manifest = {}
    make_post(tmp_path, manifest, "first-post", "aaa111aaa111",
              "2020-01-05T10:00:00Z",
              f"Hello. See [the sequel]({BASE}/second-post-bbb222bbb222).\n",
              ghost_url=f"{BASE}/2015/06/01/first-post")
    second = archive_dir(tmp_path) / "posts/2021-03-01-second-post"
    make_post(tmp_path, manifest, "second-post", "bbb222bbb222",
              "2021-03-01T10:00:00Z",
              "An image:\n\n![pic](images/001-pic.png)\n\n"
              "```\n![fenced](images/lit.png)\n```\n",
              images=["images/001-pic.png"])
    (second / "images").mkdir()
    (second / "images" / "001-pic.png").write_bytes(b"PNG")
    manifest_json(tmp_path).write_text(json.dumps(manifest))
    write_site(tmp_path, 
        {"title": "Example Blog", "description": "An example.",
         "intro": "Welcome.", "base_url": "https://blog.example.org/"})
    return tmp_path


def test_hugo_site(project):
    site_asset(project, "icon.svg").write_bytes(b"SVG")
    cfg = read_site(project)
    cfg["favicon"] = "icon.svg"
    write_site(project, cfg)
    site = build(hugo, project)
    page = site / "content/posts/2021/second-post/index.md"
    front = page_front(page)
    assert front["title"] == "Second Post"
    assert front["tags"] == ["example"] and front["authors"] == ["ada-lovelace"]
    assert front["aliases"] == ["/second-post-bbb222bbb222", "/p/bbb222bbb222"]
    assert (page.parent / "images/001-pic.png").read_bytes() == b"PNG"
    # the Ghost-era path becomes an alias too
    first = post_front(site, "first-post")
    assert "/2015/06/01/first-post" in first["aliases"]
    # in-publication links point at the new page URLs
    assert "](/posts/2021/second-post/)" in (site / "content/posts/2020/first-post/index.md").read_text()
    config = hugo_config(site)
    assert 'baseURL = "https://blog.example.org/"' in config
    assert 'author = "authors"' in config
    # the tab icon lands at the site root, under its canonical name
    assert 'favicon = "favicon.svg"' in config
    assert (site / "static/favicon.svg").read_bytes() == b"SVG"
    assert 'rel="icon"' in (site / "layouts/baseof.html").read_text()
    # full-content feed, capped: announce new posts, don't ship the archive
    assert "[services.rss]\nlimit = 20" in config
    rss = (site / "layouts/rss.xml").read_text()
    assert "content:encoded" in rss and "srcset" in rss
    # a literal <?xml gets HTML-escaped by Hugo's template engine,
    # producing an invalid feed; it must go through safeHTML
    assert 'printf "<?xml' in rss and "safeHTML" in rss
    assert not rss.lstrip("{}-% \n").startswith("<?xml")
    assert (site / "layouts/page.html").exists()
    # a year-grouped archives timeline, like the pelican theme's
    assert (site / "layouts/archives.html").exists()
    assert page_front(site / "content/archives.md")["layout"] == "archives"
    assert "Welcome." in (site / "content/_index.md").read_text()
    assert (site / "redirects.csv").read_text().count("/posts/2020/first-post/") == 3


def test_hugo_front_matter_is_yaml(project):
    """Hugo's metadata is YAML between `---` fences: the format its own
    documentation and themes are written in, and the one the pelican
    site writes, so a field is read and hand-edited the same way in
    either site. It comes from the yaml library, so a title holding a
    colon or a quote, a tag that reads as a boolean and a date that
    reads as a timestamp are quoted as the specification requires and
    read back as the strings they are."""
    site = build(hugo, project)
    text = (site / "content/posts/2021/second-post/index.md").read_text()
    assert text.startswith("---\ntitle:")
    body = text.split("---\n", 2)[2]
    assert not body.lstrip().startswith("title:")
    # the site's own pages carry the same fences
    assert (site / "content/_index.md").read_text().startswith("---\n")
    assert page_front(site / "content/_index.md") == {"title": "Example Blog"}

    # what hand-written front matter gets wrong, and yaml does not
    tricky = {"title": 'Voil\u00e0: "quoted", 5 - 3', "tags": ["no", "c++"],
              "date": "2021-03-01T10:00:00.000Z", "aliases": ["/p/abc"]}
    assert yaml.safe_load(
        sites.front_matter_yaml(tricky).split("---\n")[1]) == tricky


FIGURE_BODY = ("<figure>\n\n![Alt text](images/001-fig.gif)\n\n"
               "<figcaption>\n\nThe caption, with a "
               "[link](https://example.com).\n\n</figcaption>\n\n"
               "</figure>\n")


def captioned_archive(project):
    manifest = json.loads(manifest_json(project).read_text())
    make_post(project, manifest, "captioned-post", "ccc333ccc333",
              "2022-06-01T10:00:00Z", FIGURE_BODY,
              images=["images/001-fig.gif"])
    d = archive_dir(project) / "posts/2022-06-01-captioned-post"
    (d / "images").mkdir()
    # a real one-frame gif: a file that will not read as a gif is an
    # error where animations become clips
    gradient_frame((16, 12), 0).save(d / "images" / "001-fig.gif")
    manifest_json(project).write_text(json.dumps(manifest))
    return project


def test_hugo_page_keeps_caption_in_its_figure(project):
    site = build(hugo, captioned_archive(project))
    # the shell becomes a call to the shipped figure shortcode, the
    # caption as inner content so its Markdown still renders
    page = (site / "content/posts/2022/captioned-post/index.md").read_text()
    assert ('{{< figure src="images/001-fig.gif" alt="Alt text" >}}'
            "The caption, with a [link](https://example.com)."
            "{{< /figure >}}") in page
    # the shortcode the figure calls resolve to, and the image partial
    # it shares with the render hook
    assert (site / "layouts/_shortcodes/figure.html").exists()
    assert (site / "layouts/_partials/post-image.html").exists()


def page_front(path):
    """A page's front matter, parsed: YAML between `---` fences, the
    form both exporters write."""
    _, front, _ = Path(path).read_text().split("---\n", 2)
    return yaml.safe_load(front)


def post_page(site, stem):
    """A post's page in a hugo or pelican site. The tree is
    content/posts/<year>/<stem>/, so a test with nothing to say about
    the filing names the stem and lets the year be found; the ones that
    are about it name the year outright."""
    hits = sorted((site / "content" / "posts").glob(f"*/{stem}/index.md"))
    assert len(hits) == 1, f"{stem}: {hits}"
    return hits[0]


def post_front(site, stem):
    """The front matter of a site's post page -- hugo's and pelican's
    alike, which is the point of them sharing the format."""
    return page_front(post_page(site, stem))


def config_namespace(site):
    """The generated config, executed as pelican executes it: from its
    own path, so the `data/*.json` files it reads beside itself are
    found. It is executable Python and needs neither pelican nor the
    site's own build, so its settings, its reader and the site plugin's
    functions can all be exercised from here."""
    path = site / "pelicanconf.py"
    namespace = {"__file__": str(path)}
    exec(compile(path.read_text(), "pelicanconf.py", "exec"), namespace)
    return namespace


def config_parser(site):
    """The markdown-it parser the generated config reads posts with.
    The config's reader is built where a pelican build would build it,
    so this is the same parser the site renders with -- without needing
    pelican itself installed."""
    namespace = config_namespace(site)
    return namespace, namespace["_make_md"]()


def test_pelican_page_writes_the_figure_directive(project):
    """The shell becomes the figure directive the generated config's
    reader renders -- the counterpart of the hugo exporter's figure
    shortcode. The caption is the directive's body, so it can hold
    Markdown an attribute could not, and the reader renders it with the
    site's own parser: CommonMark says the contents of an HTML block
    are raw, so a raw <figure> in the content would show the caption's
    Markdown to the reader."""
    site = build(pelican, captioned_archive(project))
    page = (site / "content/posts/2022/captioned-post/index.md").read_text()
    assert ('::: figure src="{attach}images/001-fig.gif" alt="Alt text"\n'
            "The caption, with a [link](https://example.com).\n"
            ":::") in page

    _, md = config_parser(site)
    html = md.render(page.split("\n\n", 1)[1])
    # no <p> wrappers around the img or the caption, matching the
    # markup Medium serves and the hugo shortcode renders
    assert "<p><img" not in html and "<figcaption><p>" not in html
    assert ('<figcaption>The caption, with a '
            '<a href="https://example.com">link</a>.</figcaption>') in html
    # the image is the article's own body image, and keeps the {attach}
    # pelican resolves on the rendered page
    assert ('<img alt="Alt text" src="{attach}images/001-fig.gif" '
            'loading="lazy" data-body-image="">') in html


def test_the_figure_directive_escapes_what_it_renders(project):
    """The caption is Markdown, rendered by the site's parser; what it
    renders into an HTML attribute or a text node has to be escaped
    there, not left to the exporter."""
    _, md = config_parser(build(pelican, project))
    html = md.render('::: figure src="images/a.png" alt="R&D, 5 < 6"\n'
                     "A `code` span, R&D and 5 < 6.\n:::\n")
    assert 'alt="R&amp;D, 5 &lt; 6"' in html
    assert ("<figcaption>A <code>code</code> span, R&amp;D and 5 &lt; 6."
            "</figcaption>") in html


def test_the_front_matter_is_yaml_the_reader_reads_back(project):
    """Front matter is YAML between `---` fences, as `posts/` and the
    hugo site write it, rather than pelican's own `Key: value` headers
    that only pelican reads. It is written by the yaml library, so a
    title holding a colon or a quote is quoted as the specification
    requires, and the reader splits on the fence rather than on the
    first blank line."""
    site = build(pelican, project)
    namespace, _ = config_parser(site)
    text = (site / "content/posts/2021/second-post/index.md").read_text()
    front, body = namespace["_split_front_matter"](text)
    assert yaml.safe_load(front)["title"] == "Second Post"
    assert not body.lstrip().startswith("title:")

    # what hand-written front matter gets wrong, and yaml does not
    tricky = {"title": 'Voil\u00e0: "quoted", 5 - 3', "tags": ["a", "b"],
              "date": "2021-03-01 10:00", "slug": "x"}
    parsed = yaml.safe_load(
        pelican.front_matter_yaml(tricky).split("---\n")[1])
    assert parsed == tricky

    # a horizontal rule in the body is not a front-matter fence
    front, body = namespace["_split_front_matter"]("Text.\n\n---\n\nMore.\n")
    assert front == "" and body.startswith("Text.")


def test_the_typographer_sets_prose_the_way_hugo_does(project):
    """Goldmark's typographer is on in the hugo site, so the pelican
    reader runs markdown-it's: without it the two sites set the same
    prose differently. The symbol substitutions are the one part left
    off -- markdown-it has them and goldmark does not, and "501(c)(3)"
    is a nonprofit, not a copyright sign."""
    _, md = config_parser(build(pelican, project))
    assert md.renderInline('He said "no", it\'s fine') == \
        "He said \u201cno\u201d, it\u2019s fine"
    assert md.renderInline("wait...") == "wait\u2026"
    assert md.renderInline("a -- b and c---d") == \
        "a \u2013 b and c\u2014d"
    # a flag is not a dash, and code is not prose
    assert md.renderInline("pip install --upgrade x") == \
        "pip install --upgrade x"
    assert md.renderInline("`\"quoted\"`") == "<code>&quot;quoted&quot;</code>"
    # the (c)/(tm)/(r) substitutions markdown-it would make and
    # goldmark does not; "+-" stays part of the rule that is called
    assert md.renderInline("a 501(c)(3) nonprofit (tm) (r)") == \
        "a 501(c)(3) nonprofit (tm) (r)"


def test_the_reader_registers_through_a_receiver_that_outlives_it(project):
    """pelican holds a signal's receivers weakly, so a receiver defined
    inside register() is collected on the way out and the reader never
    registers at all -- a build that silently falls back to
    python-markdown and renders every figure directive as text. The
    receiver has to be reachable after register() returns, which means
    the config's own module namespace holds it."""
    namespace, _ = config_parser(build(pelican, project))
    connected = []
    stub = ModuleType("pelican")
    stub.signals = SimpleNamespace(
        readers_init=SimpleNamespace(connect=connected.append))
    saved = sys.modules.get("pelican")
    sys.modules["pelican"] = stub
    try:
        namespace["_CommonMarkPlugin"].register()
    finally:
        if saved is None:
            del sys.modules["pelican"]
        else:
            sys.modules["pelican"] = saved
    receiver, = connected
    assert any(receiver is value for value in namespace.values())


def test_the_config_reads_commonmark(project):
    """The reader is a CommonMark parser (markdown-it-py), with the
    pieces this site needs hung off it. python-markdown, which pelican
    reads with by default, follows no specification and differs on all
    of these."""
    _, md = config_parser(build(pelican, project))

    # heading ids, as the search page's per-section anchors
    assert '<h2 id="voila-and-friends">' in md.render("## Voilà and friends\n")
    # code blocks on the class the shared stylesheet styles, which is
    # also the one hugo's chroma emits
    assert '<div class="highlight">' in md.render("```python\nx = 1\n```\n")
    assert '<div class="highlight">' in md.render("```\nplain\n```\n")
    # pelican's placeholders survive the parser's URL encoding, so its
    # intra-site pass still finds them on the rendered page
    assert 'src="{attach}images/a.png"' in md.render("![x]({attach}images/a.png)")
    assert 'href="{attach}notes.pdf"' in md.render("[x]({attach}notes.pdf)")
    # tables, strikethrough, footnotes and definition lists are on
    assert "<table>" in md.render("| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert "<s>gone</s>" in md.render("~~gone~~\n")
    assert "footnote" in md.render("A[^1]\n\n[^1]: note\n")
    assert "<dl>" in md.render("term\n: meaning\n")
    # and the specification is followed where python-markdown guessed:
    # a bare <word> is text, not a dropped tag, and a list that starts
    # at 3 says so
    assert "&lt;my_package&gt;" in md.render("recipes/<my_package>\n")
    assert '<ol start="3">' in md.render("3. third\n4. fourth\n")


def test_link_wrapped_figures_keep_their_link():
    shell = ("<figure>\n\n[![Alt](images/a.png)](https://demo.example)\n\n"
             "<figcaption>\n\nCap.\n\n</figcaption>\n\n</figure>")
    assert hugo.figure_shortcodes(shell) == (
        '{{< figure src="images/a.png" alt="Alt" '
        'link="https://demo.example" >}}Cap.{{< /figure >}}')
    assert pelican.figure_directives(shell) == (
        '::: figure src="images/a.png" alt="Alt" '
        'link="https://demo.example"\nCap.\n:::')


def test_non_image_figure_shells_stay_raw_html():
    shell = ("<figure>\n\n[embed: https://u](https://u)\n\n<figcaption>\n\n"
             "Cap.\n\n</figcaption>\n\n</figure>")
    # neither exporter touches them: hugo leaves them to Goldmark's
    # unsafe renderer, and the pelican reader passes an HTML block
    # through the same way. Their lines are blank-line separated, which
    # ends the HTML block, so the Markdown between them still renders.
    assert hugo.figure_shortcodes(shell) == shell
    assert pelican.figure_directives(shell) == shell


def test_hugo_site_config_and_front_matter(project, capsys):
    site_asset(project, "logo.png").write_bytes(b"IMG")
    write_site(project, 
        {"title": "Example Blog", "favicon": "missing.ico",
         "hugo": {"avatar": "logo.png", "params": {"motto": "hello"}}})
    site = build(hugo, project)
    config = hugo_config(site)
    assert "theme = " not in config             # always the built-in theme
    assert 'motto = "hello"' in config          # user params merge last
    assert 'avatar = "img/avatar.png"' in config
    assert (site / "static/img/avatar.png").read_bytes() == b"IMG"
    # an asset site.toml names but the archive lacks is skipped, noted
    assert "favicon" not in hugo_params(site)
    assert "favicon not found, skipped" in capsys.readouterr().err
    assert (site / "layouts/baseof.html").exists()
    assert (site / "content/search.md").exists()
    assert (site / "content/archives.md").exists()
    front = post_front(site, "second-post")
    # the baked card cover doubles as og:image; junk bytes defeat Pillow
    # and are copied in unchanged
    assert front["cover"] == "images/cover.jpg"
    assert "images" not in front
    assert (site / "content/posts/2021/second-post/images/cover.jpg"
            ).read_bytes() == b"PNG"
    assert front["authors"] == ["ada-lovelace"]
    assert "author" not in front                # the taxonomy is the byline


def test_a_site_keeps_its_data_apart_from_its_machinery(project):
    """Each built site is meant to be checked in and carried on as a
    repository of its own, so what it says about itself is one
    hand-editable file and the generated machinery beside it holds none
    of that: the hugo site splits its config directory into params.toml
    and hugo.toml, the pelican site into site.toml and pelicanconf.py.
    Editing the data file alone, without the exporter, must be enough
    to rename a site."""
    cfg = read_site(project)
    cfg["twitter"] = "@example"
    write_site(project, cfg)
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)

    # hugo: one place to look for the config, and the data in the file
    # whose name makes its keys [params] rather than under a header
    assert not (hugo_site / "hugo.toml").exists()
    machinery = (hugo_site / "config/_default/hugo.toml").read_text()
    params = (hugo_site / "config/_default/params.toml").read_text()
    assert "[taxonomies]" in machinery and "[params]" not in params
    assert 'twitter = "@example"' in params and "twitter" not in machinery
    assert 'baseURL = "https://blog.example.org/"' in machinery

    # pelican: the config is the same bytes for every archive, and says
    # nothing about this site
    config = (pelican_site / "pelicanconf.py").read_text()
    assert sites.template_text("pelican/pelicanconf.py") in config
    assert "Example Blog" not in config and "@example" not in config
    data = pelican_data(pelican_site)
    assert data["title"] == "Example Blog" and data["twitter"] == "@example"

    # and a hand edit to that file reaches the settings on its own,
    # with no exporter run in between
    data["title"] = "Renamed By Hand"
    (pelican_site / "site.toml").write_text(toml_document(data))
    assert config_namespace(pelican_site)["SITENAME"] == "Renamed By Hand"


def test_each_site_carries_what_makes_it_a_repository(project):
    """A built site is meant to be checked in and carried on, so each
    one ships the two files that make a directory a repository rather
    than a build output: a README naming what to edit and how to build,
    and a .gitignore that keeps what the generator builds out of it --
    the same directories the exporter itself preserves across a
    rebuild. Neither is published as a page: both sit outside the
    content the generator reads."""
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)

    ignored = (pelican_site / ".gitignore").read_text()
    assert "/output/" in ignored and "__pycache__/" in ignored
    readme = (pelican_site / "README.md").read_text()
    for named in ("site.toml", "data/tags.yaml", "pelicanconf.py",
                  "content/posts/", "pelican -l"):
        assert named in readme, named
    assert not (pelican_site / "content" / "README.md").exists()

    ignored = (hugo_site / ".gitignore").read_text()
    assert "/public/" in ignored and "/resources/" in ignored
    readme = (hugo_site / "README.md").read_text()
    for named in ("config/_default/params.toml", "config/_default/hugo.toml",
                  "data/tags.yaml", "content/posts/", "hugo server"):
        assert named in readme, named
    assert not (hugo_site / "content" / "README.md").exists()


def test_cover_prefers_stills_falls_back_to_gifs_skips_huge(tmp_path):
    import struct
    images = tmp_path / "images"
    images.mkdir()
    png = lambda w, h: (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                        + struct.pack(">II", w, h) + b"\x00" * 8)
    gif = lambda w, h: b"GIF89a" + struct.pack("<HH", w, h)
    (images / "big.png").write_bytes(png(7532, 3464))
    (images / "small.png").write_bytes(png(800, 600))
    (images / "anim.gif").write_bytes(gif(400, 300))
    (images / "huge.gif").write_bytes(gif(5000, 4000))
    assert sites.image_size(images / "big.png") == (7532, 3464)
    assert sites.image_size(images / "anim.gif") == (400, 300)
    # a still anywhere in the post beats a gif ahead of it
    post = {"images": ["images/anim.gif", "images/big.png", "images/small.png"]}
    assert sites.pick_cover(post, tmp_path) == "images/small.png"
    # gif-only posts get their first sane-size gif (its first frame bakes)
    assert sites.pick_cover({"images": ["images/anim.gif"]}, tmp_path) == "images/anim.gif"
    assert sites.pick_cover({"images": ["images/huge.gif", "images/anim.gif"]},
                            tmp_path) == "images/anim.gif"
    assert sites.pick_cover({"images": ["images/huge.gif"]}, tmp_path) is None
    assert sites.pick_cover({"images": ["images/missing.png"]}, tmp_path) is None


def test_cover_bakes_first_gif_frame(tmp_path):
    from PIL import Image
    frames = [Image.new("RGB", (400, 225), c) for c in ("red", "blue")]
    src = tmp_path / "anim.gif"
    frames[0].save(src, save_all=True, append_images=frames[1:], duration=100)
    dst = tmp_path / "cover.jpg"
    assert sites.make_cover_thumbnail(src, dst)
    with Image.open(dst) as im:
        assert im.format == "JPEG" and im.size == sites.COVER_SIZE
        assert im.getpixel((320, 180))[0] > 200        # frame one, red


def test_cover_skips_svgs_and_untyped_images(tmp_path):
    """The baked cover is served as cover.jpg and Hugo's card template
    rasterizes it, so an svg badge as the cover aborts the whole hugo
    build ("image: unknown format"): only raster formats qualify. A
    .bin (bytes convert could not type) is no cover either."""
    images = tmp_path / "images"
    images.mkdir()
    (images / "badge.svg").write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
    (images / "blob.bin").write_bytes(b"?")
    (images / "photo.JPG").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16)
    post = {"images": ["images/badge.svg", "images/blob.bin", "images/photo.JPG"]}
    assert sites.pick_cover(post, tmp_path) == "images/photo.JPG"
    assert sites.pick_cover({"images": post["images"][:-1]}, tmp_path) is None


def test_cover_thumbnails_crop_or_letterbox(tmp_path):
    from PIL import Image

    from medium_archive import sites

    def thumb(im, name):
        src, dst = tmp_path / name, tmp_path / (name + ".jpg")
        im.save(src)
        assert sites.make_cover_thumbnail(src, dst)
        out = Image.open(dst)
        assert out.size == (640, 360)
        return out

    # near-16:9: center-cropped, filling the frame edge to edge
    out = thumb(Image.new("RGB", (800, 450), (160, 20, 20)), "photo.png")
    assert out.getpixel((3, 3))[0] > 100

    # a wide wordmark on white keeps its full width, letterboxed on the
    # border's color instead of cropped
    logo = Image.new("RGB", (1400, 200), "white")
    logo.paste(Image.new("RGB", (1360, 160), "black"), (20, 20))
    out = thumb(logo, "wordmark.png")
    assert all(c > 200 for c in out.getpixel((320, 10)))    # white band
    assert all(c < 60 for c in out.getpixel((320, 180)))    # content kept...
    assert all(c < 60 for c in out.getpixel((15, 180)))     # ...edge to edge

    # a small square logo is centered at no more than 2x, not blown up
    # to fill; transparency composites onto white, not black
    sq = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    sq.paste(Image.new("RGBA", (60, 60, ), (0, 0, 0, 255)), (20, 20))
    out = thumb(sq, "logo.png")
    assert all(c < 60 for c in out.getpixel((320, 180)))    # 2x: 120px wide
    assert all(c > 200 for c in out.getpixel((320, 70)))    # its own margin
    assert all(c > 200 for c in out.getpixel((100, 180)))   # canvas margin

    # no uniform border to extend: the frame fills with a blurred
    # cover-crop of the image itself, never flat bars
    import os
    noisy = Image.frombytes("RGB", (300, 900), os.urandom(300 * 900 * 3))
    out = thumb(noisy, "tall.png")
    corners = {out.getpixel(p) for p in ((3, 3), (636, 3), (3, 356))}
    assert len(corners) > 1


def test_tag_display_names_reach_both_sites(project):
    """tags.json's display map names each tag on the rendered site while
    the tag itself -- front matter, tag URL -- stays a slug."""
    (archive_dir(project) / "tags.json").write_text(json.dumps(
        {"display": {"example": "Example Tag"}}))
    hugo_site = build(hugo, project)
    front = post_front(hugo_site, "second-post")
    assert front["tags"] == ["example"]           # the tag is still a slug
    # one data file names every tag; the content adapter beside the
    # posts turns it into the term pages (kind term, path = slug)
    names = hugo_site / "data/tags.yaml"
    assert yaml.safe_load(names.read_text()) == {"example": "Example Tag"}
    adapter = (hugo_site / "content/tags/_content.gotmpl").read_text()
    assert "hugo.Data.tags" in adapter and '"kind" "term"' in adapter
    assert not (hugo_site / "content/tags/example").exists()

    pelican_site = build(pelican, project)
    # the tag is still a slug
    assert post_front(pelican_site, "second-post")["tags"] == ["example"]
    # the same data file, hand-editable beside the generated config,
    # which reads it into TAG_DISPLAY at build time
    assert yaml.safe_load((pelican_site / "data/tags.yaml").read_text()) \
        == {"example": "Example Tag"}
    config = (pelican_site / "pelicanconf.py").read_text()
    assert 'TAG_DISPLAY = _data("tags.yaml")' in config
    assert config_namespace(pelican_site)["TAG_DISPLAY"] \
        == {"example": "Example Tag"}
    assert "_name_tags" in config          # names the Tag objects, so the
    assert "article_generator_finalized" in config   # feeds get it too


def test_both_sites_write_the_same_hand_editable_data_files(project):
    """The maps that are neither a post nor site.toml -- tag names,
    author names, byline profiles -- are data files beside each site's
    config, the same two files with the same contents in both sites:
    one per taxonomy, so everything shown for a byline is that byline's
    one entry. Hugo reads them through hugo.Data; the pelican config
    reads them at config time, so editing one by hand renames a tag or
    corrects a profile in the built site without re-running the
    exporter."""
    (archive_dir(project) / "tags.json").write_text(json.dumps(
        {"display": {"example": "Example Tag"}}))
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    for name in ("tags.yaml", "authors.yaml"):
        got = (pelican_site / "data" / name).read_bytes()
        assert got == (hugo_site / "data" / name).read_bytes(), name
        assert got.endswith(b"\n")           # a diffable, editable file
        # each file opens with what its entries mean, for the hand that
        # edits it -- and is still the map it holds once parsed
        assert got.startswith(b"#")
    assert not list((pelican_site / "data").glob("*.json"))
    assert yaml.safe_load((pelican_site / "data/tags.yaml").read_text()) \
        == {"example": "Example Tag"}
    assert yaml.safe_load((pelican_site / "data/authors.yaml").read_text()) \
        == {"ada-lovelace": {"name": "Ada Lovelace",
                             "url": "https://medium.com/@ada"}}
    # nothing of the two is baked into the generated config
    config = (pelican_site / "pelicanconf.py").read_text()
    assert "Example Tag" not in config and "medium.com/@ada" not in config

    # a hand edit reaches the config's settings, with no rebuild
    (pelican_site / "data/tags.yaml").write_text(
        "example: Renamed By Hand\n")
    assert config_namespace(pelican_site)["TAG_DISPLAY"] \
        == {"example": "Renamed By Hand"}
    (pelican_site / "data/authors.yaml").write_text(
        "ada-lovelace:\n  name: Renamed By Hand\n  url: https://ada.example/\n"
        "grace-hopper:\n  name: Grace Hopper\n")
    namespace = config_namespace(pelican_site)
    assert namespace["AUTHOR_DISPLAY"] == {"ada-lovelace": "Renamed By Hand",
                                           "grace-hopper": "Grace Hopper"}
    # an author with no profile is simply an entry without one
    assert namespace["AUTHOR_LINKS"] == {"ada-lovelace": "https://ada.example/"}
    # and a deleted file leaves a site that still builds, tags and
    # authors showing as their slugs
    for name in ("tags.yaml", "authors.yaml"):
        (pelican_site / "data" / name).unlink()
    namespace = config_namespace(pelican_site)
    assert namespace["TAG_DISPLAY"] == namespace["AUTHOR_DISPLAY"] == {}
    assert namespace["AUTHOR_LINKS"] == {}


def test_tags_display_as_slugs_with_spaces_by_default(project):
    """No tags.json at all: a tag still shows with its hyphens as spaces."""
    manifest = json.loads(manifest_json(project).read_text())
    for post in manifest.values():
        post["tags"] = ["open-science"]
    manifest_json(project).write_text(json.dumps(manifest))
    site = build(hugo, project)
    names = yaml.safe_load((site / "data/tags.yaml").read_text())
    assert names == {"open-science": "open science"}


def test_feed_links_carry_the_rss_mark(project):
    """The header's feed link and the per-term ones on a tag's and an
    author's page are the shared RSS mark, pointing at that term's own
    feed."""
    hugo_site = build(hugo, project)
    nav = (hugo_site / "layouts/baseof.html").read_text()
    assert '<a class="feed-link" href="{{ "index.xml" | relURL }}"' in nav
    assert 'aria-label="RSS"' in nav and "feed-icon" in nav
    term = (hugo_site / "layouts/section.html").read_text()
    assert '.OutputFormats.Get "rss"' in term    # only where a feed exists
    assert 'aria-label="RSS feed for {{ $.Title }}"' in term

    pelican_site = build(pelican, project)
    # the tag and author pages are one template, over the feed setting
    # the page it was reached through named
    term = (pelican_site / "theme/templates/term.html").read_text()
    assert "term_feed.format(slug=term.slug)" in term
    assert 'aria-label="RSS feed for {{ term }}"' in term
    assert "feed-icon" in term
    for page, setting, var in (("tag.html", "TAG_FEED_ATOM", "tag"),
                               ("author.html", "AUTHOR_FEED_ATOM", "author")):
        text = (pelican_site / "theme/templates" / page).read_text()
        assert f"{{% set term, term_feed = {var}, {setting} %}}" in text
        assert '{% include "term.html" %}' in text
    # the head declares the term's own feed beside the site-wide one
    base = (pelican_site / "theme/templates/base.html").read_text()
    assert "TAG_FEED_ATOM.format(slug=tag.slug)" in base
    assert "AUTHOR_FEED_ATOM.format(slug=author.slug)" in base
    # ... and hugo's head has the same pair, each titled the way that
    # feed titles itself, so a reader files it under the name it shows
    assert 'site.Home.OutputFormats.Get "rss"' in nav
    assert '{{ $.Title }} · {{ site.Title }}' in nav
    css = (pelican_site / "theme/static/css/style.css").read_text()
    assert ".feed-icon" in css and ".page-title .feed-link" in css


class _FakeTag:
    """pelican.urlwrappers.Tag's naming semantics: hash and equality are
    the slug's, and setting a name re-slugifies unless a slug was set
    explicitly first."""

    def __init__(self, name):
        self._name, self._slug, self._from_name = name, None, True

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        self._name = value
        if self._from_name:
            self._slug = None

    @property
    def slug(self):
        if self._slug is None:
            self._slug = self._name.lower().replace(" ", "-")
        return self._slug

    @slug.setter
    def slug(self, value):
        self._from_name, self._slug = False, value

    def __hash__(self):
        return hash(self.slug)

    def __eq__(self, other):
        return self.slug == other.slug

    def __str__(self):
        return self.name


def test_every_article_gets_the_named_tag_object(project):
    """Pelican builds a Tag object per article and keys generator.tags on
    the slug, so it holds one object per tag while every other article
    keeps its own. Naming only the dict's keys named a tag on its own
    page and on one article's card, and left it a slug on the rest."""
    (archive_dir(project) / "tags.json").write_text(json.dumps(
        {"display": {"example": "Example Tag"}}))
    site = build(pelican, project)
    namespace = config_namespace(site)

    # three articles, each with its own object for the one tag
    articles = [SimpleNamespace(tags=[_FakeTag("example")]) for _ in range(3)]
    generator = SimpleNamespace(tags={articles[0].tags[0]: articles},
                                articles=articles, translations=[],
                                hidden_articles=[], hidden_translations=[],
                                drafts=[], drafts_translations=[])
    namespace["_name_tags"](generator)

    assert [str(a.tags[0]) for a in articles] == ["Example Tag"] * 3
    # one object per slug now, and the slug is untouched
    assert len({id(a.tags[0]) for a in articles}) == 1
    assert articles[0].tags[0].slug == "example"


def test_pelican_site(project):
    site_asset(project, "logo.png").write_bytes(b"IMG")
    site_asset(project, "icon.svg").write_bytes(b"SVG")
    cfg = read_site(project)
    cfg["avatar"] = "logo.png"
    cfg["favicon"] = "icon.svg"
    write_site(project, cfg)
    site = build(pelican, project)
    text = (site / "content/posts/2021/second-post/index.md").read_text()
    front = post_front(site, "second-post")
    assert front["title"] == "Second Post"
    assert front["date"] == "2021-03-01 10:00"
    assert front["authors"] == ["ada-lovelace"]
    assert front["tags"] == ["example"]
    assert front["cover"].startswith("images/")   # summary-card cover
    # colocated images become {attach} links -- but not inside fences
    assert "]({attach}images/001-pic.png)" in text
    assert "![fenced](images/lit.png)" in text
    config = (site / "pelicanconf.py").read_text()
    # the year in the address is the post's own date, not the directory
    # it sits in, and /posts/<year>/ answers with that year's posts
    assert 'ARTICLE_URL = "posts/{date:%Y}/{slug}/"' in config
    assert 'YEAR_ARCHIVE_SAVE_AS = "posts/{date:%Y}/index.html"' in config
    assert "FEED_MAX_ITEMS = 20" in config
    assert 'THEME = "theme"' in config
    assert '"search.html": "search/index.html"' in config
    # the CommonMark reader replaces pelican's python-markdown one, and
    # takes its settings with it
    assert "_CommonMarkReader" in config and "MARKDOWN = {" not in config
    # what the site says about itself is site.toml beside the config,
    # which holds none of it: the config reads that file instead
    data = pelican_data(site)
    assert data["title"] == "Example Blog"
    assert "Example Blog" not in config
    assert data["avatar"] == "theme/img/avatar.png"
    assert (site / "theme/static/img/avatar.png").read_bytes() == b"IMG"
    assert data["favicon"] == "theme/favicon.svg"
    assert (site / "theme/static/favicon.svg").read_bytes() == b"SVG"
    assert 'rel="icon"' in (site / "theme/templates/base.html").read_text()
    for tpl in ("base", "index", "article", "term", "tag", "author",
                "terms", "tags", "authors", "archives", "search", "macros",
                "pagination"):
        assert (site / f"theme/templates/{tpl}.html").exists(), tpl
    assert "card-grid" in (site / "theme/static/css/style.css").read_text()
    assert (site / "redirects.csv").exists()
    # the embedded plugin turns redirects.csv into redirect stubs and
    # rewrites body images into responsive webp variants
    assert "PLUGINS = [_CommonMarkPlugin, _SitePlugins]" in config
    assert "signals.finalized" in config and "redirects.csv" in config
    assert "_optimize_article_images" in config
    assert "VARIANT_WIDTHS = (480, 736, 1104)" in config


def _fake_article(source_path, slug):
    """An article as _check_slugs reads one: pelican computes save_as and
    url from ARTICLE_SAVE_AS/ARTICLE_URL and the slug."""
    return SimpleNamespace(slug=slug, relative_source_path=source_path,
                           save_as=f"posts/{slug}/index.html",
                           url=f"posts/{slug}/")


def test_posts_are_filed_and_served_under_their_publish_year(project):
    """The tree is content/posts/<year>/<slug>/ on both engines, the
    year being the one in the post's date. Twelve directories of about
    thirty beat one of several hundred, and an address dates a post
    before a reader opens it."""
    for module in (hugo, pelican):
        site = build(module, project)
        root = site / "content" / "posts"
        assert sorted(d.name for d in root.iterdir() if d.is_dir()) == \
            ["2020", "2021"]                          # the fixture's two years
        assert (root / "2020" / "first-post" / "index.md").exists()
        assert (root / "2021" / "second-post" / "index.md").exists()
        # a link from one post to another carries the year too
        assert "](/posts/2021/second-post/)" in \
            (root / "2020" / "first-post" / "index.md").read_text()
        # and so does every row of the redirect map
        rows = (site / "redirects.csv").read_text().splitlines()[1:]
        assert rows and all(re.match(r"^[^,]+,/posts/\d{4}/[^/,]+/,", r)
                            for r in rows), rows


def test_a_year_page_answers_a_trimmed_post_url(project):
    """/posts/<year>/ is an address a reader reaches by trimming a
    post's, so both engines answer it with that year's posts rather
    than a 404 -- pelican through a period archive, hugo through the
    section each year directory already is."""
    pelican_site = build(pelican, project)
    config = (pelican_site / "pelicanconf.py").read_text()
    assert 'YEAR_ARCHIVE_URL = "posts/{date:%Y}/"' in config
    assert (pelican_site / "theme/templates/period_archives.html").exists()

    hugo_site = build(hugo, project)
    for year in ("2020", "2021"):
        index = hugo_site / "content" / "posts" / year / "_index.md"
        assert page_front(index)["title"] == year
    assert (hugo_site / "layouts/posts/section.html").exists()
    # the year in a hugo address is the post's date, not its directory
    assert 'posts = "/posts/:year/:slug/"' in hugo_config(hugo_site)


def test_a_post_directory_names_the_page_it_serves(project):
    """The config reads a post's directory name as its slug, so a post
    written by hand needs no slug of its own; a post that carries one is
    served under it instead, which the build reports rather than
    refuses. Two posts writing the same page is the error."""
    site = build(pelican, project)
    namespace = config_namespace(site)

    # every exported post's directory name is the slug its front matter
    # carries and its parent is the year it was published in, so the two
    # routes agree on every page of the site
    posts = sorted(d for year in (site / "content/posts").iterdir()
                   if year.is_dir() for d in year.iterdir() if d.is_dir())
    assert posts
    for post in posts:
        match = re.match(namespace["PATH_METADATA"],
                         f"posts/{post.parent.name}/{post.name}/index.md")
        assert match and match.group("slug") == post.name
        assert match.group("diryear") == post.parent.name

    check = namespace["_check_slugs"]
    check(SimpleNamespace(articles=[_fake_article("posts/a-post/index.md",
                                                  "a-post")],
                          translations=[]))

    # a directory renamed under a post that keeps its URL: reported,
    # and the build goes on
    check(SimpleNamespace(
        articles=[_fake_article("posts/renamed/index.md", "a-post")],
        translations=[]))

    # two posts writing one page: named, both of them, and the build stops
    with pytest.raises(SystemExit) as stop:
        check(SimpleNamespace(
            articles=[_fake_article("posts/a-post/index.md", "a-post"),
                      _fake_article("posts/another-dir/index.md", "a-post")],
            translations=[]))
    assert "posts/a-post/index.html" in str(stop.value)
    assert "posts/a-post/index.md" in str(stop.value)
    assert "posts/another-dir/index.md" in str(stop.value)


def test_theme_picker_and_dark_scheme(project):
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    css = (hugo_site / "static/css/style.css").read_text()
    # dark palette under both routes: an explicit picker choice pins
    # data-theme; with none stored, the system scheme decides
    assert ':root[data-theme="dark"]' in css
    assert "@media (prefers-color-scheme: dark)" in css
    assert ':root:not([data-theme="light"])' in css
    assert ".theme-picker" in css
    # the link default is "ink-accent", so it is the state with no
    # data-link attribute; every other choice pins one
    assert ":root:not([data-link]) a:hover" in css
    assert ':root[data-link="ink-accent"]' not in css
    # the two blues hover the way the default does: an accent rule
    assert ':root[data-link="petrol-aaa"] a:hover' in css
    assert ':root[data-link="link-blue"] a:hover' in css
    # and every accent hover is drawn at the heavier of 2px and the
    # rule the link already carries, so it never thins that rule
    assert css.count("max(2px, var(--rule-w))") == 2
    assert "--rule-w: .1em;" in css
    # every font choice is set at one apparent size, so the picker
    # compares faces rather than sizes: each role states the x-height
    # of the default's own face, so the default is the choice nothing
    # moves under -- .486 (Source Sans 3 and Source Code Pro) for the
    # chrome and the code, .475 (Source Serif 4) for the article
    assert "font: 1rem/1.65 var(--body-font); font-size-adjust: .486; }" in css
    assert "line-height: 1.6; font-size-adjust: .475;" in css
    assert ".post figcaption { font-size-adjust: .486; }" in css
    # the shorthand resets font-size-adjust, so a later one would drop
    # a rule's text back to its own x-height: the only shorthands in
    # the sheet are body's, which restates the property after it, and
    # the two that inherit every longhand wholesale
    rules = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    shorthands = re.findall(r"(?<![-\w])font:\s*([^;]+);([^}]*)}", rules)
    assert len(shorthands) == 4
    for face, rest in shorthands:
        assert face == "inherit" or "font-size-adjust" in rest, face
    assert (pelican_site / "theme/static/css/style.css").read_text() == css
    for base in (hugo_site / "layouts/baseof.html",
                 pelican_site / "theme/templates/base.html"):
        text = base.read_text()
        for choice in ("light", "system", "dark"):
            assert f'data-set-theme="{choice}"' in text, base
        for choice in ("sans", "system-ui", "inter", "source-sans",
                       "source-serif", "atkinson", "ibm-plex-sans",
                       "ibm-plex-serif", "literata", "merriweather"):
            assert f'<option value="{choice}"' in text, base
        for choice in ("ink", "ink-accent", "petrol-aaa", "link-blue",
                       "browser"):
            assert f'<option value="{choice}"' in text, base
        # the link picker rides above the font one, so it is spliced in
        # first (the stack grows upwards from the corner)
        assert text.index("link-picker") < text.index("font-picker"), base
        # every family the picker offers beyond the launch stack and the
        # platform's own is a webfont: unlinked, those choices degrade to
        # their fallbacks silently, looking like a styling bug rather than
        # a missing file
        for family in ("Atkinson+Hyperlegible+Next", "Atkinson+Hyperlegible+Mono",
                       "IBM+Plex+Mono", "IBM+Plex+Sans", "IBM+Plex+Serif",
                       "Inter", "Literata", "Merriweather", "Merriweather+Sans",
                       "Source+Serif+4", "Source+Sans+3",
                       "Source+Code+Pro"):
            assert f"family={family}" in text, (base, family)
        # the stored choices apply before the stylesheet loads, so a
        # page cannot flash the wrong scheme or font
        assert text.index("localStorage.getItem") < text.index("stylesheet")
        assert text.index('localStorage.getItem("font")') < text.index("stylesheet")
        assert text.index('localStorage.getItem("link")') < text.index("stylesheet")
        # the default is stored as no choice at all, so the picker
        # falls back to it and the init script does not restore it
        assert 'apply(known ? stored : "ink-accent");' in text, base
        assert 'link === "ink-accent"' not in text, base
        assert 'link === "ink"' in text, base
    # redirect stubs load no stylesheet, so they must paint the palette
    # themselves -- following a redirect must not flash white in dark mode
    for stub_source in ((hugo_site / "layouts/alias.html").read_text(),
                        (pelican_site / "pelicanconf.py").read_text()):
        assert "prefers-color-scheme: dark" in stub_source
        assert 'localStorage.getItem("theme")' in stub_source
    # the snippets embed verbatim, so they must carry no template syntax
    # the other engine would mangle
    for name in ("theme-init", "theme-picker", "font-init", "font-picker",
                 "link-init", "link-picker", "term-sort", "announcement",
                 "nav-current", "image-zoom", "code-copy",
                 "clip-motion", "heading-anchor", "feed-icon", "share-icons",
                 "newsletter", "plausible"):
        snippet = sites.template_text(f"shared/{name}.html")
        assert "{{" not in snippet and "{%" not in snippet
    # without an avatar or announcement the site's data leaves the key
    # out -- written commented out, beside an example of it set -- and
    # the config reads the absence back as None
    data = pelican_data(pelican_site)
    settings = config_namespace(pelican_site)
    raw = (pelican_site / "site.toml").read_text()
    for key in ("avatar", "favicon", "announcement"):
        assert key not in data, key
        assert settings[key.upper()] is None, key
        assert f"# {key} = " in raw, key


def test_announcement_banner(project):
    banner_url = "https://jupyter.org/assets/banner.html"
    cfg = read_site(project)
    cfg["announcement"] = banner_url
    write_site(project, cfg)
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    assert f'announcement = "{banner_url}"' in hugo_config(hugo_site)
    assert pelican_data(pelican_site)["announcement"] == banner_url
    for base in (hugo_site / "layouts/baseof.html",
                 pelican_site / "theme/templates/base.html"):
        text = base.read_text()
        # the banner div sits above the header, emitted only when an
        # announcement is configured; a URL source is fetched
        # client-side, anything else is the banner HTML itself
        assert 'class="announcement"' in text and "data-source" in text, base
        assert text.index('class="announcement"') < text.index("site-header"), base
        assert "fetch(source)" in text, base
        # dismissal is remembered keyed by the banner's content, so a
        # changed announcement clears it and shows again
        assert 'localStorage.setItem("announcement-dismissed", html)' in text, base
        assert 'localStorage.getItem("announcement-dismissed") === html' in text, base
        # the last fetch's content is cached and rendered synchronously,
        # so navigating the site doesn't shift the layout when the
        # banner arrives
        assert 'localStorage.setItem("announcement-cache"' in text, base
        assert text.index("announcement-cache") < text.index("fetch(source)"), base
    css = (hugo_site / "static/css/style.css").read_text()
    assert ".announcement" in css and ".announcement-close" in css


def test_newsletter_band(project):
    # jupyter.org's signup band at the foot of every page: the heading
    # from site.toml, the HubSpot form's ids on the section the snippet
    # reads them off, and the band hidden until that form is on its way
    cfg = read_site(project)
    cfg["newsletter"] = {
        "heading": "Subscribe for updates",
        "hubspot_portal": "8112310",
        "hubspot_form": "3a79d744-5260-4a98-b069-39defccc8f42",
    }
    write_site(project, cfg)
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    hugo_cfg = hugo_config(hugo_site)
    assert "[newsletter]" in hugo_cfg
    assert 'hubspot_portal = "8112310"' in hugo_cfg
    # the region is optional: HubSpot's own default stands in
    assert 'hubspot_region = "na1"' in hugo_cfg
    band = pelican_data(pelican_site)["newsletter"]
    assert band["hubspot_form"] == "3a79d744-5260-4a98-b069-39defccc8f42"
    assert band["hubspot_region"] == "na1"
    for base in (hugo_site / "layouts/baseof.html",
                 pelican_site / "theme/templates/base.html"):
        text = base.read_text()
        assert 'class="newsletter"' in text, base
        for attr in ("data-hs-portal", "data-hs-form", "data-hs-region"):
            assert attr in text, (base, attr)
        assert 'class="newsletter-form"' in text, base
        # the band closes the page: after the article, before the
        # footer line, as it is on jupyter.org
        assert text.index("</main>") < text.index('class="newsletter"'), base
        assert text.index('class="newsletter"') < text.index("site-footer"), base
        # hidden markup, revealed only once the embed has loaded, so a
        # blocked script leaves no heading promising a form
        assert re.search(r'class="newsletter"[^>]*hidden', text), base
        assert "band.hidden = false" in text, base
        assert "js.hsforms.net" in text and "hbspt.forms.create" in text, base
        # the form renders in an iframe the page's CSS cannot reach, so
        # its look is passed to the embed -- with this site's accent on
        # the button, not the colour jupyter.org hard-codes
        assert "--accent" in text and ".hs-button" in text, base
        # and jupyter.org's layout: the fields on one row, then the
        # consent copy, then the button, each of the last two on a
        # full-width basis so the order holds at any field count
        assert '".hs-richtext { flex: 1 0 100%;' in text, base
        assert '".hs-submit { flex: 1 0 100%; }"' in text, base
        # and neither the copy nor the heading is held to a measure of
        # its own: both run the width of the band
        assert "max-width" not in text.split(".hs-richtext")[1][:200], base
    css = (hugo_site / "static/css/style.css").read_text()
    assert ".newsletter " in css and ".newsletter h2" in css
    assert css == (pelican_site / "theme/static/css/style.css").read_text()


def test_newsletter_band_absent_or_incomplete(project, capsys):
    # no "newsletter" at all: no band, and configs that are still valid
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    assert "newsletter" not in hugo_params(hugo_site)
    assert "newsletter" not in pelican_data(pelican_site)
    assert config_namespace(pelican_site)["NEWSLETTER"] is None
    # the band's markup is guarded by that setting, so an archive that
    # configures no newsletter renders no empty band
    assert ("{{ with site.Params.newsletter }}<section class=\"newsletter\""
            in (hugo_site / "layouts/baseof.html").read_text())
    assert ("{% if NEWSLETTER %}<section class=\"newsletter\""
            in (pelican_site / "theme/templates/base.html").read_text())
    # a half-filled entry is a mistake worth hearing about, not a band
    # quietly missing from the built site
    cfg = read_site(project)
    cfg["newsletter"] = {"heading": "Subscribe for updates"}
    write_site(project, cfg)
    build(hugo, project)
    err = capsys.readouterr().err
    assert "hubspot_portal" in err and "hubspot_form" in err


def test_plausible_analytics(project):
    # site.toml's "plausible" is the address of the counting script
    # Plausible names this site's. Both themes emit it async and last
    # in the head, with Plausible's own queue stub beside it, so an
    # event fired before the script lands is queued rather than lost.
    script = "https://plausible.io/js/pa-test-script.js"
    cfg = read_site(project)
    cfg["plausible"] = script
    write_site(project, cfg)
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    assert f'plausible = "{script}"' in hugo_config(hugo_site)
    assert pelican_data(pelican_site)["plausible"] == script
    assert config_namespace(pelican_site)["PLAUSIBLE"] == script
    # the address is the site's own, so the tag carrying it stays with
    # each engine; the stub beside it is the shared snippet, spliced in
    for base, src in ((hugo_site / "layouts/baseof.html", "{{ . }}"),
                      (pelican_site / "theme/templates/base.html",
                       "{{ PLAUSIBLE }}")):
        text = base.read_text()
        assert f'<script async src="{src}"></script>' in text, base
        assert "(plausible.q=plausible.q||[]).push(arguments)" in text, base
        assert "plausible.init()" in text, base
        # last in the head: nothing a page needs to render waits on it
        assert text.index("plausible.init()") < text.index("</head>"), base
        assert (text.index("css/style.css")
                < text.index('<script async src="')), base


def test_plausible_analytics_absent(project):
    # unset -- the default -- and the pages carry no analytics and no
    # third-party script at all: the key is written commented out,
    # beside an example of it set, and both themes guard the tag on it
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    assert "plausible" not in hugo_params(hugo_site)
    assert "plausible" not in pelican_data(pelican_site)
    assert config_namespace(pelican_site)["PLAUSIBLE"] is None
    assert "# plausible = " in (pelican_site / "site.toml").read_text()
    assert ('{{ with site.Params.plausible }}'
            in (hugo_site / "layouts/baseof.html").read_text())
    assert ('{% if PLAUSIBLE %}'
            in (pelican_site / "theme/templates/base.html").read_text())


def test_footer_line(project):
    # site.toml's "footer" is the line under every page, Markdown, with
    # {year} the year of the build -- jupyter.org's trademark notice,
    # which carries a link and a copyright year, is the shape of it
    cfg = read_site(project)
    cfg["footer"] = ("Trademarks are registered by "
                     "[LF Charities](https://lf-charities.org/). \u00a9 {year}")
    write_site(project, cfg)
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    assert "[LF Charities](https://lf-charities.org/)" in hugo_config(hugo_site)
    config = (pelican_site / "pelicanconf.py").read_text()
    assert "_FOOTER_MD = " in config
    # the year is substituted where the site is built, not baked into
    # the generated source, which stays the same from one build to the
    # next (and from one year to the next)
    assert '{year}' in config
    assert "_FOOTER_MD.replace(\"{year}\"" in config
    for base, render in ((hugo_site / "layouts/baseof.html",
                          'replace . "{year}"'),
                         (pelican_site / "theme/templates/base.html",
                          "FOOTER|safe")):
        text = base.read_text()
        assert render in text, base
        assert "site-footer" in text, base
    # the line is a paragraph of Markdown, so the footer spaces itself
    assert ".site-footer p { margin: 0; }" in (
        hugo_site / "static/css/style.css").read_text()


def test_footer_falls_back_to_the_description(project):
    # no "footer": the site's description, as the footer has always
    # carried, and a config that is still valid Python
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    assert "footer" not in hugo_params(hugo_site)
    assert "footer" not in pelican_data(pelican_site)
    assert config_namespace(pelican_site)["FOOTER"] is None
    assert "{{ site.Params.description }}" in (
        hugo_site / "layouts/baseof.html").read_text()
    assert "{{ SITESUBTITLE }}" in (
        pelican_site / "theme/templates/base.html").read_text()


def test_masthead_logo(project):
    # site.toml's "logo": a wordmark standing in for the site's name in
    # the header, the way jupyter.org's navbar carries its rectangle
    # logo, with "logo_dark" the same mark for the dark palette
    site_asset(project, "logo.svg").write_bytes(b"<svg/>")
    site_asset(project, "logo-dark.svg").write_bytes(b"<svg dark/>")
    cfg = read_site(project)
    cfg["logo"], cfg["logo_dark"] = "logo.svg", "logo-dark.svg"
    write_site(project, cfg)
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    assert 'logo = "img/logo.svg"' in hugo_config(hugo_site)
    assert 'logo_dark = "img/logo-dark.svg"' in hugo_config(hugo_site)
    assert (hugo_site / "static/img/logo.svg").read_bytes() == b"<svg/>"
    assert (pelican_site / "theme/static/img/logo-dark.svg").read_bytes() == b"<svg dark/>"
    data = pelican_data(pelican_site)
    assert data["logo"] == "theme/img/logo.svg"
    assert data["logo_dark"] == "theme/img/logo-dark.svg"
    for base in (hugo_site / "layouts/baseof.html",
                 pelican_site / "theme/templates/base.html"):
        text = base.read_text()
        assert "site-logo-light" in text and "site-logo-dark" in text, base
        # the pair carries no alt text of its own -- either image would
        # name the link twice over -- so the link is named once, on the
        # anchor, and reads the same whichever one is showing
        assert "aria-label" in text, base
    # the palettes pick between the two, like every other value that
    # differs between them
    css = (hugo_site / "static/css/style.css").read_text()
    assert "--logo-light: block" in css and "--logo-dark: block" in css
    assert "display: var(--logo-light)" in css


def test_masthead_logo_link(project):
    # site.toml's "logo_link": a mark that stands for something larger
    # than the blog (the Jupyter mark over a Jupyter blog) links there
    # instead of to the site's home, and the link reads under that
    # address's host rather than the site's own title
    site_asset(project, "logo.svg").write_bytes(b"<svg/>")
    cfg = read_site(project)
    cfg["logo"], cfg["logo_link"] = "logo.svg", "https://jupyter.org"
    write_site(project, cfg)
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    toml = hugo_config(hugo_site)
    assert "[logo_link]" in toml
    assert 'url = "https://jupyter.org"' in toml
    assert 'label = "jupyter.org"' in toml
    assert pelican_data(pelican_site)["logo_link"] == {
        "url": "https://jupyter.org", "label": "jupyter.org"}
    # the nav still carries the way home, so the site is not left
    # without a link to its own landing page
    for base, home in ((hugo_site / "layouts/baseof.html",
                        '<a href="{{ site.Home.RelPermalink }}">Blog</a>'),
                       (pelican_site / "theme/templates/base.html",
                        '<a href="{{ SITEURL }}/">Blog</a>')):
        assert home in base.read_text(), base


def test_masthead_link_defaults_home(project):
    # no "logo_link": the masthead links to the site's own home, named
    # by the site's title, as it always has
    site_asset(project, "logo.svg").write_bytes(b"<svg/>")
    cfg = read_site(project)
    cfg["logo"] = "logo.svg"
    write_site(project, cfg)
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    assert "logo_link" not in hugo_params(hugo_site)
    assert "logo_link" not in pelican_data(pelican_site)
    # and with no logo to carry it the link is not read at all: the
    # masthead is then the site's own name, which cannot lead off-site
    del cfg["logo"]
    cfg["logo_link"] = "https://jupyter.org"
    write_site(project, cfg)
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    assert "logo_link" not in hugo_params(hugo_site)
    assert "logo_link" not in pelican_data(pelican_site)


def test_nav_current_highlight(project):
    # the nav link whose path prefixes the current page's gets
    # aria-current, which the stylesheet paints in the accent
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    for base in (hugo_site / "layouts/baseof.html",
                 pelican_site / "theme/templates/base.html"):
        text = base.read_text()
        assert 'setAttribute("aria-current", "page")' in text, base
        # the script follows the nav it marks
        assert text.index("</header>") < text.index("aria-current"), base
    assert 'a[aria-current="page"]' in (hugo_site / "static/css/style.css").read_text()


def test_nav_wraps_without_overlapping_itself(project):
    # A phone-width screen wraps the nav, and each item is pulled
    # .9375rem down the header to land its tab bar on the header's
    # border. Laid out as inline-blocks the two together overlapped:
    # a line box is sized to the pulled-in margin box, so a wrapped
    # row was laid that .9375rem short and the row above printed its
    # accent bar through the words below it. The rows are flex rows
    # now, and the row-gap gives that space back with room to spare.
    css = (build(hugo, project) / "static/css/style.css").read_text()
    nav = css[css.index(".site-header nav {"):]
    nav = nav[:nav.index("}")]
    assert "flex-wrap: wrap" in nav and "row-gap: 1.25rem" in nav
    item = css[css.index(".site-header nav a {"):css.index(".site-header nav a:hover")]
    assert "margin-bottom: -.9375rem" in item
    # the row's own gaps space the items, so no item carries a margin
    # that would indent the start of a wrapped row
    assert "margin-left" not in item


def test_term_sort_control(project):
    # the tag/author chip indexes carry the name/count sort control,
    # placed above the chip list it reorders
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    for page in (hugo_site / "layouts/taxonomy.html",
                 pelican_site / "theme/templates/terms.html"):
        text = page.read_text()
        for order in ("name", "count"):
            assert f'data-sort="{order}"' in text, page
        assert text.index("term-sort") < text.index("term-list"), page
    for page, terms in (("tags.html", "tags"), ("authors.html", "authors")):
        text = (pelican_site / "theme/templates" / page).read_text()
        assert f"{{% set terms, terms_title = {terms}," in text
        assert '{% include "terms.html" %}' in text
    css = (hugo_site / "static/css/style.css").read_text()
    assert ".term-sort" in css
    assert css == (pelican_site / "theme/static/css/style.css").read_text()


SITE_URL = "https://blog.example.org"


class _Term(str):
    """A pelican Tag or Author as the theme sees one: it renders as its
    name and carries the slug and URL the theme builds links from."""

    def __new__(cls, name, kind):
        term = super().__new__(cls, name)
        term.name = name
        term.slug = name.lower().replace(" ", "-")
        term.url = f"{kind}/{term.slug}/"
        return term


def render_pelican_page(site, template, **context):
    """One page of the generated theme, rendered with the jinja settings
    the generated config gives pelican, over the values that page reads."""
    env = Environment(loader=FileSystemLoader(site / "theme/templates"),
                      autoescape=True, trim_blocks=True, lstrip_blocks=True)
    article = SimpleNamespace(
        title="First Post", url="posts/first-post/", cover=None,
        summary="Hello.", locale_date="2020-01-05",
        tags=[_Term("example", "tags")],
        authors=[_Term("Ada Lovelace", "authors")])
    values = dict(
        SITEURL=SITE_URL, SITENAME="Example Blog", SITESUBTITLE="An example.",
        DEFAULT_LANG="en", THEME_STATIC_DIR="theme", AUTHOR_LINKS={},
        FEED_ALL_ATOM="feeds/all.atom.xml",
        TAG_FEED_ATOM="feeds/tag-{slug}.atom.xml",
        AUTHOR_FEED_ATOM="feeds/author-{slug}.atom.xml",
        TAGS_URL="tags/", AUTHORS_URL="authors/",
        articles_page=SimpleNamespace(object_list=[article], number=1,
                                      has_other_pages=lambda: False))
    values.update(context)
    return env.get_template(template).render(**values)


def test_taxonomy_pages_render_through_the_shared_templates(project):
    """A tag page and an author page are one template (term.html) handed
    the page's own term, and their chip indexes another (terms.html):
    the two pages of each pair differed in nothing but the variable's
    name, so a change to one had to be made twice. Rendered here because
    the delegation is only right or wrong at render time, and pelican
    itself is not a dependency of these tests."""
    site = build(pelican, project)
    tag = _Term("example", "tags")
    author = _Term("Ada Lovelace", "authors")

    for page, var, term, feed, index in (
            ("tag.html", "tag", tag, "feeds/tag-example.atom.xml", "Tags"),
            ("author.html", "author", author,
             "feeds/author-ada-lovelace.atom.xml", "Authors")):
        html = render_pelican_page(site, page, **{var: term},
                                   output_file=term.url + "index.html")
        assert f"<title>{term} \u00b7 Example Blog</title>" in html
        # the term's own heading, with the feed link beside it, and that
        # same feed declared in the head
        assert (f'<h1 class="page-title">{term}<a class="feed-link" '
                f'href="{SITE_URL}/{feed}"') in html
        assert (f'<link rel="alternate" type="application/atom+xml" '
                f'href="{SITE_URL}/{feed}"') in html
        # base.html and jsonld.html still read the page's own tag or
        # author: the crumbs place it under that taxonomy's index
        assert f'"name": "{index}"' in html
        # and the term's posts are there as cards
        assert (f'<h2 class="card-title"><a href="{SITE_URL}/'
                f'posts/first-post/">First Post</a></h2>') in html

    # the chip indexes, each over its own taxonomy, in name order
    beta = _Term("beta", "tags")
    chips = ('<a class="chip" href="%s/%s">%s <span>%d</span></a>'
             % (SITE_URL, t.url, t, n) for t, n in ((beta, 2), (tag, 1)))
    html = render_pelican_page(site, "tags.html", articles_page=None,
                               output_file="tags/index.html",
                               tags=[(tag, [1]), (beta, [1, 2])])
    assert "<title>Tags \u00b7 Example Blog</title>" in html
    assert '<h1 class="page-title">Tags</h1>' in html
    assert "\n".join(chips) in html
    html = render_pelican_page(site, "authors.html", articles_page=None,
                               output_file="authors/index.html",
                               authors=[(author, [1])])
    assert "<title>Authors \u00b7 Example Blog</title>" in html
    assert '<h1 class="page-title">Authors</h1>' in html
    assert ('<a class="chip" href="%s/%s">%s <span>1</span></a>'
            % (SITE_URL, author.url, author)) in html


def test_image_zoom(project):
    # post pages carry the click-to-zoom modal, on both engines
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    for page in (hugo_site / "layouts/page.html",
                 pelican_site / "theme/templates/article.html"):
        text = page.read_text()
        assert '<dialog class="zoom-dialog"' in text, page
        # the dialog follows the article whose images it zooms
        assert text.index("</article>") < text.index("zoom-dialog"), page
        # zoom to the src attribute, never currentSrc: src is the
        # full-size original, currentSrc the smaller srcset variant
        assert "full.src = img.src" in text, page
        assert "currentSrc" not in text, page
        # only images holding more detail than the column shows are
        # marked, and the width attribute -- not naturalWidth, which
        # srcset density-corrects -- is what the original measures
        assert 'parseInt(img.getAttribute("width"), 10)' in text, page
        # keyboard reachable, and a linked image keeps its link
        assert 'img.closest("a")' in text, page
        assert "img.tabIndex = 0" in text, page
    css = (hugo_site / "static/css/style.css").read_text()
    assert "img.zoomable { cursor: zoom-in; }" in css
    assert ".zoom-dialog::backdrop" in css
    assert "prefers-reduced-motion" in css
    assert css == (pelican_site / "theme/static/css/style.css").read_text()


def test_code_copy(project):
    # post pages carry the code-block copy button, on both engines
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    for page in (hugo_site / "layouts/page.html",
                 pelican_site / "theme/templates/article.html"):
        text = page.read_text()
        assert '<template class="code-copy-template">' in text, page
        # the button follows the article whose blocks it serves, and is
        # added only where the clipboard API can honour it
        assert text.index("</article>") < text.index("code-copy-template"), page
        assert "navigator.clipboard.writeText" in text, page
        # every pre in the article, whatever the engine wrapped it in,
        # gets a positioning box of its own and a cloned button
        assert 'article.querySelectorAll("pre")' in text, page
        assert 'block.className = "code-block"' in text, page
        assert "template.content.firstElementChild.cloneNode(true)" in text, page
        # the icons swap by attribute: an SVG element has no `hidden`
        # property to set, so assigning one would change nothing
        assert 'icon.toggleAttribute("hidden"' in text, page
        assert "icon.hidden" not in text, page
        # the copied text is the block's, without its trailing newline
        assert 'pre.textContent.replace(/\\n$/, "")' in text, page
        # a screen reader hears the copy through the live region
        assert 'role="status"' in text, page
        assert 'announce("Copied to clipboard")' in text, page
    # hugo highlights by class, never Chroma's inlined Monokai, which
    # paints a dark block on the light page; the theme colours the
    # tokens on the class names Pygments and Chroma share, per palette
    config = hugo_config(hugo_site)
    assert "[markup.highlight]\nnoClasses = false" in config
    css = (hugo_site / "static/css/style.css").read_text()
    assert css.count("--syn-keyword:") == 3      # light, and dark twice
    assert ".post .highlight .k," in css
    assert ".code-block { position: relative; }" in css
    assert ".code-copy { position: absolute;" in css
    assert ".code-copy:focus-visible" in css
    # hidden until the block is hovered or the button reached by
    # keyboard, and always shown where there is no hover
    assert ".code-block:hover .code-copy, .code-copy:focus-visible { opacity: 1; }" in css
    assert "@media (hover: none) { .code-copy { opacity: 1; } }" in css
    assert css == (pelican_site / "theme/static/css/style.css").read_text()


def test_heading_anchor(project):
    # post pages carry the per-heading link mark, on both engines
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    for page in (hugo_site / "layouts/page.html",
                 pelican_site / "theme/templates/article.html"):
        text = page.read_text()
        assert '<template class="heading-anchor-template">' in text, page
        # the mark follows the article whose headings it serves
        assert text.index("</article>") < text.index("heading-anchor-template"), page
        # every body heading that has an id gets one, cloned from the
        # template; the ids are the readers' own, so the script makes none
        assert 'article.querySelectorAll(' in text, page
        assert '"h1[id], h2[id], h3[id], h4[id], h5[id], h6[id]"' in text, page
        assert "template.content.firstElementChild.cloneNode(true)" in text, page
        assert 'link.setAttribute("href", "#" + heading.id)' in text, page
        # an SVG is not a label, so the link carries its own
        assert 'aria-label="Link to this heading"' in text, page
    # a plain link, so the browser's own handling applies: the snippet
    # binds no click of its own, and sets no id the readers didn't give
    snippet = sites.template_text("shared/heading-anchor.html")
    assert "addEventListener(\"click\"" not in snippet
    assert "preventDefault" not in snippet
    assert "heading.id =" not in snippet
    css = (hugo_site / "static/css/style.css").read_text()
    # hidden until its heading is hovered or the mark reached by
    # keyboard, and always shown where there is no hover; by opacity,
    # so revealing it never re-wraps the heading
    assert ".heading-anchor { margin-left: .3em; color: var(--muted); opacity: 0; }" in css
    assert (".post :hover > .heading-anchor,\n"
            ".heading-anchor:focus-visible { opacity: 1; }") in css
    assert "@media (hover: none) { .heading-anchor { opacity: 1; } }" in css
    # a mark, not words: no rule under it, and the accent only under
    # the pointer, where the copy button takes it too
    assert ".heading-anchor, .heading-anchor:hover { text-decoration-line: none; }" in css
    assert (".heading-anchor:hover, .heading-anchor:focus-visible"
            " { color: var(--accent); }") in css
    assert css == (pelican_site / "theme/static/css/style.css").read_text()


def _contrast(a: str, b: str) -> float:
    """WCAG 2 contrast ratio of two #rrggbb colours."""
    def luminance(hex_colour):
        channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
                  for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_syntax_colours_contrast():
    """The syntax colours are Primer's, an AA scheme: every token clears
    WCAG AA (4.5:1) on its palette's code background, except the light
    comment grey, GitHub's own shortfall, which is held where it is."""
    import re
    # template_text splices the dark palette into card.css twice, so
    # the light set is what comes before the first splice point
    css = sites.template_text("shared/card.css")
    light = css.split(':root[data-theme="dark"]')[0]
    dark = sites.template_text("shared/dark-palette.css")
    for name, palette in (("light", light), ("dark", dark)):
        code_bg = re.search(r"--code-bg: (#[0-9a-f]{6})", palette).group(1)
        colours = re.findall(r"--syn-([a-z]+): (#[0-9a-f]{6})", palette)
        assert len(colours) == 6, name
        for token, colour in colours:
            ratio = _contrast(colour, code_bg)
            floor = 4.2 if (name, token) == ("light", "comment") else 4.5
            assert ratio >= floor, (name, token, colour, round(ratio, 2))


def test_post_share_links(project):
    """A post carries the five share links twice -- under the byline and
    at the foot -- from one definition per engine, each mark coming from
    the shared sprite."""
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    # each engine names the same three values, so the bars keep one shape
    for bar, url, title, text in (
            (hugo_site / "layouts/_partials/share.html", "{{ .Permalink }}",
             "{{ .Title }}", "{{ $text }}"),
            (pelican_site / "theme/templates/macros.html", "{{ enc_url }}",
             "{{ enc_title }}", "{{ enc_text }}")):
        source = bar.read_text()
        for network in ("linkedin", "facebook", "bluesky", "mastodon", "email"):
            assert f'<use href="#share-{network}"></use>' in source, bar
        assert '<div class="post-share" data-pagefind-ignore>' in source, bar
        # each network's own documented share URL: the page address alone
        # for LinkedIn and Facebook, which read the rest off the Open
        # Graph tags; a prefilled text for Bluesky and Mastodon. A toot
        # goes to the reader's own server, which the page cannot know,
        # so Mastodon's link is to the network's share sheet, which asks
        # for the server; the text rides in the fragment, as its own
        # instructions generate it, out of server logs and referrers
        for target in (f"linkedin.com/sharing/share-offsite/?url={url}",
                       f"facebook.com/sharer/sharer.php?u={url}",
                       f"bsky.app/intent/compose?text={text}",
                       f"share.joinmastodon.org/#text={text}",
                       f"mailto:?subject={title}&amp;body={url}"):
            assert target in source, bar
        assert "data-share-text" not in source, bar

    for page, call in ((hugo_site / "layouts/page.html",
                        '{{ partial "share.html" . }}'),
                       (pelican_site / "theme/templates/article.html",
                        "{{ share(post_url, post_title) }}")):
        source = page.read_text()
        # once under the byline and once after the body, both inside the
        # post card, and the sprite they draw from ahead of the first
        head, foot = source.index(call), source.rindex(call)
        assert head != foot, page
        assert source.index("share-sprite") < head, page
        assert source.index("post-meta") < head < source.index("</article>"), page
        assert foot < source.index("</article>"), page
        # the bar is plain links: the Mastodon prompt script is gone
        # (the sprite's #share-mastodon symbol is still on the page)
        assert 'querySelectorAll(".share-mastodon")' not in source, page
        assert "mastodon-host" not in source, page

    # hugo escapes each value for its URL context on its own; pelican's
    # Jinja does not, so the theme spells the encoding out -- on each
    # value, which is what this pins: the check must fail when one of
    # them loses its encoding, not merely when the file has none left
    lines = {line.split()[2]: line for line
             in (pelican_site / "theme/templates/macros.html").read_text()
             .splitlines() if line.startswith("{% set ")}
    for name in ("enc_url", "enc_title", "enc_text"):
        assert lines[name].endswith('|urlencode|replace("/", "%2F") %}'), name

    css = (hugo_site / "static/css/style.css").read_text()
    assert ".share-sprite { display: none; }" in css
    assert ".share-icon { width: 1.05rem" in css
    # the marks are the networks' logos: a hover may deepen them, but
    # recoloring them to this site's accent is against most of those
    # networks' brand guidelines. The ring around a mark is the site's
    # own, so only the text colour (the mark's, via currentColor) is
    # held off the accent
    hover = next(line for line in css.splitlines()
                 if line.startswith(".share-link:hover"))
    colour = re.search(r"[{;]\s*color: ([^;]+);", hover).group(1)
    assert colour != "var(--accent)", hover
    assert css == (pelican_site / "theme/static/css/style.css").read_text()


def test_share_targets_get_the_open_graph_tags_they_render_from(project):
    """LinkedIn's and Facebook's share URLs carry only the page address:
    everything their share box shows comes from the page's Open Graph
    tags, so the share links are worth no more than these."""
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    for head in (hugo_site / "layouts/baseof.html",
                 pelican_site / "theme/templates/base.html"):
        source = head.read_text()
        for prop in ("og:site_name", "og:type", "og:title", "og:url",
                     "og:description", "og:image", "article:published_time"):
            assert f'property="{prop}"' in source, (head, prop)
        assert 'rel="canonical"' in source, head
        # a post with no cover has no image to promise
        assert "summary_large_image" in source and "summary" in source, head


def test_a_title_is_plain_text_not_html(project):
    """Pelican renders only FORMATTED_FIELDS (summary) as markdown, so a
    post's title reaches the theme as the plain text of its Title:
    header. Stripping tags from it would delete any run shaped like one
    -- "Using <script> tags safely" -> "Using tags safely" -- rather
    than escape it, losing what hugo keeps."""
    pelican_site = build(pelican, project)
    article = (pelican_site / "theme/templates/article.html").read_text()
    assert "{% set post_title = article.title %}" in article
    assert "article.title|striptags" not in article
    # the page name the head renders into <title> and og:title is the
    # template's name block, and the article's is the title as it is
    assert "{% block name %}{{ article.title }}{% endblock %}" in article

    base = (pelican_site / "theme/templates/base.html").read_text()
    og_title = next(line for line in base.splitlines()
                    if 'property="og:title"' in line)
    assert 'content="{{ page_title }}' in og_title, og_title
    assert "{% set page_title = self.name() %}" in base
    assert "<title>{% block name %}{{ SITENAME }}{% endblock %}" in base
    assert "striptags" not in og_title, og_title
    # the summary, though, really is HTML -- pelican formats that one,
    # and an auto-generated summary is a fragment of the body -- so it
    # keeps the stripping, here as in the card macro
    og_desc = next(line for line in base.splitlines()
                   if 'property="og:description"' in line)
    assert "article.summary|striptags|e" in og_desc, og_desc


def test_pelican_escapes_by_default(project):
    """Pelican's own default JINJA_ENVIRONMENT sets no autoescape and
    jinja's default is off, so a theme emits every {{ }} raw -- which
    makes a post title reading `<script>...` a running script on every
    page that renders it, and titles, tag names and authors all come
    from the archived publication. The generated config turns escaping
    on; only the rendered body is marked safe."""
    site = build(pelican, project)
    config = (site / "pelicanconf.py").read_text()
    assert '"autoescape": True' in config
    # the setting replaces pelican's defaults rather than merging, so
    # the rest of them have to be restated with it
    for key in ('"trim_blocks": True', '"lstrip_blocks": True',
                '"extensions": []'):
        assert key in config, key
    # the genuinely-HTML values in the theme, and the only ones: the
    # rendered body, and the landing-page intro and footer line the
    # config renders from site.toml's Markdown. Both of those are
    # hand-written and versioned with the archive, unlike a title or a
    # tag name, which come from whoever wrote the post -- that is what
    # makes them safe to mark safe.
    templates = site / "theme/templates"
    safe = [(f.name, line.strip()) for f in sorted(templates.glob("*.html"))
            for line in f.read_text().splitlines() if "|safe" in line]
    assert safe == [
        # a FORMATTED_FIELD: the rendered HTML of the subtitle's own
        # Markdown, like article.content below it
        ("article.html",
         '{% if article.subtitle %}<div class="post-subtitle">'
         "{{ article.subtitle|safe }}</div>{% endif %}"),
        ("article.html", "{{ article.content|safe }}"),
        ("base.html",
         '<footer class="wrap site-footer">{% if FOOTER %}{{ FOOTER|safe }}'
         '{% else %}{{ SITESUBTITLE }}{% endif %}</footer>'),
        ("index.html",
         '{% if INTRO %}<div class="intro">{{ INTRO|safe }}</div>{% endif %}'),
    ], safe


def test_the_subtitle_reaches_every_post_page(tmp_path):
    """The post's subtitle line is front matter, not body, and each
    site's post template renders it under the title -- with the links
    the plain-text description lost, and with a link to another post of
    the publication rewritten to its page, as a body link would be."""
    manifest = {}
    make_post(tmp_path, manifest, "first-post", "aaa111aaa111",
              "2020-01-05T10:00:00Z", "Body.\n",
              subtitle=f"Read [the sequel]({BASE}/second-post-bbb222bbb222) "
                       "and [the docs](https://example.org/docs).")
    make_post(tmp_path, manifest, "second-post", "bbb222bbb222",
              "2021-03-01T10:00:00Z", "Body.\n")
    manifest_json(tmp_path).write_text(json.dumps(manifest))
    write_site(tmp_path, 
        {"title": "Example Blog", "base_url": "https://blog.example.org/"})

    for module in (hugo, pelican):
        site = build(module, tmp_path)
        page = (site / "content/posts/2020/first-post/index.md").read_text()
        line = next(l for l in page.split("\n") if l.startswith("subtitle:"))
        # the links survive, and the in-publication one points at its page
        assert "[the sequel](/posts/2021/second-post/)" in line
        assert "[the docs](https://example.org/docs)" in line
        # and the body is the body alone
        assert page.split("\n---\n", 1)[1].strip() == "Body."

    # both post templates render it in the element the shared CSS styles
    for tpl in ("hugo/layouts/page.html",
                "pelican/theme/templates/article.html"):
        assert 'class="post-subtitle"' in sites.template_text(tpl)
    assert ".post-subtitle {" in sites.template_text("shared/card.css")


def test_missing_base_url_is_not_silent(tmp_path, capsys):
    """Every absolute link -- feeds, redirect stubs, og:url, the share
    links -- is built from base_url, and a share link with the wrong one
    fails outright rather than degrading, so an unset base_url has to be
    said out loud at build time."""
    manifest = {}
    make_post(tmp_path, manifest, "post", "aaa111aaa111",
              "2020-01-05T10:00:00Z", "Hello.\n")
    manifest_json(tmp_path).write_text(json.dumps(manifest))
    write_site(tmp_path, {"title": "Example"})
    sites.load_site_inputs(archive_dir(tmp_path), site_inputs(tmp_path))
    assert "no base_url" in capsys.readouterr().err
    # and stays quiet once it is set
    write_site(tmp_path, 
        {"title": "Example", "base_url": "https://blog.example.org"})
    sites.load_site_inputs(archive_dir(tmp_path), site_inputs(tmp_path))
    assert "base_url" not in capsys.readouterr().err


def test_build_output_survives_regeneration(project):
    for module, kept in ((hugo, "public"), (pelican, "output")):
        site = build(module, project)
        (site / kept).mkdir()
        (site / kept / "index.html").write_text("built")
        build(module, project)
        assert (site / kept / "index.html").read_text() == "built"


def git_init(path: Path):
    """A git working tree at path, so the exporters' --clean can ask
    git what to keep there."""
    import subprocess

    for cmd in (["init", "-q"], ["config", "user.email", "t@example.com"],
                ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(path), *cmd], check=True,
                       capture_output=True)


def test_a_site_is_rebuilt_in_place(project, tmp_path):
    """--out is written over, not emptied first: it can be a checkout
    of the published site, so what the run does not write -- a file a
    person put there, the repository itself -- is still there
    afterwards."""
    out = tmp_path / "published"
    out.mkdir()
    git_init(out)
    (out / "CNAME").write_text("blog.example.org\n")

    build(pelican, project, out=out)
    build(pelican, project, out=out)      # and again, over its own work

    assert (out / "CNAME").read_text() == "blog.example.org\n"
    assert (out / ".git").is_dir()
    assert (out / "content/posts/2020/first-post/index.md").exists()
    assert (out / "content/posts/2021/second-post/images/001-pic.png").exists()


def test_clean_keeps_the_repository_and_what_it_ignores(project, tmp_path):
    """--clean is what removes a page the archive no longer has, so it
    empties the site -- but a site kept in git carries the two things
    that are not the exporter's to delete: the repository itself, and
    the build output and caches its own .gitignore names."""
    out = tmp_path / "published"
    site = build(pelican, project, out=out)
    git_init(out)
    (site / "output").mkdir()
    (site / "output" / "index.html").write_text("built")
    stale = site / "content/posts/2020/deleted-post"
    stale.mkdir(parents=True)
    (stale / "index.md").write_text("a post the archive no longer has\n")
    (site / "CNAME").write_text("blog.example.org\n")

    build(pelican, project, out=out, clean=True)

    assert not stale.exists()             # the point of --clean
    assert (site / "output" / "index.html").read_text() == "built"
    assert (site / ".git").is_dir()
    assert not (site / "CNAME").exists()  # tracked or not, it is not ignored
    assert (site / "content/posts/2020/first-post/index.md").exists()


def test_clean_outside_a_repository_keeps_the_build_output(project, tmp_path):
    """With no git working tree there are no ignore rules to read, so
    the generator's own build directories are kept by name and
    everything else goes."""
    out = tmp_path / "unversioned"
    site = build(hugo, project, out=out)
    (site / "public").mkdir()
    (site / "public" / "index.html").write_text("built")
    stale = site / "content/posts/2020/deleted-post"
    stale.mkdir(parents=True)

    build(hugo, project, out=out, clean=True)

    assert (site / "public" / "index.html").read_text() == "built"
    assert not stale.exists()


def test_clean_inside_a_repository_that_ignores_the_site(project, tmp_path):
    """The default site-<generator>/ sits inside the archive's own
    repository, which ignores it whole -- git then calls every file
    under it ignored, which must not read as "keep all of it" and turn
    --clean into a no-op."""
    git_init(tmp_path)
    (tmp_path / ".gitignore").write_text("/site-hugo/\n")
    site = build(hugo, project, out=tmp_path / "site-hugo")
    (site / "public").mkdir()
    (site / "public" / "index.html").write_text("built")
    stale = site / "content/posts/2020/deleted-post"
    stale.mkdir(parents=True)

    build(hugo, project, out=site, clean=True)

    assert not stale.exists()
    assert (site / "public" / "index.html").read_text() == "built"


def test_a_page_the_archive_lost_is_reported(project, capsys):
    """A rebuild writes the pages it makes and leaves the rest alone,
    so a page whose post has left the archive would go on being served
    unsaid. Every exporter names them, and what removes them."""
    site = build(pelican, project)
    (site / "content/posts/2020/deleted-post").mkdir(parents=True)
    capsys.readouterr()

    build(pelican, project)

    err = capsys.readouterr().err
    assert "deleted-post" in err and "--clean" in err


def test_rebuilding_relinks_an_image_git_replaced(project, tmp_path):
    """git breaks a hard link whenever it writes the file itself (a
    checkout, a stash, a merge), so a rebuild has to replace what it
    finds rather than write through it -- writing through would edit
    the image cache's own copy under its content-addressed name."""
    out = tmp_path / "published"
    site = build(pelican, project, out=out)
    placed = site / "content/posts/2021/second-post/images/001-pic.png"
    original = archive_dir(project) / "posts/2021-03-01-second-post/images/001-pic.png"
    assert placed.stat().st_ino == original.stat().st_ino

    placed.unlink()                       # as git checkout leaves it:
    placed.write_bytes(b"PNG")            # same bytes, its own inode
    assert placed.stat().st_ino != original.stat().st_ino

    build(pelican, project, out=out)
    assert placed.stat().st_ino == original.stat().st_ino
    assert placed.read_bytes() == b"PNG"


def line_art(w, h):
    """Flat-colored art like the charts and screenshots most of the
    archive's PNGs are: few colors, long runs of identical pixels."""
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(im)
    for i in range(h // 40):
        draw.rectangle((20, 20 + i * 40, 20 + (i + 1) * 30, 44 + i * 40),
                       fill="#1f77b4")
        draw.text((26, 24 + i * 40), f"row {i} of the chart", fill="black")
    return im


def make_image_post(tmp_path, still_bytes=None, gif_bytes=None):
    """An archive whose one post carries real images: a large noisy PNG
    (a photograph, in PNG clothing), a small noisy PNG, a wide line-art
    PNG (a chart), and junk bytes with a .png name (unreadable; must
    pass through)."""
    import os

    from PIL import Image

    manifest = {}
    images = ["images/big.png", "images/small.png", "images/chart.png",
              "images/junk.png"]
    if gif_bytes:
        images.append("images/anim.gif")
    body = "![big](images/big.png)\n\n![chart](images/chart.png)\n"
    if gif_bytes:
        body += "\n![a screen recording](images/anim.gif)\n"
    make_post(tmp_path, manifest, "picture-post", "ccc333ccc333",
              "2022-06-01T10:00:00Z", body, images=images)
    img_dir = archive_dir(tmp_path) / "posts/2022-06-01-picture-post/images"
    img_dir.mkdir()

    def noise(w, h):
        return Image.frombytes("RGB", (w, h), os.urandom(w * h * 3))

    noise(2000, 1200).save(img_dir / "big.png")
    noise(200, 100).save(img_dir / "small.png")
    line_art(2400, 900).save(img_dir / "chart.png")
    (img_dir / "junk.png").write_bytes(b"PNG")
    if gif_bytes:
        frames = [noise(1600, 1200).convert("P") for _ in range(3)]
        frames[0].save(img_dir / "anim.gif", save_all=True,
                       append_images=frames[1:], duration=100, loop=0)
    manifest_json(tmp_path).write_text(json.dumps(manifest))
    write_site(tmp_path, {"title": "Pics"})
    return img_dir


def test_photographs_capped_into_display_copies(tmp_path):
    from PIL import Image

    src = make_image_post(tmp_path)
    site = build(hugo, tmp_path)
    placed = site / "content/posts/2022/picture-post/images"
    # a photograph is capped and encoded lossily, whatever it arrived as
    with Image.open(placed / "big.jpg") as im:
        assert max(im.size) == 1600 and im.format == "JPEG"
    assert not (placed / "big.png").exists()
    assert (placed / "big.jpg").stat().st_size < (src / "big.png").stat().st_size
    assert (placed / "small.jpg").stat().st_size < (src / "small.png").stat().st_size
    # the page follows the images it actually got
    page = (site / "content/posts/2022/picture-post/index.md").read_text()
    assert "![big](images/big.jpg)" in page
    # an unreadable file passes through as a hard link
    assert (placed / "junk.png").read_bytes() == b"PNG"
    assert (placed / "junk.png").stat().st_ino == (src / "junk.png").stat().st_ino
    # the display copy is built once and shared across exporters
    pelican_site = build(pelican, tmp_path)
    assert (pelican_site / "content/posts/2022/picture-post/images/big.jpg"
            ).stat().st_ino == (placed / "big.jpg").stat().st_ino
    # caps are configurable, 0 leaves stills alone entirely
    write_site(tmp_path, 
        {"title": "Pics", "images": {"still_max_edge": 0}})
    site = build(hugo, tmp_path)
    assert (placed / "big.png").stat().st_ino == (src / "big.png").stat().st_ino


def test_line_art_keeps_every_pixel(tmp_path):
    """Charts and screenshots are re-encoded losslessly at their own
    resolution: the small text in them does not survive a downscale, and
    flat color costs little to keep."""
    from PIL import Image, ImageChops

    src = make_image_post(tmp_path)
    site = build(hugo, tmp_path)
    placed = site / "content/posts/2022/picture-post/images"
    assert not (placed / "chart.png").exists()
    with Image.open(src / "chart.png") as before, \
            Image.open(placed / "chart.webp") as after:
        assert after.size == before.size          # past the 1600 px cap
        assert not ImageChops.difference(before.convert("RGB"),
                                         after.convert("RGB")).getbbox()
    assert (placed / "chart.webp").stat().st_size < (
        src / "chart.png").stat().st_size
    page = (site / "content/posts/2022/picture-post/index.md").read_text()
    assert "![chart](images/chart.webp)" in page


def test_line_art_classifier(tmp_path):
    import os

    from PIL import Image

    assert sites.is_line_art(line_art(600, 400))
    noise = Image.frombytes("RGB", (300, 200), os.urandom(300 * 200 * 3))
    assert not sites.is_line_art(noise)
    # a photograph on a flat background is still a photograph
    inset = Image.new("RGB", (600, 400), "white")
    inset.paste(noise, (150, 100))
    assert not sites.is_line_art(inset)


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"),
                    reason="ffmpeg not installed")
def test_animated_gifs_placed_as_video(tmp_path):
    """An animation is placed as an h264 clip with its first frame
    beside it as a poster, and the page follows it to its new
    extension. The clip carries the frames at the gif's own size (no
    animated cap by default); the poster is the same size, so it states
    the clip's dimensions for the theme."""
    from PIL import Image

    src = make_image_post(tmp_path, gif_bytes=True)
    site = build(hugo, tmp_path)
    placed = site / "content/posts/2022/picture-post/images"
    assert not (placed / "anim.gif").exists()
    assert (placed / "anim.mp4").stat().st_size < (
        src / "anim.gif").stat().st_size
    with Image.open(placed / "anim-poster.webp") as im:
        assert im.size == (1600, 1200)     # the gif's own size
    page = (site / "content/posts/2022/picture-post/index.md").read_text()
    assert "![a screen recording](images/anim.mp4)" in page


def has_encoder(name):
    """Whether the ffmpeg on PATH lists the encoder `name`."""
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        return False
    return f" {name} " in subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True, text=True).stdout


def video_stream(path):
    """codec, pixel format, width and height of a clip's video, and its
    length in ms."""
    import subprocess
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,pix_fmt,width,height"
         ":format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout)
    v = probe["streams"][0]
    return (v["codec_name"], v["pix_fmt"], v["width"], v["height"],
            round(float(probe["format"]["duration"]) * 1000))


# a site.toml that stores every animation as its AV1 master
ALWAYS_MASTER = {"images": {"master_max_share": 1000}}

needs_av1_and_h264 = pytest.mark.skipif(
    not (has_encoder("libaom-av1") and has_encoder("libx264")
         and __import__("shutil").which("ffprobe")),
    reason="ffmpeg with libaom and libx264 not installed")


def make_clip_post(tmp_path, config=None):
    """An archive whose one post carries one animated gif of an odd
    size with uneven delays, and the site.toml `config` (title added)."""
    manifest = {}
    make_post(tmp_path, manifest, "clip-post", "ddd444ddd444",
              "2022-06-01T10:00:00Z",
              "![a screen recording](images/anim.gif)\n",
              images=["images/anim.gif"])
    img_dir = archive_dir(tmp_path) / "posts/2022-06-01-clip-post/images"
    img_dir.mkdir()
    delays = [590, 750, 300, 450, 120, 870, 330, 1010]
    animation(img_dir / "anim.gif",
              [gradient_frame((201, 151), i * 9) for i in range(8)],
              duration=delays)
    manifest_json(tmp_path).write_text(json.dumps(manifest))
    write_site(tmp_path, {"title": "Clips", **(config or {})})
    return img_dir / "anim.gif", delays


@needs_av1_and_h264
def test_the_pelican_site_stores_av1_masters_of_its_clips(tmp_path):
    """The pelican site keeps an animation as the AV1 4:4:4 master its
    build makes the served h264 from: under the clip's name, at the
    gif's own size (4:4:4 needs no padding), as long as the gif, and
    with the poster the served clip will have, padded to its even
    size. The hugo site, which serves what it stores, keeps h264.
    (master_max_share stores the master whatever the lossless copies
    weigh, since this is what is being tested.)"""
    from PIL import Image

    gif, delays = make_clip_post(tmp_path, ALWAYS_MASTER)
    images = "content/posts/2022/clip-post/images"
    pelican_site = build(pelican, tmp_path)
    master = pelican_site / images / "anim.mp4"
    assert video_stream(master) == ("av1", "yuv444p", 201, 151, sum(delays))
    with Image.open(pelican_site / images / "anim-poster.webp") as im:
        assert im.size == (202, 152)
    page = (pelican_site / "content/posts/2022/clip-post/index.md").read_text()
    assert "![a screen recording]({attach}images/anim.mp4)" in page

    hugo_site = build(hugo, tmp_path)
    assert video_stream(hugo_site / images / "anim.mp4")[:2] == (
        "h264", "yuv420p")


@needs_av1_and_h264
def test_the_pelican_build_serves_h264_made_from_each_master(
        tmp_path, monkeypatch, capsys):
    """What a pelican build copies into output/ is the master; the site
    plugin replaces it with the h264 every browser decodes in hardware,
    padded to an even size and as long as the gif, and keeps that h264
    by the master's hash, so the next build encodes nothing."""
    import shutil

    gif, delays = make_clip_post(tmp_path, ALWAYS_MASTER)
    site = build(pelican, tmp_path)
    master = site / "content/posts/2022/clip-post/images/anim.mp4"
    output = tmp_path / "output"
    served = output / "posts/2022/clip-post/images/anim.mp4"
    served.parent.mkdir(parents=True)
    monkeypatch.setenv("CLIP_CACHE", str(tmp_path / "clips"))
    serve = config_namespace(site)["_serve_clips"]

    for encoded in (1, 0):
        shutil.copyfile(master, served)          # as pelican copies it
        serve(SimpleNamespace(output_path=str(output)))
        assert video_stream(served) == ("h264", "yuv420p", 202, 152,
                                        sum(delays))
        assert f"({encoded} encoded)" in capsys.readouterr().out
    assert master.read_bytes() != served.read_bytes()


@needs_av1_and_h264
def test_a_pelican_site_can_store_h264_instead(tmp_path, monkeypatch,
                                               capsys):
    """clip_master = "none" stores the h264 clip the other sites carry,
    the very file, and the build serves it as it is."""
    make_clip_post(tmp_path, {"images": {"clip_master": "none"}})
    images = "content/posts/2022/clip-post/images"
    pelican_site = build(pelican, tmp_path)
    hugo_site = build(hugo, tmp_path)
    for name in ("anim.mp4", "anim-poster.webp"):
        assert (pelican_site / images / name).stat().st_ino == (
            hugo_site / images / name).stat().st_ino
    output = tmp_path / "output"
    served = output / "posts/2022/clip-post/images/anim.mp4"
    served.parent.mkdir(parents=True)
    stored = (pelican_site / images / "anim.mp4").read_bytes()
    served.write_bytes(stored)
    monkeypatch.setenv("CLIP_CACHE", str(tmp_path / "clips"))
    config_namespace(pelican_site)["_serve_clips"](
        SimpleNamespace(output_path=str(output)))
    assert served.read_bytes() == stored
    assert "0 served as h264" in capsys.readouterr().out


@needs_av1_and_h264
def stored_lossless(images):
    """The one lossless copy of `anim` a site stores: gif or WebP."""
    found = [p for p in images.iterdir()
             if p.name in ("anim.gif", "anim.webp")]
    assert len(found) == 1, found
    return found[0]


@needs_av1_and_h264
def test_a_pelican_site_stores_a_lossless_copy_where_a_master_does_not_pay(
        tmp_path, monkeypatch, capsys):
    """Where the AV1 master is not enough smaller than the smaller of
    the capped gif and the capped lossless WebP (master_max_share; 0
    here, so never), the site stores that copy -- frame for frame the
    animation's -- with the clip's poster beside it, and the page
    points at it. The build makes the h264 beside it and shows that as
    the page's <video>; the copy stays for the feeds."""
    from PIL import Image, ImageChops

    gif, delays = make_clip_post(tmp_path, {"images": {"master_max_share": 0}})
    site = build(pelican, tmp_path)
    images = site / "content/posts/2022/clip-post/images"
    stored = stored_lossless(images)
    assert (images / "anim-poster.webp").exists()
    assert not (images / "anim.mp4").exists()
    page = (site / "content/posts/2022/clip-post/index.md").read_text()
    assert f"![a screen recording]({{attach}}images/{stored.name})" in page
    with Image.open(gif) as before, Image.open(stored) as after:
        assert after.n_frames == before.n_frames
        for i in range(before.n_frames):
            before.seek(i)
            after.seek(i)
            after.load()             # WebP sets a frame's duration then
            assert after.info["duration"] == delays[i]
            assert not ImageChops.difference(
                before.convert("RGB"), after.convert("RGB")).getbbox()

    # the build, as pelican runs it: the stored files copied into the
    # output, the page rendered with the gif as a body image
    output = tmp_path / "output"
    served = output / "posts/2022/clip-post/images"
    served.mkdir(parents=True)
    for f in images.iterdir():
        (served / f.name).write_bytes(f.read_bytes())
    page = output / "posts/2022/clip-post/index.html"
    page.write_text(f'<p><img src="/posts/2022/clip-post/images/{stored.name}" '
                    'alt="a screen recording" data-body-image '
                    'loading="lazy"></p>\n')
    monkeypatch.setenv("CLIP_CACHE", str(tmp_path / "clips"))
    namespace = config_namespace(site)
    namespace["_serve_clips"](SimpleNamespace(output_path=str(output)))
    assert ("0 served as h264 made from AV1 masters and 1 from gifs and "
            "WebPs") in capsys.readouterr().out
    assert video_stream(served / "anim.mp4") == ("h264", "yuv420p", 202, 152,
                                                 sum(delays))
    assert (served / stored.name).exists()
    namespace["_optimize_article_images"](
        SimpleNamespace(output_path=str(output)))
    html = page.read_text()
    assert '<video src="/posts/2022/clip-post/images/anim.mp4"' in html
    assert 'poster="/posts/2022/clip-post/images/anim-poster.webp"' in html
    assert "<img" not in html


@needs_av1_and_h264
@pytest.mark.parametrize("kind", ["gif", "webp"])
def test_a_lossless_copy_is_frame_rate_capped_exactly(tmp_path, kind):
    """A lossless copy stored in place of a master, gif or WebP, drops
    the frames a clip drops (kept_frames), and keeps every other one
    pixel for pixel, each from its own start until the next kept
    frame's -- the gif written whole, since dropping a frame breaks the
    partial frames after it."""
    from PIL import Image, ImageChops

    src = tmp_path / "src"
    src.mkdir()
    delays = [10] * 12 + [500, 10, 10, 10, 400]
    gif = animation(src / "burst.gif",
                    [gradient_frame((96, 64), i * 7)
                     for i in range(len(delays))], duration=delays)
    keep = sites.kept_frames(delays)
    assert len(keep) < len(delays)
    placer = sites.ImagePlacer(tmp_path / "cache",
                               {"images": {"master_max_share": 0}},
                               masters=True)
    out = tmp_path / "out"
    out.mkdir()
    placer.place(gif, out / "burst.gif")
    stored = next(placer.cache.glob(f"*.capped.{kind}"))
    assert stored.stat().st_size
    assert sites.poster_path(stored).exists()
    starts = [sum(delays[:i]) for i in range(len(delays) + 1)]
    ends = keep[1:] + [len(delays)]
    with Image.open(gif) as before, Image.open(stored) as after:
        assert after.n_frames == len(keep)
        for j, (i, end) in enumerate(zip(keep, ends)):
            before.seek(i)
            after.seek(j)
            after.load()             # WebP sets a frame's duration then
            assert after.info["duration"] == starts[end] - starts[i]
            assert not ImageChops.difference(
                before.convert("RGB"), after.convert("RGB")).getbbox()


def test_a_webp_that_cannot_win_is_not_made(tmp_path):
    """The WebP is the slow candidate, so it is made only where it could
    be stored: not where the master is at most master_max_share of
    WEBP_MIN_SHARE of the capped gif. Nothing is cached for it then, so
    a larger share makes it on the next lookup."""
    placer = sites.ImagePlacer(tmp_path / "cache", {}, masters=True)
    master, gif = tmp_path / "m.mp4", tmp_path / ("x" + sites.CAPPED_GIF_SUFFIX)
    gif.write_bytes(b"g" * 1000)
    bound = sites.MASTER_MAX_SHARE * sites.WEBP_MIN_SHARE * 1000
    master.write_bytes(b"m" * int(bound))
    assert placer._webp_cannot_win(master, [gif])
    master.write_bytes(b"m" * (int(bound) + 1))
    assert not placer._webp_cannot_win(master, [gif])
    # no capped gif to bound it by: the WebP is made
    assert not placer._webp_cannot_win(master, [])
    # a site that always stores the lossless copy (share 0) makes it too
    always = sites.ImagePlacer(tmp_path / "cache",
                               {"images": {"master_max_share": 0}},
                               masters=True)
    assert not always._webp_cannot_win(master, [gif])


def test_an_unknown_clip_master_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="clip_master"):
        sites.ImagePlacer(tmp_path, {"images": {"clip_master": "vp9"}},
                          masters=True)
    # a site that does not store masters does not read the key
    sites.ImagePlacer(tmp_path, {"images": {"clip_master": "vp9"}})


def test_a_clip_master_needs_an_ffmpeg_with_libaom(tmp_path):
    """An ffmpeg that cannot write AV1 cannot make the master, and says
    so rather than failing inside ffmpeg."""
    src = tmp_path / "src"
    src.mkdir()
    gif = animation(src / "anim.gif",
                    [gradient_frame((64, 48), i * 9) for i in range(4)])
    build = tmp_path / "ffmpeg"                  # one without libaom
    build.write_text("#!/bin/sh\necho ' V....D libx264   H.264'\n"
                     "echo ' V....D libwebp   WebP'\n")
    build.chmod(0o755)
    placer = sites.ImagePlacer(tmp_path / "cache", {}, masters=True)
    placer.ffmpeg = str(build)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(sites.AnimationError, match="without libaom"):
        placer.place(gif, out / "anim.gif")


@pytest.mark.skipif(not __import__("shutil").which("gifsicle"),
                    reason="gifsicle not installed")
def test_animated_gifs_capped_via_gifsicle(tmp_path):
    """A site that asks for gifs keeps them: gifsicle resizes them to
    the animated cap it sets, and no clip is written."""
    from PIL import Image

    src = make_image_post(tmp_path, gif_bytes=True)
    write_site(tmp_path,
               {"title": "Pics", "images": {"animated_format": "gif",
                                            "animated_max_edge": 1104}})
    site = build(hugo, tmp_path)
    placed = site / "content/posts/2022/picture-post/images/anim.gif"
    with Image.open(placed) as im:
        assert max(im.size) == 1104 and im.n_frames == 3
    assert placed.stat().st_size < (src / "anim.gif").stat().st_size
    assert not (placed.parent / "anim.mp4").exists()


def animation(path, frames, duration=100, **save):
    frames[0].save(path, save_all=True, append_images=frames[1:],
                   duration=duration, loop=0, **save)
    return path


def gradient_frame(size, shift, see_through=False):
    """A frame of a moving greyscale gradient: bulky as gif, cheap as
    video, so a clip of it is worth placing. Colour 0 -- the index a
    gif spends on transparency -- is left out of the picture unless the
    frame is meant to be see-through."""
    from PIL import Image

    w, h = size
    base = 0 if see_through else 1
    span = 256 - base
    frame = Image.frombytes(
        "P", size, bytes((base + (x + y + shift) % span)
                         for y in range(h) for x in range(w)))
    frame.putpalette([v for level in range(256) for v in (level,) * 3])
    return frame


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"),
                    reason="ffmpeg not installed")
def test_a_see_through_gif_is_an_error(tmp_path):
    """Gif spends its transparent index on "unchanged since the
    previous frame", so declaring one says nothing about whether a
    reader sees through the picture. What decides is alpha on the
    composited first frame: a gif transparent there cannot become a
    clip -- which stops the build rather than ship an animation a
    reader cannot pause -- and one that is merely delta-coded can."""
    src = tmp_path / "src"
    src.mkdir()
    size = (400, 300)
    holes = animation(
        src / "holes.gif",
        [gradient_frame(size, i * 9, see_through=True) for i in range(8)],
        transparency=0)
    deltas = animation(
        src / "deltas.gif",
        [gradient_frame(size, i * 9) for i in range(8)], transparency=0)
    placer = sites.ImagePlacer(tmp_path / "cache", {})
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(sites.AnimationError, match="transparent"):
        placer.place(holes, out / "holes.gif")
    assert not (out / "holes.gif").exists()
    clip = placer.place(deltas, out / "deltas.gif")
    assert clip.suffix == ".mp4"
    assert sites.poster_path(clip).exists()


@pytest.mark.skipif(not __import__("shutil").which("ffprobe"),
                    reason="ffmpeg not installed")
def test_a_clip_keeps_each_frame_of_the_gif_on_its_own_delay(tmp_path):
    """A gif's delays are hundredths of a second, frame by frame. The
    clip starts every frame exactly where the gif does rather than on
    the grid of a frame rate ffmpeg would otherwise guess, so a reader
    sees each frame for as long as its author meant. (How long the
    last frame lasts is the muxer's guess on older ffmpeg, so only the
    starts are compared.)"""
    import subprocess

    src = tmp_path / "src"
    src.mkdir()
    delays = [590, 750, 300, 450, 120, 870, 330, 1010]
    gif = animation(src / "uneven.gif",
                    [gradient_frame((400, 300), i * 9) for i in range(8)],
                    duration=delays)
    placer = sites.ImagePlacer(tmp_path / "cache", {})
    out = tmp_path / "out"
    out.mkdir()
    clip = placer.place(gif, out / "uneven.gif")
    assert clip.suffix == ".mp4"

    times = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v",
         "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(clip)],
        capture_output=True, text=True, check=True).stdout.split()
    starts = sorted(round(float(t) * 1000) for t in times)
    assert starts == [sum(delays[:i]) for i in range(len(delays))]


@pytest.mark.skipif(not __import__("shutil").which("ffprobe"),
                    reason="ffmpeg not installed")
def test_a_clip_runs_as_long_as_its_gif(tmp_path):
    """The clip holds the gif's frames to the gif's end, the last one
    included: with B-frames, x264 gave the mp4 muxer no durations and
    the clip ended at its last frame's decode time, cutting a long hold
    near the end short."""
    import subprocess

    src = tmp_path / "src"
    src.mkdir()
    delays = [50] * 20 + [2640, 60, 130]
    gif = animation(src / "hold.gif",
                    [gradient_frame((320, 240), i * 9)
                     for i in range(len(delays))], duration=delays)
    placer = sites.ImagePlacer(tmp_path / "cache", {})
    out = tmp_path / "out"
    out.mkdir()
    clip = placer.place(gif, out / "hold.gif")
    length = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(clip)],
        capture_output=True, text=True, check=True).stdout
    assert round(float(length) * 1000) == sum(delays)


def test_an_ffmpeg_without_libwebp_says_so(tmp_path):
    """A build without libwebp cannot write a clip's poster, and fails
    with "Encoder not found". That is asked about up front, and the
    error names it as the thing to fix -- or animated_format = "gif"
    for a site that means to keep gifs."""
    src = tmp_path / "src"
    src.mkdir()
    gif = animation(src / "clip.gif",
                    [gradient_frame((400, 300), i * 9) for i in range(8)])
    build = tmp_path / "ffmpeg"                # one without libwebp
    build.write_text("#!/bin/sh\necho ' V....D libx264   H.264'\n")
    build.chmod(0o755)
    placer = sites.ImagePlacer(tmp_path / "cache", {})
    placer.ffmpeg = str(build)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(sites.AnimationError,
                       match="libwebp.*animated_format"):
        placer.place(gif, out / "clip.gif")


def test_a_single_frame_gif_is_placed_as_a_png_would_be(tmp_path):
    """A gif of one frame is a still under a .gif name: line art becomes
    lossless webp, every pixel kept, as a line-art PNG does -- in every
    site, the pelican site too, and without ffmpeg."""
    from PIL import Image, ImageChops

    src = tmp_path / "src"
    src.mkdir()
    still = src / "chart.gif"
    line_art(1200, 500).convert("P").save(still)
    for masters in (False, True):
        placer = sites.ImagePlacer(tmp_path / "cache", {}, masters=masters)
        placer.ffmpeg = None
        out = tmp_path / f"out-{masters}"
        out.mkdir()
        placed = placer.place(still, out / "chart.gif")
        assert placed == out / "chart.webp"
        assert placed.stat().st_size < still.stat().st_size
        assert not sites.poster_path(placed).exists()
        with Image.open(still) as before, Image.open(placed) as after:
            assert not ImageChops.difference(
                before.convert("RGB"), after.convert("RGB")).getbbox()


def test_an_animation_without_ffmpeg_is_an_error(tmp_path):
    """Where clips are asked for, an animated gif that cannot become one
    stops the build: no ffmpeg is an error, not a note. A still under a
    .gif name is not an animation and needs no ffmpeg, and a site that
    asks for gifs keeps them without ffmpeg."""
    from PIL import Image

    src = tmp_path / "src"
    src.mkdir()
    moving = animation(src / "moving.gif",
                       [gradient_frame((200, 150), i * 9) for i in range(4)])
    still = src / "still.gif"
    gradient_frame((200, 150), 0).save(still)
    out = tmp_path / "out"
    out.mkdir()

    placer = sites.ImagePlacer(tmp_path / "cache", {})
    placer.ffmpeg = None
    with pytest.raises(sites.AnimationError, match="ffmpeg is not installed"):
        placer.place(moving, out / "moving.gif")
    assert placer.place(still, out / "still.gif").stem == "still"

    as_gifs = sites.ImagePlacer(tmp_path / "cache",
                                {"images": {"animated_format": "gif"}})
    assert as_gifs.place(moving, out / "kept.gif") == out / "kept.gif"
    with Image.open(out / "kept.gif") as im:
        assert im.n_frames == 4


def test_warm_names_every_animation_it_could_not_place(tmp_path):
    """warm() tries every image before it gives up, so one build names
    all the gifs that failed, not only the first."""
    archive = tmp_path / "archive"
    for post in ("a", "b"):
        images = archive / "posts" / post / "images"
        images.mkdir(parents=True)
        animation(images / f"{post}.gif",
                  [gradient_frame((100, 80), i * 9) for i in range(3)])
    manifest = {f"https://x/{post}": {"dir": f"posts/{post}"}
                for post in ("a", "b")}
    placer = sites.ImagePlacer(tmp_path / "cache", {})
    placer.ffmpeg = None
    with pytest.raises(sites.AnimationError) as err:
        placer.warm(archive, manifest)
    message = str(err.value)
    assert message.startswith("2 animated gif(s)")
    assert "posts/a/images/a.gif" in message
    assert "posts/b/images/b.gif" in message


def test_the_cli_reports_an_animation_error_without_a_traceback(
        monkeypatch, capsys):
    from medium_archive import cli

    def fail(args):
        raise sites.AnimationError("1 animated gif(s) could not be placed")
    monkeypatch.setattr(cli, "cmd_pelican", fail)
    monkeypatch.setattr("sys.argv", ["medium-archive", "pelican"])
    with pytest.raises(SystemExit) as exit_:
        cli.main()
    assert exit_.value.code == ("error: 1 animated gif(s) could not be "
                                "placed")


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"),
                    reason="ffmpeg not installed")
def test_every_animation_is_a_clip_even_where_it_costs_more(tmp_path):
    """A display copy is placed only when it undercuts what it replaces
    -- except a clip, where what it buys is the pause control WCAG
    2.2.2 asks for rather than the bytes. Noise is the one thing gif
    carries more cheaply than near-lossless video, so both of these cost
    more as clips, and both are placed as clips, the short one too."""
    import os

    from PIL import Image

    src = tmp_path / "src"
    src.mkdir()

    def noise_loop(name, frames):
        pictures = [Image.frombytes("RGB", (160, 120),
                                    os.urandom(160 * 120 * 3)).convert("P")
                    for _ in range(frames)]
        return animation(src / name, pictures, duration=200)

    long_loop = noise_loop("long.gif", 30)          # 6 s
    short_loop = noise_loop("short.gif", 10)        # 2 s
    placer = sites.ImagePlacer(tmp_path / "cache", {"images": {"video_crf": 1}})
    out = tmp_path / "out"
    out.mkdir()
    for loop in (long_loop, short_loop):
        clip = placer.place(loop, out / loop.name)
        assert clip.suffix == ".mp4"
        assert (clip.stat().st_size + sites.poster_path(clip).stat().st_size
                > loop.stat().st_size)           # placed for the controls


def test_video_size_scales_to_the_cap_only():
    assert sites.video_size((1600, 1200), 1104) == (1104, 828)
    assert sites.video_size((801, 603), 0) == (801, 603)
    assert sites.video_size((1000, 500), 1104) == (1000, 500)


def test_kept_frames_thin_bursts_and_keep_what_stays():
    """Frames of a burst faster than ~30 fps are dropped; the first, the
    last, and every frame on screen for 29.5 ms or more are kept, and a
    frame kept for its start gives way to a following long frame rather
    than showing for less than that."""
    assert sites.kept_frames([100, 100, 100]) == [0, 1, 2]
    # 10/20 ms alternating: one frame about every 30 ms, and the last
    assert sites.kept_frames([10, 20, 10, 20, 20, 10, 500]) == [0, 2, 4, 6]
    # a burst ending in a long frame: frame 3 (kept at 30 ms) would show
    # for 10 ms before the long frame 4, so 4 replaces it
    assert sites.kept_frames([10, 10, 10, 10, 800, 10]) == [0, 4, 5]
    assert sites.kept_frames([500]) == [0]


def test_select_frames_nests_its_runs():
    assert (sites.select_frames([0, 1, 2, 5, 7, 8])
            == "select='(between(n,0,2)+(between(n,5,5)+between(n,7,8)))'")
    # hundreds of runs stay shallow enough for ffmpeg's parser
    expr = sites.select_frames(list(range(0, 2000, 2)))
    depth = max(expr[:i].count("(") - expr[:i].count(")")
                for i in range(len(expr)))
    assert depth < 20


@pytest.mark.skipif(not __import__("shutil").which("ffprobe"),
                    reason="ffmpeg not installed")
def test_a_clip_caps_the_frame_rate_and_keeps_the_gifs_timing(tmp_path):
    """A gif recorded faster than ~30 fps loses the frames of its
    bursts in the clip, but every frame the clip keeps starts exactly
    when the gif shows it. (How long the last frame lasts is the
    muxer's guess on older ffmpeg, so only the starts are compared.)
    The gif's odd size is padded by a pixel for 4:2:0, in the clip and
    the poster alike."""
    import subprocess

    src = tmp_path / "src"
    src.mkdir()
    delays = [10, 20] * 12 + [400]
    gif = animation(src / "fast.gif",
                    [gradient_frame((201, 151), i * 9)
                     for i in range(len(delays))], duration=delays)
    placer = sites.ImagePlacer(tmp_path / "cache", {})
    out = tmp_path / "out"
    out.mkdir()
    clip = placer.place(gif, out / "fast.gif")
    assert clip.suffix == ".mp4"

    def probe(*args):
        return subprocess.run(["ffprobe", "-v", "error", *args, str(clip)],
                              capture_output=True, text=True,
                              check=True).stdout.split()
    starts = sorted(round(float(t) * 1000)
                    for t in probe("-select_streams", "v", "-show_entries",
                                   "packet=pts_time", "-of", "csv=p=0"))
    gif_starts = [sum(delays[:i]) for i in range(len(delays))]
    assert starts == [gif_starts[i] for i in sites.kept_frames(delays)]
    assert len(starts) < len(delays)
    assert probe("-select_streams", "v", "-show_entries",
                 "stream=width,height", "-of", "csv=p=0") == ["202,152"]
    from PIL import Image
    with Image.open(sites.poster_path(clip)) as im:
        assert im.size == (202, 152)


def test_clips_are_video_the_reader_controls(project):
    """A clip reaches both card themes as a <video>: its picture until
    it is played (the poster), its text alternative as the accessible
    name a <video> has no alt attribute for, controls to stop it with,
    and nothing fetched until it is asked for."""
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    partial = (hugo_site
               / "layouts/_partials/post-image.html").read_text()
    plugin = (pelican_site / "pelicanconf.py").read_text()
    assert 'strings.HasSuffix .src ".mp4"' in partial
    assert 'VIDEO_SUFFIXES = (".mp4",)' in plugin
    for source in (partial, plugin):
        assert "-poster.webp" in source
        assert "preload=\"none\"" in source
        assert "aria-label" in source
        assert "controls" in source
    # and the script that gives a gif's motion back where it is welcome
    # follows the article on both engines
    for page in (hugo_site / "layouts/page.html",
                 pelican_site / "theme/templates/article.html"):
        text = page.read_text()
        assert "prefers-reduced-motion: reduce" in text, page
        assert text.index("</article>") < text.index("IntersectionObserver"), page
        # a clip the reader pauses is not started again
        assert 'clip.dataset.held = "1"' in text, page


def test_multiple_authors_reach_both_sites(tmp_path):
    """A post's authors list, of any length, is the byline everywhere:
    hugo's authors taxonomy (front matter slugs only; the feed reads the
    same list, the card and post link each term's listing page),
    pelican's Authors: header, which it splits into
    Author objects -- on commas, so a name holding one flips the
    separator to semicolons."""
    manifest = {}
    make_post(tmp_path, manifest, "duet", "abc123abc123", "2020-01-01T00:00:00Z",
              "Hi.\n", authors=[{"name": "Ada Lovelace", "url": "https://medium.com/@ada"},
                                {"name": "yuvipanda", "url": None}])
    make_post(tmp_path, manifest, "trio", "abc123abc124", "2020-01-02T00:00:00Z",
              "Hi.\n", authors=[{"name": "Project Jupyter, Inc.", "url": None},
                                {"name": "Min RK", "url": None}])
    make_post(tmp_path, manifest, "solo", "abc123abc125", "2020-01-03T00:00:00Z",
              "Hi.\n", authors=[])
    manifest_json(tmp_path).write_text(json.dumps(manifest))
    write_site(tmp_path, {"title": "T"})

    site = build(hugo, tmp_path)
    front = lambda stem: post_front(site, stem)
    assert front("duet")["authors"] == ["ada-lovelace", "yuvipanda"]
    assert "author" not in front("duet")
    assert "authors" not in front("solo")
    assert "capitalizeListTitles = false" in hugo_config(site)
    # the feed, the card's byline and the post page's all walk the
    # taxonomy terms, so each shows the author's name, not the slug
    text = (site / "layouts/rss.xml").read_text()
    assert '.GetTerms "authors"' in text and ".Params.author" not in text
    for layout in ("layouts/_partials/card.html", "layouts/page.html"):
        text = (site / layout).read_text()
        assert '.GetTerms "authors"' in text and ".Params.author" not in text, layout
        assert 'href="{{ .RelPermalink }}">{{ .LinkTitle }}</a>' in text, layout

    site = build(pelican, tmp_path)
    assert post_front(site, "duet")["authors"] == ["ada-lovelace",
                                                   "yuvipanda"]
    # a slug holds no comma, so the reader's comma split is unambiguous
    # even for a byline like "Project Jupyter, Inc." that once forced
    # pelican's semicolon separator
    assert post_front(site, "trio")["authors"] == ["project-jupyter-inc",
                                                   "min-rk"]
    assert "authors" not in post_front(site, "solo")
    for tpl in ("article", "macros", "base"):
        text = (site / f"theme/templates/{tpl}.html").read_text()
        assert "article.authors" in text and "article.author " not in text \
            and "article.author." not in text and "article.author|" not in text, tpl
    for tpl in ("article", "macros"):
        text = (site / f"theme/templates/{tpl}.html").read_text()
        assert 'for a in article.authors' in text \
            and '<a href="{{ SITEURL }}/{{ a.url }}">{{ a }}</a>' in text, tpl


def test_first_image_loads_eagerly(project):
    """Every body image is lazy except the first, which is the one most
    likely on screen at load (WordPress's treatment of the first content
    image): the exporter names it, and each theme fetches it eagerly at
    high priority. A reference inside a code fence is not an image."""
    assert sites.first_image("text\n\n```\n![x](images/a.png)\n```\n"
                             "![y](images/b.png) and ![z](images/c.png)\n"
                             ) == "images/b.png"
    assert sites.first_image("no images\n") is None
    hugo_site = build(hugo, project)
    front = post_front(hugo_site, "second-post")
    assert front["first_image"] == "images/001-pic.png"
    first = post_front(hugo_site, "first-post")
    assert "first_image" not in first
    partial = (hugo_site / "layouts/_partials/post-image.html").read_text()
    assert ".page.Params.first_image" in partial
    assert 'fetchpriority="high"' in partial and 'loading="lazy"' in partial
    pelican_site = build(pelican, project)
    config = (pelican_site / "pelicanconf.py").read_text()
    assert "_prioritize_first_images" in config
    assert 'fetchpriority="high"' in config


def test_crawl_files(project):
    """What search engines ask for first: a sitemap and a robots.txt
    naming it (Hugo generates the sitemap itself; Pelican's plugin
    writes both), plus the redirect map as a `_redirects` file for hosts
    that turn one into HTTP 301s. The search page stays out of the
    index and the sitemap."""
    hugo_site = build(hugo, project)
    assert "enableRobotsTXT = true" in hugo_config(hugo_site)
    robots = (hugo_site / "layouts/robots.txt").read_text()
    assert '"sitemap.xml" | absURL' in robots and "Disallow: /" in robots
    search = page_front(hugo_site / "content/search.md")
    assert search["noindex"] is True and search["sitemap"] == {"disable": True}
    redirects = (hugo_site / "static/_redirects").read_text().splitlines()
    assert "/first-post-aaa111aaa111 /posts/2020/first-post/ 301" in redirects
    assert "/2015/06/01/first-post /posts/2020/first-post/ 301" in redirects
    assert "/p/bbb222bbb222 /posts/2021/second-post/ 301" in redirects
    assert all(line.endswith(" 301") for line in redirects)
    baseof = (hugo_site / "layouts/baseof.html").read_text()
    assert 'name="robots"' in baseof and "max-image-preview:large" in baseof
    assert "site.Params.noindex" in baseof and ".Params.noindex" in baseof

    pelican_site = build(pelican, project)
    config = (pelican_site / "pelicanconf.py").read_text()
    assert pelican_data(pelican_site)["noindex"] is False
    for name in ("_collect_sitemap", "_write_crawl_files", "sitemap.xml",
                 "robots.txt", '"_redirects"'):
        assert name in config, name
    base = (pelican_site / "theme/templates/base.html").read_text()
    assert 'name="robots"' in base and "max-image-preview:large" in base
    assert "NOINDEX or noindex" in base
    search = (pelican_site / "theme/templates/search.html").read_text()
    assert "{% set noindex = true %}" in search


def test_noindex_and_twitter_reach_both_sites(project):
    """site.toml's "noindex" keeps search engines off a deployment (a
    preview, which would otherwise be indexed as a copy of the real
    site); "twitter" credits the publication's handle on shared links."""
    cfg = read_site(project)
    cfg["noindex"] = True
    cfg["twitter"] = "@example"
    write_site(project, cfg)
    config = hugo_config(build(hugo, project))
    assert "noindex = true" in config and 'twitter = "@example"' in config
    data = pelican_data(build(pelican, project))
    assert data["noindex"] is True and data["twitter"] == "@example"


def test_page_metadata_search_engines_read(project):
    """What Medium's and WordPress's pages carry beyond the share tags:
    the post's own description, its modified date, its author by name
    and by page, structured data (a schema.org BlogPosting), and a
    canonical address that is the page's own -- page 2 of a listing
    included, which both engines would otherwise call page one."""
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    heads = {"hugo": (hugo_site / "layouts/baseof.html").read_text(),
             "pelican": (pelican_site / "theme/templates/base.html").read_text()}
    for engine, head in heads.items():
        for prop in ("article:modified_time", "article:author"):
            assert f'property="{prop}"' in head, (engine, prop)
        assert 'name="author"' in head, engine
        assert 'name="twitter:site"' in head, engine
    # structured data: one block, a BlogPosting, with the fields that
    # matter, and every value escaped for a <script>
    ld = (hugo_site / "layouts/_partials/jsonld.html").read_text()
    assert 'type="application/ld+json"' in ld
    pelican_ld = (pelican_site / "theme/templates/jsonld.html").read_text()
    assert 'type="application/ld+json"' in pelican_ld
    for key in ("BlogPosting", "headline", "datePublished", "dateModified",
                "author", "publisher", "mainEntityOfPage"):
        assert key in ld, key
    assert "jsonify | safeJS" in ld
    assert 'partial "jsonld.html"' in heads["hugo"]
    for key in ("BlogPosting", "headline", "datePublished", "dateModified",
                "author", "publisher", "mainEntityOfPage"):
        assert key in pelican_ld, key
    assert "|tojson }}" in pelican_ld
    assert '{% include "jsonld.html" %}' in heads["pelican"]
    # a post page's description is the post's, not the site's
    desc = next(line for line in heads["pelican"].splitlines()
                if 'name="description"' in line)
    assert "article.summary|striptags|e" in desc, desc
    # the address of the page being rendered: the listing's paginator
    # in hugo (one partial for the head and the list templates), the
    # output file in pelican
    assert 'partial "paginator.html"' in heads["hugo"]
    assert '<link rel="canonical" href="{{ or .Params.canonical $url }}">' in heads["hugo"]
    for layout in ("home.html", "section.html"):
        assert 'partial "paginator.html"' in (hugo_site / "layouts" / layout).read_text()
    assert "output_file" in heads["pelican"]
    assert ('<link rel="canonical" href="{{ article.canonical if article '
            'and article.canonical else page_url }}">') in heads["pelican"]


def test_hugo_article_metadata_and_feed_bylines(project):
    """The /posts/ and /posts/<year>/ sections share the posts' Type, so
    the article tags and the BlogPosting are limited to regular pages;
    and a feed names each author by the term's title, as pelican's
    feeds do, not by the slug in front matter."""
    hugo_site = build(hugo, project)
    baseof = (hugo_site / "layouts/baseof.html").read_text()
    assert '{{ $post := and .IsPage (eq .Type "posts") }}' in baseof
    ld = (hugo_site / "layouts/_partials/jsonld.html").read_text()
    assert '{{- if and $p.IsPage (eq $p.Type "posts") -}}' in ld
    rss = (hugo_site / "layouts/rss.xml").read_text()
    assert ('{{ range .GetTerms "authors" }}<dc:creator>{{ .LinkTitle }}'
            '</dc:creator>{{ end }}') in rss
    assert ".Params.authors" not in rss


def _graph_source(engine_site, engine):
    if engine == "hugo":
        return (engine_site / "layouts/_partials/jsonld.html").read_text()
    return (engine_site / "theme/templates/jsonld.html").read_text()


def test_external_canonical_reaches_the_head(project):
    """A post that declared a canonical on another host (Medium's
    "originally published at") is a copy of that page and says so, as a
    WordPress per-post canonical does; one naming the publication's own
    host (a Ghost-era slug) is the same post and is ignored. Every
    other page is its own canonical: the archive is the posts' home,
    and the Medium copy is never named as one."""
    gist = {"canonical_url": "https://gist.github.com/ada/1",
            "original_url": f"{BASE}/x-1"}
    own = {"canonical_url": f"{BASE}/old-slug", "original_url": f"{BASE}/x-1"}
    assert sites.canonical_for(gist) == "https://gist.github.com/ada/1"
    assert sites.canonical_for(own) is None
    assert sites.canonical_for({"canonical_url": None, "original_url": f"{BASE}/x-1"}) is None

    manifest = json.loads(manifest_json(project).read_text())
    url = next(u for u in manifest if "second-post" in u)
    manifest[url]["canonical_url"] = "https://gist.github.com/ada/1"
    manifest_json(project).write_text(json.dumps(manifest))
    hugo_site = build(hugo, project)
    second = post_front(hugo_site, "second-post")
    assert second["canonical"] == "https://gist.github.com/ada/1"
    first = post_front(hugo_site, "first-post")
    assert "canonical" not in first
    baseof = (hugo_site / "layouts/baseof.html").read_text()
    assert '<link rel="canonical" href="{{ or .Params.canonical $url }}">' in baseof
    pelican_site = build(pelican, project)
    assert post_front(pelican_site, "second-post")["canonical"] == \
        "https://gist.github.com/ada/1"
    assert "canonical" not in post_front(pelican_site, "first-post")
    base = (pelican_site / "theme/templates/base.html").read_text()
    assert 'href="{{ article.canonical if article and article.canonical else page_url }}"' in base
    # neither head knows the Medium address: a post without a declared
    # canonical (first-post above) is its own
    assert "original_url" not in baseof and "original_url" not in base


def test_share_image_stands_in_for_a_missing_cover(project):
    """site.toml "share_image": the og:image of every page without a
    cover of its own, so a listing or a coverless post still shares
    with a picture; both heads declare the image's dimensions, so
    Facebook renders the large card on the first share."""
    pytest.importorskip("PIL")
    from PIL import Image
    Image.new("RGB", (1200, 630)).save(site_asset(project, "share.png"))
    cfg = read_site(project)
    cfg["share_image"] = "share.png"
    write_site(project, cfg)
    hugo_site = build(hugo, project)
    assert 'share_image = "img/share.png"' in hugo_config(hugo_site)
    assert (hugo_site / "assets/img/share.png").is_file()   # readable dims
    baseof = (hugo_site / "layouts/baseof.html").read_text()
    assert 'with site.Params.share_image }}{{ with resources.Get .' in baseof
    for prop in ("og:image:width", "og:image:height"):
        assert f'property="{prop}"' in baseof, prop
    pelican_site = build(pelican, project)
    data = pelican_data(pelican_site)
    assert data["share_image"] == "theme/img/share.png"
    assert data["share_image_size"] == [1200, 630]
    assert data["cover_size"] == [640, 360]
    assert (pelican_site / "theme/static/img/share.png").is_file()
    base = (pelican_site / "theme/templates/base.html").read_text()
    assert "SHARE_IMAGE if SHARE_IMAGE" in base
    for prop in ("og:image:width", "og:image:height"):
        assert f'property="{prop}"' in base, prop
    # unset: no fallback, no size, nothing declared
    del cfg["share_image"]
    write_site(project, cfg)
    assert "share_image" not in hugo_params(build(hugo, project))
    data = pelican_data(build(pelican, project))
    assert "share_image" not in data and "share_image_size" not in data


def test_structured_data_graph(project):
    """Every page carries one schema.org graph, as WordPress's SEO
    plugins emit it: the Organization (publisher, with its profiles
    elsewhere as sameAs) and the WebSite (with the search page as its
    SearchAction), a BreadcrumbList placing the page, the post's
    BlogPosting with each author's Medium profile as sameAs, and an
    author page as a ProfilePage of that Person. The author profiles
    come from the bylines through one data file per site."""
    assert sites.site_profiles({"twitter": "@ex", "profiles": ["https://a.b/"]}) \
        == ["https://a.b/", "https://x.com/ex"]
    assert sites.site_profiles({}) == []
    manifest = json.loads(manifest_json(project).read_text())
    assert sites.author_entries(manifest) == {
        "ada-lovelace": {"name": "Ada Lovelace",
                         "url": "https://medium.com/@ada"}}
    cfg = read_site(project)
    cfg["twitter"] = "@example"
    cfg["profiles"] = ["https://github.com/example"]
    write_site(project, cfg)

    hugo_site = build(hugo, project)
    assert yaml.safe_load((hugo_site / "data/authors.yaml").read_text()) \
        == {"ada-lovelace": {"name": "Ada Lovelace",
                             "url": "https://medium.com/@ada"}}
    config = hugo_config(hugo_site)
    assert 'profiles = ["https://github.com/example", "https://x.com/example"]' in config
    # the graph on every page, not only posts
    baseof = (hugo_site / "layouts/baseof.html").read_text()
    assert '{{ end }}{{ partial "jsonld.html"' in baseof
    # the tag and author indexes are titled as the nav names them, which
    # the breadcrumbs repeat
    for plural, title in (("tags", "Tags"), ("authors", "Authors")):
        assert page_front(hugo_site / "content" / plural / "_index.md") \
            == {"title": title}
    pelican_site = build(pelican, project)
    assert pelican_data(pelican_site)["profiles"] == [
        "https://github.com/example", "https://x.com/example"]
    # the byline profiles are the same data file in both sites, read
    # into the pelican config as AUTHOR_LINKS
    assert yaml.safe_load((pelican_site / "data/authors.yaml").read_text()) \
        == {"ada-lovelace": {"name": "Ada Lovelace",
                             "url": "https://medium.com/@ada"}}
    # keyed by the slug the byline reaches either engine as, which is
    # what each site's structured data looks the profile up by
    assert config_namespace(pelican_site)["AUTHOR_LINKS"] \
        == {"ada-lovelace": "https://medium.com/@ada"}
    assert '{% include "jsonld.html" %}' in (pelican_site / "theme/templates/base.html").read_text()
    for engine, site in (("hugo", hugo_site), ("pelican", pelican_site)):
        src = _graph_source(site, engine)
        for key in ("@graph", "Organization", "WebSite", "SearchAction",
                    "search/?q={search_term_string}", "BreadcrumbList",
                    "ListItem", "BlogPosting", "ProfilePage", "sameAs",
                    "isPartOf", "ImageObject", "articleSection"):
            assert key in src, (engine, key)
    assert "hugo.Data.authors" in _graph_source(hugo_site, "hugo")
    assert "AUTHOR_LINKS" in _graph_source(pelican_site, "pelican")

    # the profile is looked up by the author's slug, which is what the
    # byline reaches either engine as and what data/authors.yaml is
    # keyed by -- a lookup by the rendered name would miss every author
    # whose name is not its own slug. Rendered, because that is the
    # only place a template's key is right or wrong.
    links = {"ada-lovelace": "https://medium.com/@ada"}
    author = _Term("Ada Lovelace", "authors")
    for page, context in (("article.html", {"article": SimpleNamespace(
                               title="First Post", url="posts/first-post/",
                               date=datetime.datetime(2020, 1, 5),
                               modified=None, locale_date="2020-01-05",
                               summary="Hello.", content="<p>Hi.</p>",
                               cover=None, tags=[_Term("example", "tags")],
                               authors=[author], related_posts=[])}),
                          ("author.html", {"author": author})):
        html = render_pelican_page(pelican_site, page, AUTHOR_LINKS=links,
                                   output_file="x/index.html", **context)
        assert '"sameAs": ["https://medium.com/@ada"]' in html, page


def test_related_posts(project):
    """Each post page closes with more posts, by shared tags, then
    author, then date: Hugo's related content, configured in the
    generated config; the pelican plugin scores the same way. The
    block is headed "More posts" in both sites -- the scoring guesses
    at a kinship from tags and bylines, so the heading claims none."""
    hugo_site = build(hugo, project)
    config = hugo_config(hugo_site)
    assert "[related]" in config and 'name = "tags"' in config
    assert 'partial "related.html"' in (hugo_site / "layouts/page.html").read_text()
    related = (hugo_site / "layouts/_partials/related.html").read_text()
    assert '(where site.RegularPages "Type" "posts").Related' in related and 'partial "card.html"' in related
    assert "| first 3 }}" in related     # three, the width of the home page's card rows
    pelican_site = build(pelican, project)
    article = (pelican_site / "theme/templates/article.html").read_text()
    assert "article.related_posts" in article
    # the same neutral heading in both sites: the scoring only guesses
    # at a kinship, so the heading does not head them "Related posts"
    for markup in (related, article):
        assert '<h2 class="page-title">More posts</h2>' in markup
        assert '"page-title">Related posts' not in markup
    namespace = config_namespace(pelican_site)
    from datetime import datetime
    day = lambda n: datetime(2020, 1, n)
    tag = lambda s: SimpleNamespace(slug=s)
    author = lambda n: SimpleNamespace(name=n)
    a = SimpleNamespace(tags=[tag("x"), tag("y")], authors=[author("Ada")], date=day(1))
    b = SimpleNamespace(tags=[tag("x")], authors=[author("Bob")], date=day(2))
    c = SimpleNamespace(tags=[tag("x"), tag("y")], authors=[author("Bob")], date=day(9))
    d = SimpleNamespace(tags=[tag("z")], authors=[author("Ada")], date=day(3))
    e = SimpleNamespace(tags=[tag("z")], authors=[author("Eve")], date=day(4))
    f = SimpleNamespace(tags=[tag("y")], authors=[author("Fay")], date=day(5))
    got = namespace["related_posts"](a, [a, b, c, d, e])
    assert got == [c, b, d]          # two tags, one tag, shared author; not e
    # three at most, the width of the home page's card rows: d's shared
    # author loses its place to f's shared tag
    assert namespace["related_posts"](a, [a, b, c, d, e, f]) == [c, b, f]
    assert namespace["related_posts"](e, [a, b, c, d, e]) == [d]
    assert namespace["related_posts"](a, [a, b, c, d, e], limit=1) == [c]


def test_alt_text_falls_back_to_the_caption():
    """An image with no alt inside a captioned figure takes the
    caption's plain text as its alt in both sites: most Medium images
    carry none, while the caption describes them exactly."""
    assert sites.caption_text("The [dashboard](https://x.y) *running*, **now**") \
        == "The dashboard running, now"
    assert sites.caption_text("a * b = 5*3 and snake_case <br> x") == "a * b = 5*3 and snake_case x"
    shell = ("<figure>\n\n![](images/1.png)\n\n<figcaption>\n\nA [chart](https://x.y) of *it*"
             "\n\n</figcaption>\n\n</figure>")
    assert 'alt="A chart of it"' in hugo.figure_shortcodes(shell)
    assert 'alt="A chart of it"' in pelican.figure_directives(shell)
    given = shell.replace("![]", "![Given]")
    assert 'alt="Given"' in hugo.figure_shortcodes(given)
    assert 'alt="Given"' in pelican.figure_directives(given)


def test_intro_reaches_both_landing_pages(project):
    """site.toml's "intro" is the landing-page blurb, and belongs on
    both preferred targets. Hugo renders it from content/_index.md; the
    pelican config renders the same Markdown (jinja has no Markdown
    filter of its own) and index.html emits it into the same .intro
    block the shared stylesheet already styles."""
    hugo_site = build(hugo, project)
    pelican_site = build(pelican, project)
    assert "Welcome." in (hugo_site / "content" / "_index.md").read_text()
    assert pelican_data(pelican_site)["intro"] == "Welcome."
    index = (pelican_site / "theme/templates/index.html").read_text()
    assert 'class="intro"' in index and "INTRO" in index


def test_intro_absent_leaves_a_valid_config(project):
    """No intro is a null in the site's data, and the config reads it
    back as no blurb rather than the string "None"."""
    cfg = read_site(project)
    del cfg["intro"]
    write_site(project, cfg)
    site = build(pelican, project)
    assert "intro" not in pelican_data(site)
    assert config_namespace(site)["INTRO"] is None


def test_hugo_cards_show_the_curated_description(project):
    """A card's excerpt is the description convert writes, which is what
    the pelican card renders. Hugo's .Summary is its own auto-summary of
    the body, so leaving it first would show a post's opening sentence
    on the card while the other engine showed the subtitle."""
    card = (build(hugo, project)
            / "layouts/_partials/card.html").read_text()
    assert "or .Description .Summary" in card


def test_body_images_are_marked_where_only_a_body_image_can_be(project):
    """The post-build pass has to run on the finished HTML: it needs
    output paths to encode variants beside, and pelican leaves {attach}
    unresolved until then (and never resolves it inside a srcset). By
    then a body image and one the theme rendered are the same markup,
    and no path rule separates them -- a related-post card points into
    another post's own images/ directory, exactly where that post's
    body images live. So the distinction is recorded upstream, in the
    reader, which is the counterpart of the hugo theme's render hook:
    every image the reader renders is in an article's body, and a card
    the theme renders never passes through it. The pass keys off the
    mark and strips it, so no reader sees it."""
    site = build(pelican, project)
    namespace, md = config_parser(site)
    assert namespace["BODY_IMAGE_ATTR"] == "data-body-image"
    # the path rule this replaced must not creep back: it is what took
    # a card's cover.jpg for a body image
    assert "ARTICLE_IMG" not in namespace and "VARIANT_IMG" not in namespace

    # one definition of the marker in the generated file, which the
    # reader half takes from the plugin half appended after it
    config = (site / "pelicanconf.py").read_text()
    assert config.count("BODY_IMAGE_ATTR = ") == 1

    # the reader really marks what an article's body holds, both the
    # images written as Markdown and the one a figure directive names
    html = md.render("Text.\n\n![pic](images/001-pic.png)\n")
    assert 'data-body-image=""' in html and 'loading="lazy"' in html
    figure = md.render('::: figure src="images/a.png" alt="A"\nCap.\n:::\n')
    assert 'data-body-image=""' in figure and 'loading="lazy"' in figure

    # and the pass takes the mark off whichever way it returns a tag
    # exactly one way out keeps the tag as it stands -- the one for a
    # tag with no mark, which is the theme's; every other return is of
    # the marker-stripped `bare`
    assert "bare = marker_re.sub(" in config
    assert config.count("return tag") == 1

    # an alt holding a ">" (a caption naming a <code> span) must not cut
    # the tag short: that would leave the image unprocessed, its mark on
    tag_re = re.compile(namespace["IMG_TAG"])
    tricky = ('<img alt="a <code>x</code> span" src="/posts/p/images/1.jpg"'
              ' loading="lazy" data-body-image="">')
    assert tag_re.search(tricky).group(0) == tricky


def test_author_slugs_are_clean_and_shared_by_both_sites(tmp_path):
    """A byline is a person's name, not a slug, so left as the term it
    would reach each generator raw: hugo puts a name's accents and
    punctuation straight into the path it builds, while pelican folds
    the same name to ASCII, and one author ends up at two addresses.
    Both exporters therefore write the slug, as they already do for
    tags, and each carries the name separately for rendering."""
    hard = [("Frédéric Collonval", "frederic-collonval"),
            ("Michał Krassowski", "michal-krassowski"),
            ("C.A.M. Gerlach", "cam-gerlach"),
            ("Matt McCormick @thewtex@fosstodon.org",
             "matt-mccormick-thewtexfosstodonorg"),
            ("Joe Lucas ", "joe-lucas")]
    for name, slug in hard:
        assert sites.author_slug(name) == slug, name
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug), slug

    manifest = {}
    for i, (name, _slug) in enumerate(hard):
        make_post(tmp_path, manifest, f"post-{i}", f"abc123abc12{i}",
                  f"2020-01-0{i + 1}T00:00:00Z", "Hi.\n",
                  authors=[{"name": name, "url": None}])
    manifest_json(tmp_path).write_text(json.dumps(manifest))
    write_site(tmp_path, {"title": "T"})
    slugs = [slug for _name, slug in hard]

    # the map both sites are named from: slug -> the byline it shows
    assert sites.author_entries(manifest) == {s: {"name": n} for n, s in hard}

    hugo_site = build(hugo, tmp_path)
    front = lambda stem: post_front(hugo_site, stem)
    assert [front(f"post-{i}")["authors"][0] for i in range(len(hard))] == slugs
    # the term pages come from that map, so the path stays the slug
    # while the title carries the name
    names = yaml.safe_load((hugo_site / "data/authors.yaml").read_text())
    assert names == {s: {"name": n} for n, s in hard}
    adapter = (hugo_site / "content/authors/_content.gotmpl").read_text()
    assert "hugo.Data.authors" in adapter and '"kind" "term"' in adapter

    pelican_site = build(pelican, tmp_path)
    assert [post_front(pelican_site, f"post-{i}")["authors"][0]
            for i in range(len(hard))] == slugs
    # the same map, as the same data file the hugo site got, read back
    # by the config
    assert yaml.safe_load((pelican_site / "data/authors.yaml").read_text()) \
        == {s: {"name": n} for n, s in hard}
    namespace = config_namespace(pelican_site)
    assert namespace["AUTHOR_DISPLAY"] == {s: n for n, s in hard}

    # the plugin names the Author objects, as it does the tags: one
    # object per slug, the slug untouched, the name the one shown
    articles = [SimpleNamespace(authors=[_FakeTag(s)]) for s in slugs]
    generator = SimpleNamespace(
        authors=[(a.authors[0], [a]) for a in articles], articles=articles,
        translations=[], hidden_articles=[], hidden_translations=[],
        drafts=[], drafts_translations=[])
    namespace["_name_authors"](generator)
    assert [str(a.authors[0]) for a in articles] == [n for n, _s in hard]
    assert [a.authors[0].slug for a in articles] == slugs


def test_hugo_does_not_publish_the_posts_section_page(project):
    """content/posts/ is a Hugo section, so Hugo would publish a list
    page and a feed for it unasked: /posts/ is the home listing over
    again, pagination and all, canonical to itself and in the sitemap
    while nothing links to it. Pelican has no sections -- posts/<slug>/
    is only a URL pattern there -- so dropping the page is also what
    keeps the two sites' address spaces the same. The posts themselves
    stay exactly where they were."""
    site = build(hugo, project)
    section = page_front(site / "content/posts/_index.md")
    assert section["build"] == {"render": "never", "list": "never"}
    # the posts are untouched: the section's own page is all that goes
    assert (site / "content/posts/2021/second-post/index.md").exists()
    assert (site / "content/posts/2020/first-post/index.md").exists()


def test_figure_alt_text_cannot_end_its_own_tag(project):
    """An alt is prose, and prose holds characters that end an HTML tag
    for anything reading it with a regex: a literal ">" ("File -> Hub
    Control Panel"), seen in the archive, ends the img tag early for
    pelican's own intra-site link pass, which then leaves the {attach}
    in src unresolved and the image broken on the page. A Markdown code
    span in an alt is the same problem arrived at from the other
    direction, so both exporters carry the caption's plain text.

    The alt now crosses two boundaries, and each escapes for its own:
    the exporter quotes it for the directive's argument line (the same
    quoting the hugo shortcode call uses, undone by the reader's
    shlex), and the reader escapes it for the attribute it writes."""
    arrow = ("<figure>\n\n![File -> Hub Control Panel](images/a.png)\n\n"
             "<figcaption>\n\nCap.\n\n</figcaption>\n\n</figure>")
    directive = pelican.figure_directives(arrow)
    assert 'alt="File -> Hub Control Panel"' in directive

    code = ("<figure>\n\n![a `p5.js` kernel](images/a.png)\n\n"
            "<figcaption>\n\nCap.\n\n</figcaption>\n\n</figure>")
    assert 'alt="a p5.js kernel"' in pelican.figure_directives(code)
    assert 'alt="a p5.js kernel"' in hugo.figure_shortcodes(code)

    # a quote in an alt would end the argument, so it is escaped for the
    # directive line and comes back whole from the reader's parse
    quoted = ("<figure>\n\n![the \"run\" button](images/a.png)\n\n"
              "<figcaption>\n\nCap.\n\n</figcaption>\n\n</figure>")
    assert r'alt="the \"run\" button"' in pelican.figure_directives(quoted)

    namespace, md = config_parser(build(pelican, project))
    tag_re = re.compile(namespace["IMG_TAG"])
    for shell in (arrow, quoted):
        html = md.render(pelican.figure_directives(shell))
        alt = re.search(r'alt="([^"]*)"', html).group(1)
        assert ">" not in alt and "<" not in alt
        img = tag_re.search(html)
        # the tag the post-build pass will read is the whole tag
        assert img.group(0).endswith('data-body-image="">')


def set_redirects(project, mode):
    """site.toml's "redirects" set to one mode, for the exporters to read."""
    cfg = read_site(project)
    if mode is None:
        cfg.pop("redirects", None)
    else:
        cfg["redirects"] = mode
    write_site(project, cfg)


def run_pelican_redirects(site, tmp_path):
    """The generated config's redirect pass, run as a build runs it:
    the site's own redirects.csv into an empty output directory. The
    file it wrote (or None) and the stub paths it wrote."""
    namespace = config_namespace(site)
    output = tmp_path / "output"
    output.mkdir()
    namespace["_write_redirects"](SimpleNamespace(output_path=str(output)))
    stubs = sorted(str(p.parent.relative_to(output))
                   for p in output.rglob("index.html"))
    rules = (output / "_redirects")
    return (rules.read_text() if rules.exists() else None), stubs


def test_redirects_default_to_both_mechanisms(project, tmp_path):
    """Unset, "redirects" leaves both mechanisms in place: the stub
    pages every static host serves, and the `_redirects` file the hosts
    that read one answer with a real 301. That is the default because
    it is the only setting that redirects an old link on a host nobody
    has chosen yet."""
    set_redirects(project, None)
    hugo_site = build(hugo, project)
    assert post_front(hugo_site, "first-post")["aliases"] == [
        "/first-post-aaa111aaa111", "/p/aaa111aaa111", "/2015/06/01/first-post"]
    assert (hugo_site / "static/_redirects").exists()

    pelican_site = build(pelican, project)
    rules, stubs = run_pelican_redirects(pelican_site, tmp_path)
    assert "/p/aaa111aaa111 /posts/2020/first-post/ 301" in rules
    assert "p/aaa111aaa111" in stubs


def test_redirects_stubs_only_leaves_no_redirects_file(project, tmp_path):
    """"stubs" is what a GitHub Pages deployment wants: it never reads
    `_redirects`, so the file is inert weight there and the stub pages
    are the whole mechanism."""
    set_redirects(project, "stubs")
    hugo_site = build(hugo, project)
    assert "aliases" in post_front(hugo_site, "first-post")
    assert not (hugo_site / "static/_redirects").exists()

    pelican_site = build(pelican, project)
    assert config_namespace(pelican_site)["REDIRECT_FILE"] is False
    rules, stubs = run_pelican_redirects(pelican_site, tmp_path)
    assert rules is None
    assert "p/aaa111aaa111" in stubs


def test_redirects_file_only_leaves_no_stub_pages(project, tmp_path):
    """"file" is what a Netlify or Cloudflare Pages deployment wants: a
    real HTTP 301 from one text file, and none of the hundreds of stub
    directories the site root would otherwise carry -- which on Netlify
    would shadow the rules and answer in their place."""
    set_redirects(project, "file")
    hugo_site = build(hugo, project)
    assert "aliases" not in post_front(hugo_site, "first-post")
    assert "/p/aaa111aaa111 /posts/2020/first-post/ 301" in (
        hugo_site / "static/_redirects").read_text()

    pelican_site = build(pelican, project)
    assert config_namespace(pelican_site)["REDIRECT_STUBS"] is False
    rules, stubs = run_pelican_redirects(pelican_site, tmp_path)
    assert "/p/aaa111aaa111 /posts/2020/first-post/ 301" in rules
    assert stubs == []


def test_redirects_none_still_writes_the_map(project, tmp_path):
    """"none" leaves the redirects to a rule set kept somewhere else --
    and still writes redirects.csv, which is what such a rule set is
    built from."""
    set_redirects(project, "none")
    hugo_site = build(hugo, project)
    assert "aliases" not in post_front(hugo_site, "first-post")
    assert not (hugo_site / "static/_redirects").exists()
    assert (hugo_site / "redirects.csv").exists()

    pelican_site = build(pelican, project)
    assert (pelican_site / "redirects.csv").exists()
    rules, stubs = run_pelican_redirects(pelican_site, tmp_path)
    assert rules is None and stubs == []


def test_an_unknown_redirects_value_is_reported_and_ignored(project, capsys):
    """A typo must not silently drop every redirect the site serves."""
    set_redirects(project, "netlify")
    hugo_site = build(hugo, project)
    assert "'netlify' is not one of" in capsys.readouterr().err
    assert "aliases" in post_front(hugo_site, "first-post")
    assert (hugo_site / "static/_redirects").exists()
