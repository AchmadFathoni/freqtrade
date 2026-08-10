"""Build an EPUB from an mkdocs project, preserving Calibre highlights across rebuilds.

Calibre stores highlights/notes in its library database, keyed by (book, file
path inside the epub, character offset) — not inside the epub file itself. A
rebuild only keeps them if unchanged chapters produce byte-identical files.
This script therefore:

  * renders the site with `mkdocs build` into a temp dir,
  * extracts each nav page's content into its own xhtml file,
  * reuses the previous build's chapter filename whenever the content hash
    matches (also covers renamed pages), otherwise writes a fresh slug name,
  * packs everything into a deterministic EPUB (fixed UUID, timestamps, entry
    order) so unchanged chapters are byte-identical to the previous build.

Deleted pages drop their file; their highlights become unlocatable in Calibre
and are cleaned up manually in the viewer's Highlights panel.
"""

import argparse
import hashlib
import json
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path
from urllib.parse import unquote
from xml.sax.saxutils import escape

from bs4 import BeautifulSoup, Comment
from mkdocs.config import load_config
from mkdocs.structure.files import File
from pygments.formatters import HtmlFormatter


FIXED_TIME = (2000, 1, 1, 0, 0, 0)
BOOK_UUID = uuid.uuid5(uuid.NAMESPACE_URL, "https://www.freqtrade.io/epub/docs")

BASE_CSS = """
body { margin: 1em; font-family: Georgia, 'Times New Roman', serif; line-height: 1.5; }
h1, h2, h3, h4 { font-family: sans-serif; line-height: 1.25; }
h1 { border-bottom: 1px solid #ccc; padding-bottom: .2em; }
a { color: #0366d6; text-decoration: none; }
img { max-width: 100%; }
pre, code { font-family: 'DejaVu Sans Mono', monospace; }
pre {
  white-space: pre-wrap; font-size: .85em; background: #f6f8fa;
  padding: .6em; overflow-wrap: break-word;
}
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th, td { border: 1px solid #ddd; padding: .35em .6em; font-size: .9em; }
th { background: #f6f8fa; }
blockquote { border-left: .25em solid #dfe2e5; margin-left: 0; padding-left: 1em; color: #555; }
sup, sub { font-size: .75em; }
.admonition {
  border: 1px solid #ccc; border-left-width: .3em; border-radius: .2em;
  padding: .6em 1em; margin: 1em 0;
}
.admonition > .admonition-title { font-weight: bold; margin: 0 0 .4em; }
.admonition.note, .admonition.info, .admonition.tip, .admonition.success,
.admonition.question, .admonition.example, .admonition.abstract { border-left-color: #448aff; }
.admonition.warning, .admonition.caution, .admonition.attention,
.admonition.important { border-left-color: #ff9100; }
.admonition.danger, .admonition.error { border-left-color: #ff1744; }
details { border: 1px solid #ccc; border-left-width: .3em; padding: .6em 1em; margin: 1em 0; }
summary { font-weight: bold; cursor: pointer; }
.tabbed-set { border: 1px solid #ddd; margin: 1em 0; }
.tabbed-set > .tabbed-content { padding: .6em 1em; }
.task-list-item { list-style: none; }
.task-list-item input { margin-right: .4em; }
.footnote { font-size: .85em; color: #555; }
.footnote hr { border: none; border-top: 1px solid #ccc; }
.md-annotation { font-size: .7em; vertical-align: super; color: #448aff; }
"""

XHTML_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en">
<head>
<meta charset="utf-8"/>
<title>{title}</title>
<link rel="stylesheet" type="text/css" href="css/style.css"/>
</head>
<body>
{content}
</body>
</html>
"""

CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def chapter_name(md: str) -> str:
    """Stable chapter filename for a source doc path, e.g. freqai/foo.md -> freqai-foo.xhtml."""
    p = Path(md)
    stem = "home" if p.name == "index.md" else p.stem
    slug = f"{p.parent}/{stem}" if p.parent.as_posix() != "." else stem
    return f"{slug.replace('/', '-')}.xhtml"


def flatten_nav(nav) -> list[tuple[str, str]]:
    """Ordered (title, source path) pairs from mkdocs config nav."""
    pages = []
    for entry in nav:
        if isinstance(entry, dict):
            for title, value in entry.items():
                if isinstance(value, str):
                    pages.append((title, value))
                elif isinstance(value, list):
                    pages.extend(flatten_nav(value))
    return pages


def _clean_images(art, page_url: str, site_dir: Path, images: dict) -> None:
    for img in art.find_all("img"):
        if getattr(img, "attrs", None) is None:
            continue  # already decomposed (nested img tags)
        src = img.get("src", "")
        if not src or src.startswith("data:"):
            continue
        if src.startswith(("http://", "https://")):
            img.decompose()  # badges/remote icons, broken offline
            continue
        if src.endswith("#only-dark") and any(
            j is not img
            and j.get("alt") == img.get("alt")
            and j.get("src", "").endswith("#only-light")
            for j in art.find_all("img")
        ):
            img.unwrap()  # drop the dark variant; unwrap keeps its nested light twin
            continue
        path, _ = src.split("#", 1) if "#" in src else (src, "")
        resolved = posixpath.normpath(posixpath.join(page_url, unquote(path)))
        if resolved.startswith(("..", "/")):
            img.decompose()
        elif (site_dir / resolved).is_file():
            name = images.setdefault(resolved, Path(resolved).name)
            img["src"] = f"images/{name}"
        else:
            img.decompose()


def _clean_links(art, page_url: str, page_map: dict) -> None:
    for a in art.find_all("a"):
        href = a.get("href", "")
        if not href or href.startswith(("#", "http://", "https://", "mailto:", "tel:")):
            continue
        path, frag = href.split("#", 1) if "#" in href else (href, "")
        resolved = posixpath.normpath(posixpath.join(page_url, unquote(path))).rstrip("/")
        if resolved in (".", ""):
            resolved = "home"
        if resolved.startswith(("..", "/")) or resolved not in page_map:
            a.unwrap()
        else:
            a["href"] = page_map[resolved] + (f"#{frag}" if frag else "")


def _drop_leftover_chrome(art) -> None:
    """Remove HTML comments and empty wrappers left by removed badges/buttons."""
    for comment in art.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for tag in art.find_all(["a", "p"]):
        if tag.get_text(strip=True) or tag.find("img"):
            continue
        tag.decompose()


def clean_article(art, page_url: str, site_dir: Path, page_map: dict, images: dict) -> None:
    """Strip site chrome, rewrite links/images so the article is self-contained for EPUB."""
    for tag in art.find_all(["script", "iframe"]):
        tag.decompose()
    for tag in art.find_all("a", class_="md-content__button"):
        tag.decompose()
    for tag in art.find_all("a", class_=lambda c: c and "md-button" in c):
        tag.decompose()  # web CTA buttons (Star/Fork/...), meaningless offline
    for tag in art.find_all("a", class_="headerlink"):
        tag.decompose()
    for hr in art.find_all("hr", recursive=False):
        hr.decompose()
    for details in art.find_all("details"):
        details["open"] = "open"

    _clean_images(art, page_url, site_dir, images)
    _clean_links(art, page_url, page_map)
    _drop_leftover_chrome(art)


def render_chapter(
    title: str,
    md: str,
    site_dir: Path,
    docs_dir: Path,
    use_directory_urls: bool,
    page_map: dict,
    images: dict,
) -> bytes:
    f = File(md, str(docs_dir), str(site_dir), use_directory_urls)
    soup = BeautifulSoup((site_dir / f.dest_path).read_text(encoding="utf-8"), "html.parser")
    art = soup.find("article", class_="md-content__inner")
    if art is None:
        raise RuntimeError(f"No article content found in {f.dest_path}")
    page_url = f.url.rstrip("/")
    clean_article(art, page_url, site_dir, page_map, images)
    content = "".join(str(c) for c in art.contents)
    return XHTML_TEMPLATE.format(title=title, content=content).encode("utf-8")


def _first_page(value) -> str | None:
    for sub in value:
        if isinstance(sub, dict):
            for _, v in sub.items():
                if isinstance(v, str):
                    return chapter_name(v)
                if isinstance(v, list):
                    if p := _first_page(v):
                        return p
    return None


def _nav_items(nav) -> str:
    lis = []
    for entry in nav:
        if isinstance(entry, dict):
            for title, value in entry.items():
                if isinstance(value, str):
                    lis.append(f'<li><a href="{chapter_name(value)}">{title}</a></li>')
                elif isinstance(value, list):
                    first = _first_page(value)
                    href = f'<a href="{first}">{title}</a>' if first else f"<span>{title}</span>"
                    lis.append(f"<li>{href}<ol>{_nav_items(value)}</ol></li>")
        elif isinstance(entry, str):
            lis.append(f"<li><span>{entry}</span></li>")
    return "".join(lis)


def build_nav_xhtml(nav) -> bytes:
    nav_html = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">
<head>
<meta charset="utf-8"/>
<title>Contents</title>
</head>
<body>
<nav epub:type="toc">
<h1>Contents</h1>
<ol>{_nav_items(nav)}</ol>
</nav>
</body>
</html>
"""
    return nav_html.encode("utf-8")


def build_cover_xhtml(title: str) -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="en">
<head>
<meta charset="utf-8"/>
<title>Cover</title>
<link rel="stylesheet" type="text/css" href="css/style.css"/>
</head>
<body>
<img alt="{escape(title)}" src="images/cover.png" style="display:block; margin:15% auto 0;"/>
</body>
</html>
""".encode()


def docs_modified_date(docs_dir: Path) -> str:
    """Last commit date of the docs dir (deterministic per commit), fallback fixed date."""
    proc = subprocess.run(
        ["git", "log", "-1", "--format=%cs", "--", str(docs_dir)],
        capture_output=True,
        text=True,
    )
    return (proc.stdout.strip() or "2000-01-01") if proc.returncode == 0 else "2000-01-01"


_BUMP_RE = re.compile(r"bump version to \d+\.\d+(?:\.\d+)?$", re.IGNORECASE)


def _bump_date(log: str) -> str | None:
    """Date of the newest `bump version to X.Y` commit (releases, not `-dev` bumps)."""
    for line in log.splitlines():
        date, _, subject = line.partition("\x00")
        if _BUMP_RE.search(subject):
            return date
    return None


def release_date(docs_dir: Path) -> str:
    """Date of the latest release: git tag, else 'bump version' commit, else docs date."""
    tag = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0"], capture_output=True, text=True
    )
    if tag.returncode == 0 and tag.stdout.strip():
        date = subprocess.run(
            ["git", "log", "-1", "--format=%cs", tag.stdout.strip()],
            capture_output=True,
            text=True,
        )
        if date.returncode == 0 and date.stdout.strip():
            return date.stdout.strip()
    log = subprocess.run(["git", "log", "--format=%cs%x00%s"], capture_output=True, text=True)
    if log.returncode == 0:
        if date := _bump_date(log.stdout):
            return date
    return docs_modified_date(docs_dir)


def build_opf(chapter_files: list[tuple[str, str]], image_files: list[str], meta: dict) -> bytes:
    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="css" href="css/style.css" media-type="text/css"/>',
    ]
    spine = []
    if meta.get("cover"):
        manifest.append('<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>')
        manifest.append(
            '<item id="cover-img" href="images/cover.png" media-type="image/png" '
            'properties="cover-image"/>'
        )
        spine.append('<itemref idref="cover"/>')
    for i, (title, name) in enumerate(chapter_files):
        manifest.append(f'<item id="ch-{i}" href="{name}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="ch-{i}"/>')
    for i, name in enumerate(image_files):
        ext = Path(name).suffix.lower().lstrip(".")
        media = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "svg": "image/svg+xml",
            "webp": "image/webp",
        }.get(ext, "application/octet-stream")
        manifest.append(f'<item id="img-{i}" href="{name}" media-type="{media}"/>')
    subjects = "".join(f"<dc:subject>{escape(s)}</dc:subject>" for s in meta["tags"])
    modified = f"{meta['modified']}T00:00:00Z"
    cover_meta = '<meta name="cover" content="cover-img"/>' if meta.get("cover") else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0"
         unique-identifier="pub-id" xml:lang="en">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="pub-id">urn:uuid:{BOOK_UUID}</dc:identifier>
    <dc:title>{escape(meta["title"])}</dc:title>
    <dc:language>{meta["language"]}</dc:language>
    <dc:creator>{escape(meta["creator"])}</dc:creator>
    <dc:publisher>{escape(meta["publisher"])}</dc:publisher>
    <dc:description>{escape(meta["description"])}</dc:description>
    <dc:rights>{escape(meta["rights"])}</dc:rights>
    <dc:date>{meta["modified"]}</dc:date>
    {subjects}
    <meta property="dcterms:modified">{modified}</meta>
    {cover_meta}
  </metadata>
  <manifest>
    {chr(10).join(manifest)}
  </manifest>
  <spine>
    {chr(10).join(spine)}
  </spine>
</package>
""".encode()


def write_epub(output: Path, files: list[tuple[str, bytes]]) -> None:
    """Deterministic zip: fixed timestamps, sorted entries, mimetype stored first."""
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        zi = zipfile.ZipInfo("mimetype", FIXED_TIME)
        zi.compress_type = zipfile.ZIP_STORED
        z.writestr(zi, b"application/epub+zip")
        for name, data in sorted(files, key=lambda x: x[0]):
            z.writestr(zipfile.ZipInfo(name, FIXED_TIME), data)


def pick_cover(cover: Path | None, no_cover: bool, docs_dir: Path) -> Path | None:
    if no_cover:
        return None
    candidate = cover or Path(docs_dir) / "images" / "logo.png"
    return candidate if candidate.is_file() else None


def build_meta(
    cfg,
    docs_dir: Path,
    creator: str,
    publisher: str,
    rights: str,
    tags: str,
    cover_file: Path | None,
) -> tuple[dict, list[tuple[str, bytes]]]:
    meta = {
        "title": cfg["site_name"],
        "language": str(cfg["theme"].get("language", "en")),
        "creator": creator,
        "publisher": publisher,
        "description": str(cfg.get("site_description", "")),
        "rights": rights,
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "modified": release_date(docs_dir),
        "cover": cover_file is not None,
    }
    cover_files = []
    if cover_file:
        cover_files = [
            ("OEBPS/cover.xhtml", build_cover_xhtml(meta["title"])),
            ("OEBPS/images/cover.png", cover_file.read_bytes()),
        ]
    return meta, cover_files


def default_library() -> Path | None:
    """Calibre GUI's configured library (global.py.json), else ~/Calibre Library."""
    config = Path.home() / ".config" / "calibre" / "global.py.json"
    try:
        lib = json.loads(config.read_text()).get("library_path")
        if lib:
            return Path(lib)
    except (OSError, ValueError):
        pass
    home_lib = Path.home() / "Calibre Library"
    return home_lib if home_lib.is_dir() else None


CALIBRE_SERVICE = "calibre-server"


def _service_active() -> bool:
    proc = subprocess.run(
        ["systemctl", "is-active", "--quiet", CALIBRE_SERVICE], capture_output=True
    )
    return proc.returncode == 0


def _control_service(action: str) -> bool:
    proc = subprocess.run(["sudo", "systemctl", action, CALIBRE_SERVICE])
    return proc.returncode == 0


def _running_cmdlines(procdir: Path) -> list[str]:
    """Decoded command lines from a /proc-style dir (testable)."""
    cmdlines = []
    for proc in procdir.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmdlines.append((proc / "cmdline").read_bytes().replace(b"\x00", b" ").decode())
        except OSError:
            continue
    return cmdlines


def calibre_gui_running(procdir: Path = Path("/proc")) -> bool:
    """True if the Calibre GUI or its ebook viewer is open (they lock the library).

    Matches the nix wrapper scripts (the actual process is python3.x).
    """
    for cmdline in _running_cmdlines(procdir):
        if "/.calibre-wrapped" in cmdline or "/.ebook-viewer-wrapped" in cmdline:
            return True
    return False


def update_calibre(library: Path, epub: Path, title: str) -> int:
    """Replace the EPUB format on the matching Calibre book and refresh its metadata."""

    def run(*args: str) -> str:
        proc = subprocess.run(
            ["calibredb", "--library-path", str(library), *args],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"calibredb {' '.join(args[:2])} failed:\n{proc.stderr or proc.stdout}"
            )
        return proc.stdout

    ids = [int(i) for i in run("search", f"title:{title}").split() if i.isdigit()]
    if not ids:
        raise RuntimeError(f"No book titled '{title}' in {library}; add it to Calibre first.")
    if len(ids) > 1:
        raise RuntimeError(f"Multiple books titled '{title}' in {library}: ids {ids}.")
    book_id = ids[0]
    run("add_format", str(book_id), str(epub))
    with zipfile.ZipFile(epub) as z:
        opf = z.read("OEBPS/content.opf")
    opf_path = Path(tempfile.mkstemp(suffix=".opf")[1])
    try:
        opf_path.write_bytes(opf)
        run("set_metadata", str(book_id), str(opf_path))
    finally:
        opf_path.unlink(missing_ok=True)
    return book_id


def build_epub(
    config: Path,
    output: Path,
    verbose: bool = False,
    creator: str = "Freqtrade",
    publisher: str = "freqtrade.io",
    rights: str = "GPLv3",
    tags: str = "cryptocurrency trading bot, algorithmic trading, backtesting, hyperopt",
    cover: Path | None = None,
    no_cover: bool = False,
) -> dict:
    site_dir = Path(tempfile.mkdtemp(prefix="epub-site-"))
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "mkdocs",
                "build",
                "-f",
                str(config),
                "-d",
                str(site_dir),
                "--clean",
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"mkdocs build failed:\n{proc.stderr or proc.stdout}")

        cfg = load_config(config_file=str(config))
        docs_dir = Path(cfg["docs_dir"])
        use_dir = bool(cfg["use_directory_urls"])
        pages = flatten_nav(cfg["nav"])
        if not pages:
            raise RuntimeError("No pages in nav")

        name_map = {chapter_name(md): md for _, md in pages}
        page_map = {md.removesuffix(".md"): chapter_name(md) for md in name_map.values()}

        images: dict = {}
        rendered = []
        for title, md in pages:
            rendered.append(
                (
                    title,
                    chapter_name(md),
                    render_chapter(title, md, site_dir, docs_dir, use_dir, page_map, images),
                )
            )

        # merge with previous build: reuse old filename when content hash matches
        old_chapters = {}
        if output.is_file():
            try:
                with zipfile.ZipFile(output) as z:
                    for name in z.namelist():
                        if (
                            name.startswith("OEBPS/")
                            and name.endswith(".xhtml")
                            and not name.endswith(("nav.xhtml", "cover.xhtml"))
                        ):
                            old_chapters[name.removeprefix("OEBPS/")] = hashlib.sha256(
                                z.read(name)
                            ).hexdigest()
            except zipfile.BadZipFile:
                old_chapters = {}
        by_hash = {}
        for name, digest in old_chapters.items():
            by_hash.setdefault(digest, name)

        used, stats = set(), {"unchanged": 0, "renamed": 0, "new": 0}
        final = []
        for title, slug, data in rendered:
            digest = hashlib.sha256(data).hexdigest()
            old_name = by_hash.get(digest)
            if old_name and old_name not in used:
                name = old_name
                stats["renamed" if old_name != slug else "unchanged"] += 1
            else:
                name = slug
                stats["new"] += 1
            used.add(name)
            final.append((title, name, data))

        image_files = []
        for site_path, name in images.items():
            shutil.copy2(site_dir / site_path, site_dir / name)
            image_files.append(name)

        cover_file = pick_cover(cover, no_cover, docs_dir)
        meta, cover_files = build_meta(cfg, docs_dir, creator, publisher, rights, tags, cover_file)

        files = [
            ("META-INF/container.xml", CONTAINER_XML.encode("utf-8")),
            (
                "OEBPS/content.opf",
                build_opf([(t, n) for t, n, _ in final], sorted(image_files), meta),
            ),
            ("OEBPS/nav.xhtml", build_nav_xhtml(cfg["nav"])),
            (
                "OEBPS/css/style.css",
                (BASE_CSS + HtmlFormatter().get_style_defs(".highlight, .codehilite")).encode(
                    "utf-8"
                ),
            ),
        ]
        files += cover_files
        files += [("OEBPS/images/" + n, (site_dir / n).read_bytes()) for n in sorted(image_files)]
        files += [(f"OEBPS/{n}", d) for _, n, d in final]
        write_epub(output, files)

        removed = [n for n in old_chapters if n not in used]
        stats["removed"] = len(removed)
        stats["removed_names"] = removed
        stats["total"] = len(final)
        stats["title"] = cfg["site_name"]
        return stats
    finally:
        shutil.rmtree(site_dir, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an EPUB from an mkdocs project.")
    parser.add_argument(
        "-f", "--config", type=Path, default=Path("mkdocs.yml"), help="mkdocs config file"
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path.home() / "Documents" / "freqtrade-docs.epub",
        help="output EPUB path (default: ~/Documents/freqtrade-docs.epub)",
    )
    parser.add_argument("--creator", default="Freqtrade", help="author (default: Freqtrade)")
    parser.add_argument("--publisher", default="freqtrade.io")
    parser.add_argument("--rights", default="GPLv3")
    parser.add_argument(
        "--tags",
        default="cryptocurrency trading bot, algorithmic trading, backtesting, hyperopt",
        help="comma-separated subject tags",
    )
    parser.add_argument(
        "--cover",
        type=Path,
        default=None,
        help="cover image (default: <docs>/images/logo.png)",
    )
    parser.add_argument("--no-cover", action="store_true", help="skip the cover page")
    parser.add_argument(
        "--calibre-library",
        type=Path,
        default=None,
        help="Calibre library to update (default: the GUI's configured library)",
    )
    parser.add_argument("--no-calibre", action="store_true", help="skip the Calibre library update")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    library = args.calibre_library if args.calibre_library is not None else default_library()
    if not args.no_calibre and library and calibre_gui_running():
        raise SystemExit(
            "Calibre GUI is open and would lock the library. "
            "Close it, or use --no-calibre to build the epub only."
        )

    summary = build_epub(
        args.config,
        args.output,
        verbose=args.verbose,
        creator=args.creator,
        publisher=args.publisher,
        rights=args.rights,
        tags=args.tags,
        cover=args.cover,
        no_cover=args.no_cover,
    )
    print(
        f"Wrote {args.output} ({summary['total']} chapters, "
        f"{summary['unchanged']} unchanged, {summary['renamed']} renamed, "
        f"{summary['removed']} removed)"
    )
    if summary["removed_names"]:
        print("Removed chapters:", ", ".join(summary["removed_names"]))
    if not args.no_calibre and library:
        restart = _service_active()
        if restart and not _control_service("stop"):
            raise RuntimeError(
                "Failed to stop calibre-server (sudo cancelled?); the library may be locked."
            )
        try:
            book_id = update_calibre(library, args.output, summary["title"])
        finally:
            if restart and not _control_service("start"):
                print(
                    "WARNING: could not restart calibre-server; "
                    "run: sudo systemctl start calibre-server"
                )
        print(f"Updated Calibre library {library} (book id {book_id})")


if __name__ == "__main__":
    main()
