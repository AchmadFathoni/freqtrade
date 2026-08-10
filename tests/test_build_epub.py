import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from build_helpers.build_epub import _bump_date, build_epub, calibre_gui_running


Build = Callable[[], None]


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, Path]:
    docs = tmp_path / "docs"
    (docs / "images").mkdir(parents=True)
    (docs / "index.md").write_text(
        "# Home\n\nHello **world**.\n\n"
        "<!-- GitHub action buttons -->\n\n"
        "[Star](https://github.com/x/y){ .md-button .md-button--sm }\n\n"
        "![badge](https://img.shields.io/badge/x-y)\n"
    )
    (docs / "intro.md").write_text(
        "# Intro\n\n## Heading\n\nFirst paragraph.\n\n![pic](images/pic.png)\n"
    )
    (docs / "images" / "pic.png").write_bytes(b"png")
    (tmp_path / "mkdocs.yml").write_text(
        "site_name: Test\nsite_description: Test\n"
        "theme:\n  name: material\n"
        "markdown_extensions:\n  - attr_list\n"
        "nav:\n  - Home: index.md\n  - Section:\n      - Intro: intro.md\n"
    )
    return tmp_path / "mkdocs.yml", tmp_path / "out.epub"


def chapter(epub: Path, name: str) -> bytes:
    with zipfile.ZipFile(epub) as z:
        return z.read(f"OEBPS/{name}")


def nav_xhtml(epub: Path) -> str:
    with zipfile.ZipFile(epub) as z:
        return z.read("OEBPS/nav.xhtml").decode()


def test_section_labels_link_to_first_child(project: tuple[Path, Path]):
    cfg, out = project
    build_epub(cfg, out)
    assert '<a href="intro.xhtml">Section</a>' in nav_xhtml(out)
    assert '<a href="intro.xhtml#heading">Heading</a>' in nav_xhtml(out)


def test_web_chrome_removed(project: tuple[Path, Path]):
    cfg, out = project
    build_epub(cfg, out)
    home = chapter(out, "home.xhtml").decode()
    assert 'class="md-button' not in home
    assert "img.shields.io" not in home
    assert "action buttons" not in home
    assert '<a href="https' not in home
    assert "<!--" not in home


def test_calibre_gui_running(tmp_path: Path):
    def procdir(name: str, *procs: tuple[str, str]) -> Path:
        d = tmp_path / name
        d.mkdir()
        for pid, cmdline in procs:
            p = d / pid
            p.mkdir()
            (p / "cmdline").write_bytes(cmdline.encode() + b"\x00")
        return d

    gui = procdir("gui", ("1", "python /nix/store/x/bin/.calibre-wrapped"))
    assert calibre_gui_running(gui)

    viewer = procdir("viewer", ("2", "python /nix/store/x/bin/.ebook-viewer-wrapped"))
    assert calibre_gui_running(viewer)

    server = procdir("server", ("3", "python /nix/store/x/bin/.calibre-server-wrapped"))
    assert not calibre_gui_running(server)

    unrelated = procdir("unrelated", ("4", "python build_helpers/build_epub.py"))
    assert not calibre_gui_running(unrelated)


def test_bump_date():
    log = (
        "2026-07-30\x00fix: something\n"
        "2026-07-31\x00chore: bump version to 2026.7\n"
        "2026-06-29\x00chore: bump version to 2026.7-dev\n"
        "2026-06-29\x00chore: bump version to 2026.6\n"
    )
    assert _bump_date(log) == "2026-07-31"
    assert _bump_date("2026-06-29\x00chore: bump version to 2026.7-dev\n") is None
    assert _bump_date("2026-07-30\x00fix: something\n") is None


def test_metadata_present(project: tuple[Path, Path]):
    cfg, out = project
    cover = cfg.parent / "docs" / "images" / "pic.png"
    build_epub(cfg, out, publisher="Test Pub", rights="MIT", tags="a, b", cover=cover)
    with zipfile.ZipFile(out) as z:
        opf = z.read("OEBPS/content.opf").decode()
        assert "OEBPS/cover.xhtml" in z.namelist()
        assert "OEBPS/images/cover.png" in z.namelist()
    assert "<dc:publisher>Test Pub</dc:publisher>" in opf
    assert "<dc:rights>MIT</dc:rights>" in opf
    assert "<dc:subject>a</dc:subject>" in opf
    assert "<dc:subject>b</dc:subject>" in opf
    assert "<dc:description>Test</dc:description>" in opf
    assert 'properties="cover-image"' in opf
    assert '<meta name="cover" content="cover-img"/>' in opf
    assert "<dc:date>" in opf
    assert "dcterms:modified" in opf


def test_rebuild_is_byte_identical(project: tuple[Path, Path]):
    cfg, out = project
    build_epub(cfg, out)
    first = out.read_bytes()
    build_epub(cfg, out)
    assert out.read_bytes() == first


def test_unchanged_chapters_survive_update(project: tuple[Path, Path]):
    cfg, out = project
    build_epub(cfg, out)
    home_before = chapter(out, "home.xhtml")
    intro_before = chapter(out, "intro.xhtml")

    (cfg.parent / "docs" / "intro.md").write_text("# Intro\n\nSecond paragraph.\n")
    build_epub(cfg, out)

    assert chapter(out, "home.xhtml") == home_before
    assert chapter(out, "intro.xhtml") != intro_before


def test_renamed_page_keeps_chapter_file(project: tuple[Path, Path]):
    cfg, out = project
    build_epub(cfg, out)
    intro_before = chapter(out, "intro.xhtml")

    (cfg.parent / "docs" / "intro.md").rename(cfg.parent / "docs" / "renamed.md")
    (cfg.parent / "mkdocs.yml").write_text(
        "site_name: Test\n"
        "theme:\n"
        "  name: material\n"
        "nav:\n"
        "  - Home: index.md\n"
        "  - Intro: renamed.md\n"
    )
    summary = build_epub(cfg, out)

    assert chapter(out, "intro.xhtml") == intro_before
    assert summary["renamed"] == 1
    with zipfile.ZipFile(out) as z:
        assert not any("renamed.xhtml" in n for n in z.namelist())


def test_removed_page_drops_chapter(project: tuple[Path, Path]):
    cfg, out = project
    build_epub(cfg, out)

    (cfg.parent / "docs" / "intro.md").unlink()
    (cfg.parent / "mkdocs.yml").write_text(
        "site_name: Test\ntheme:\n  name: material\nnav:\n  - Home: index.md\n"
    )
    summary = build_epub(cfg, out)

    assert summary["removed"] == 1
    with zipfile.ZipFile(out) as z:
        assert "OEBPS/intro.xhtml" not in z.namelist()
