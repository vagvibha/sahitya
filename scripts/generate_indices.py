#!/usr/bin/env python3
"""
generate_indices.py
====================

Pre-build generation script for the साहित्यशास्त्रम् MkDocs site.

What it does, in order:

1.  Reads scripts/site_config.yaml, which declares the site's top-level
    content sections (shastra, kavya, and any future ones) purely as data
    — see that file's header comment for the schema. Adding a new section
    that follows the `<dir>/texts/<slug>/...` convention needs no code
    changes here.
2.  Reads `meta.yaml`/`meta.yml` for every text under each configured
    section's `texts/` directory, and (optionally) for every individual
    chapter directory (`chapter_name`).
3.  Reads every topic page under the topics-carrying section's `topics/`
    folder (their Devanagari `title:` frontmatter is the canonical key
    other files point back to via `topics:`), plus the two glossary pages
    `topics/chandas.md` and `topics/alankara.md`, each an HTML `<table>`
    of meters/alankaras (see `build_glossary_page` for the exact
    convention).
4.  Walks every chapter directory under each text, concatenates its
    section files into a single generated chapter page, and along the
    way:
      - collects every `topics:` reference (for shastra-domain sections)
        so the chapter page can show back-links, and so each topic page
        can list every section that cites it;
      - extracts every `<div class="shloka" ...>` (with `data-type=`,
        `data-chandas=`, `data-alankara=`, optional `highlight="true"`)
        and every verse-level `chandas:`/`alankara:` frontmatter pair,
        builds a per-chapter Shloka Table, and records each occurrence
        against the matching row in the chandas/alankara glossary.
        Extraction walks a real nesting-aware parse tree (see
        `parse_divs` below) rather than scanning with a flat regex, so it
        works correctly no matter how deeply a shloka sits inside
        wrapper divs (e.g. a kavya-play's dialog-block).
5.  Writes the generated chapter pages, per-text landing pages (sorted by
    `order:` in meta.yaml, falling back to title), topic pages, the two
    glossary pages, and one auto-generated (nav-less) detail page per
    meter/alankara into `docs/` (source files are never modified).
6.  Writes `docs/index.md` (home page, one card per content section).
7.  Writes `mkdocs.yml`, including an auto-generated `nav:` block.

This script is idempotent: it always starts by deleting only the generated
output directory contents for each configured section, `docs/assets/`
(a fresh mirror of `assets/` — see below), `docs/index.md`, and
`mkdocs.yml` — never the hand-maintained `docs/stylesheets/`,
`docs/javascripts/`, or any source directory.

Requires: PyYAML, beautifulsoup4 is no longer required — this script now
does its own nesting-aware div parsing (see `parse_divs`) rather than
relying on BeautifulSoup, so it can splice text back in place without
re-serializing (and thereby risking mangling) untouched Devanagari
Markdown content. BeautifulSoup is still used for the chandas/alankara
glossary `<table>` parsing, where full re-serialization is fine because
those tables are small, hand-authored, well-formed HTML.

Layout expected on disk (see scripts/migrate_layout.py if your repo still
has the old flat layout):

    <section>/                 e.g. shastra/, kavya/
        meta.yaml               (optional, currently unused by the script
                                  itself — reserved for future section-level
                                  notes; section-level *display* config
                                  lives in site_config.yaml instead)
        texts/
            <slug>/
                meta.yaml        title, author, type, default_shloka_type, default_class, order, ...
                <chapter>/
                    meta.yaml    (optional) chapter_name
                    *.md         section files, concatenated in numeric order
        topics/                 (optional, only on the section with
                                  `topics: true` in site_config.yaml)
            *.md
            chandas.md
            alankara.md

    assets/                      (optional, repo root — NOT inside any
                                  section) — static assets (audio/*.mp3
                                  today, anything else later) referenced
                                  by an explicit link/embed from some
                                  page's content, e.g.
                                  `<audio src="../../assets/audio/foo.mp3">`.
                                  Mirrored verbatim into docs/assets/
                                  on every build (see copy_assets());
                                  never parsed as Markdown, never linked
                                  from nav or the home page on its own.
"""

from __future__ import annotations

import posixpath
import re
import shutil
import sys
from pathlib import Path

import yaml
from bs4 import BeautifulSoup, Comment

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

SCRIPTS_DIR = Path(__file__).resolve().parent
SITE_CONFIG_PATH = SCRIPTS_DIR / "site_config.yaml"
GLOSS_TYPES_CONFIG_PATH = SCRIPTS_DIR / "gloss_types.yaml"

META_FILENAMES = ("meta.yaml", "meta.yml")

KAVYA_PROSE_TYPES = {"kavya-play", "kavya-prose"}
KAVYA_VERSE_TYPES = {"kavya-verse"}
SHASTRA_TEXT_TYPES = {"shastra-karika", "shastra-vada"}

WARNINGS: list[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"WARNING: {msg}", file=sys.stderr)


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


SITE_CONFIG = load_yaml(SITE_CONFIG_PATH)
if not SITE_CONFIG.get("content_sections"):
    warn(f"{SITE_CONFIG_PATH} has no content_sections: — nothing will be built")

# UI strings shown by the generated site that aren't tied to any one
# section/text (those go through SectionConfig/Text instead) — see
# site_config.yaml's labels: block for the full list and what each
# defaults to. Every default below matches the literal string it used to
# be, so an existing site_config.yaml without one of these keys yet
# still builds identically.
_RAW_LABELS = SITE_CONFIG.get("labels", {}) or {}


def site_label(key: str, default: str) -> str:
    return str(_RAW_LABELS.get(key, "")).strip() or default


class SectionConfig:
    """One entry of site_config.yaml's content_sections: list."""

    def __init__(self, raw: dict):
        self.dir = str(raw.get("dir", "")).strip()
        self.h1_label = str(raw.get("h1_label", self.dir)).strip()
        self.h2_text_label = str(raw.get("h2_text_label", "ग्रन्थाः")).strip()
        self.h2_topics_label = str(raw.get("h2_topics_label", "विषयाः")).strip()
        self.default_chapter_word = (
            str(raw.get("default_chapter_word", "")).strip()
            or str(SITE_CONFIG.get("default_chapter_word", "अध्यायः")).strip()
        )
        self.has_topics = bool(raw.get("topics", False))

    @property
    def src(self) -> Path:
        return ROOT / self.dir

    @property
    def texts_src(self) -> Path:
        return self.src / "texts"

    @property
    def out_dir(self) -> Path:
        return DOCS / self.dir

    @property
    def topics_src(self) -> Path:
        return self.src / "topics"


SECTIONS: list[SectionConfig] = [
    SectionConfig(raw) for raw in (SITE_CONFIG.get("content_sections") or [])
]
TOPICS_SECTION = next((s for s in SECTIONS if s.has_topics), None)


# ---------------------------------------------------------------------------
# Frontmatter helpers
# ---------------------------------------------------------------------------

FRONTMATTER_RE = re.compile(r"^---[ \t]*\n(.*?\n)---[ \t]*\n?", re.DOTALL)


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body_text). Tolerant of missing frontmatter."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        data = yaml.safe_load(m.group(1)) or {}
        if not isinstance(data, dict):
            warn(f"frontmatter did not parse to a mapping: {m.group(1)[:60]!r}")
            data = {}
    except yaml.YAMLError as e:
        warn(f"could not parse YAML frontmatter ({e})")
        data = {}
    return data, text[m.end():]


def as_list(value) -> list[str]:
    """Normalize a frontmatter value that may be a scalar, list, or None."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    v = str(value).strip()
    return [v] if v else []


def read_meta(text_dir: Path) -> dict:
    for name in META_FILENAMES:
        p = text_dir / name
        if p.exists():
            try:
                return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as e:
                warn(f"could not parse {p} ({e})")
                return {}
    return {}


def find_meta_file(text_dir: Path) -> Path | None:
    for name in META_FILENAMES:
        p = text_dir / name
        if p.exists():
            return p
    return None


# ---------------------------------------------------------------------------
# Numeric-aware sorting for chapter/section filenames — plain numeric
# ("01", "02", ... "10") and prefixed ("da01-01", "da01-02", ... "da01-10")
# both sort correctly, and correctly regardless of zero-padding, because
# the sort key is (everything before the trailing digit run, that run's
# actual numeric value) rather than a plain string compare.
# ---------------------------------------------------------------------------

TRAILING_DIGITS_RE = re.compile(r"^(.*?)(\d+)$")


def numeric_key(stem: str):
    m = TRAILING_DIGITS_RE.match(stem)
    if m:
        prefix, digits = m.groups()
        return (0, prefix, int(digits))
    return (1, stem, 0)


def rel_link(from_rel_file: str, to_rel_file: str) -> str:
    """Relative link from the page at `from_rel_file` to `to_rel_file`,
    both given as paths relative to the docs root. Real relative-path math
    (not same-depth assumptions), needed once pages live at varying depths
    (e.g. the glossary detail pages).

    NOTE: only works inside genuine Markdown link syntax `[text](...)`
    (MkDocs rewrites those into the clean-URL form for you) — NOT inside
    literal HTML `<a href="...">`, which MkDocs never touches. Use
    `raw_html_href()` for that instead."""
    from_dir = posixpath.dirname(from_rel_file)
    return posixpath.relpath(to_rel_file, start=from_dir or ".")


def url_dir_for(rel_md_file: str) -> str:
    """The clean-URL *directory* MkDocs serves a given docs/**/*.md file
    at, under the default `use_directory_urls: true` (e.g.
    'shastra/topics/alankara.md' is served at '.../shastra/topics/alankara/',
    NOT '.../shastra/topics/alankara.md' — an 'index.md' is the one
    exception, served at its own parent directory with no extra segment)."""
    d = posixpath.dirname(rel_md_file)
    stem = posixpath.basename(rel_md_file)
    if stem.endswith(".md"):
        stem = stem[:-3]
    if stem == "index":
        return d or "."
    return posixpath.join(d, stem) if d else stem


def raw_html_href(from_rel_md_file: str, to_rel_md_file: str) -> str:
    """Relative href to use inside literal HTML (raw <a href="...">), which
    MkDocs never rewrites — unlike Markdown-syntax links, this must already
    point at the final clean-URL directory, not at a '.md' path."""
    from_dir = url_dir_for(from_rel_md_file)
    to_dir = url_dir_for(to_rel_md_file)
    rel = posixpath.relpath(to_dir, start=from_dir)
    return (rel if rel != "." else "") + "/"


# ---------------------------------------------------------------------------
# Nesting-aware <div class="..."> parser
# ---------------------------------------------------------------------------
#
# The old version of this script found shloka/commentary divs with a flat
# regex: `<div class="...">(.*?)</div>`. Non-greedy `.*?` stops at the
# FIRST `</div>` it sees — which silently mispairs open/close tags the
# moment a div contains another div before its own true closing tag (e.g.
# a kavya-play's <div class="dialog-block"> wrapping a <div class="shloka">,
# or — found live in shastra/texts/sd/05/sd05-01.md — a run of
# <div class="karika">...) blocks whose authors didn't close each one
# before the next opens). That mispairing is exactly what caused the
# श्लोकसूची to sometimes silently drop verses.
#
# parse_divs() below replaces that with a real (if lightweight) stack-based
# parser: every open/close <div> tag is tracked on a stack, so nesting is
# resolved correctly regardless of depth. It also recovers from the
# "forgot to close it" pattern above with the same rule browsers use for
# things like unclosed <p>: a new div reopening the SAME class while the
# previous one of that class is still open on top of the stack implicitly
# closes the previous one first, rather than nesting under it.
#
# Callers get back a tree of DivNode objects with exact character offsets
# into the original string — so callers can do precise, minimal text
# splices (insert an anchor, replace one div's span, remove a span
# entirely for repositioning) without ever re-serializing text they didn't
# touch. This is important: these files are hand-authored Devanagari
# Markdown, and round-tripping them through an HTML serializer risks
# subtly rewriting whitespace/entities in content the script has no
# business touching.

DIV_OPEN_RE = re.compile(r'<div\b((?:[^>"]|"[^"]*")*)>')
DIV_CLOSE_RE = re.compile(r'</div\s*>')
CLASS_ATTR_RE = re.compile(r'class\s*=\s*"([^"]*)"')


class DivNode:
    __slots__ = (
        "cls", "classes", "base_cls", "attrs_str",
        "start", "tag_end", "inner_end", "end", "children",
    )

    def __init__(self, cls: str, attrs_str: str, start: int, tag_end: int):
        self.cls = cls
        self.classes = cls.split()
        self.base_cls = self.classes[0].lower() if self.classes else ""
        self.attrs_str = attrs_str
        self.start = start          # index of '<' of the opening tag
        self.tag_end = tag_end      # index right after the opening tag's '>'
        self.inner_end: int | None = None   # index of '<' of the matching close (or end-of-text)
        self.end: int | None = None         # index right after the matching close (or == inner_end)
        self.children: list["DivNode"] = []

    def has_class(self, name: str) -> bool:
        return name in self.classes


def parse_divs(text: str) -> list[DivNode]:
    """Parse every <div class="..."> ... </div> in `text` into a forest of
    DivNode, tolerant of (a) real nesting to any depth and (b) a div of
    some class left unclosed right before a sibling *of the same class*
    reopens (see module-level comment above)."""
    tokens: list[tuple[int, int, str, str | None]] = []
    for m in DIV_OPEN_RE.finditer(text):
        tokens.append((m.start(), m.end(), "open", m.group(1)))
    for m in DIV_CLOSE_RE.finditer(text):
        tokens.append((m.start(), m.end(), "close", None))
    tokens.sort(key=lambda t: t[0])

    root: list[DivNode] = []
    stack: list[DivNode] = []

    def finish(node: DivNode, inner_end: int, end: int) -> None:
        node.inner_end = inner_end
        node.end = end
        (stack[-1].children if stack else root).append(node)

    for start, tag_end, kind, attrs_str in tokens:
        if kind == "open":
            cls_m = CLASS_ATTR_RE.search(attrs_str or "")
            cls = cls_m.group(1).strip() if cls_m else ""
            node = DivNode(cls, attrs_str or "", start, tag_end)
            if stack and stack[-1].base_cls == node.base_cls and node.base_cls:
                # implicit close of the previous same-class div right here
                prev = stack.pop()
                finish(prev, start, start)
            stack.append(node)
        else:  # close
            if not stack:
                continue  # stray </div> with nothing open — ignore
            node = stack.pop()
            finish(node, start, tag_end)

    # anything still open at EOF: close it at end-of-text
    while stack:
        node = stack.pop()
        finish(node, len(text), len(text))

    return root


def walk_divs(nodes: list[DivNode]):
    """Pre-order iterator over every DivNode in the forest, recursing into
    every node's children (callers that want to STOP descending into a
    particular node's children — e.g. once it's been matched and handled
    — should not use this; see the hand-written recursive visits in
    extract_shlokas / process_content_sections instead, which need that
    control)."""
    for node in nodes:
        yield node
        yield from walk_divs(node.children)


def apply_splices(text: str, splices: list[tuple[int, int, str]]) -> str:
    """Apply a list of non-overlapping (start, end, replacement) spans to
    `text` in one pass. (end == start) means a pure insertion at that
    position with nothing removed."""
    splices = sorted(splices, key=lambda s: s[0])
    out = []
    pos = 0
    for start, end, repl in splices:
        if start < pos:
            raise ValueError(f"overlapping splice at {start} (previous ended at {pos})")
        out.append(text[pos:start])
        out.append(repl)
        pos = end
    out.append(text[pos:])
    return "".join(out)


ATTR_RE = re.compile(r'([a-zA-Z\-]+)\s*=\s*"([^"]*)"')


def parse_attrs(attr_str: str) -> dict:
    return {m.group(1): m.group(2) for m in ATTR_RE.finditer(attr_str)}


# ---------------------------------------------------------------------------
# Discovery: texts (<section>/texts/<slug>)
# ---------------------------------------------------------------------------

def title_order_sort_key(frontmatter: dict, title: str, source_for_warning: object = "") -> tuple:
    """Shared sort key for anything with an optional numeric `order:` field
    (texts on a section's landing page, topics under विषयाः, ...) — explicit
    `order:` takes priority (ascending), with unordered entries (or a
    non-numeric order:) falling back to alphabetical-by-title, sorted after
    every explicitly ordered one."""
    order = frontmatter.get("order")
    try:
        order = float(order) if order is not None else float("inf")
    except (TypeError, ValueError):
        warn(f"{source_for_warning}: 'order: {order!r}' isn't a number — ignoring it, sorting by title instead")
        order = float("inf")
    return (order, title)



class Text:
    def __init__(self, slug: str, directory: Path, meta: dict, section: SectionConfig):
        self.slug = slug
        self.dir = directory
        self.meta = meta
        self.section = section
        self.title = str(meta.get("title", slug)).strip()
        self.author = str(meta.get("author", "")).strip()
        self.type = str(meta.get("type", "")).strip()
        self.default_shloka_type = str(meta.get("default_shloka_type", "")).strip()
        self.default_class = str(meta.get("default_class", "")).strip()
        # per-book (never per-chapter) override of a gloss/vada data-type's
        # fixed label — e.g. some books call claim/refute something other
        # than the site-wide पक्षः/निरासः default. Keys are data_type
        # values from gloss_types.yaml; only ever replaces a fixed
        # `label`, never a `label_from_attr`. See gloss_types.yaml.
        raw_labels = meta.get("gloss_labels") or {}
        self.gloss_labels: dict[str, str] = (
            {str(k).strip().lower(): str(v).strip() for k, v in raw_labels.items()}
            if isinstance(raw_labels, dict) else {}
        )
        self.chapters: list["Chapter"] = []

    @property
    def sort_key(self):
        return title_order_sort_key(self.meta, self.title, self.dir)

    @property
    def out_dir(self) -> Path:
        return self.section.out_dir / "texts" / self.slug

    @property
    def rel_out_dir(self) -> str:
        return f"{self.section.dir}/texts/{self.slug}"


class Chapter:
    def __init__(self, text: Text, slug: str, sections: list[Path], meta: dict | None = None):
        self.text = text
        self.slug = slug
        self.sections = sections  # list of source .md Paths, in order
        self.meta = meta or {}

    @property
    def out_file(self) -> Path:
        return self.text.out_dir / f"{self.slug}.md"

    @property
    def rel_out_file(self) -> str:
        return f"{self.text.rel_out_dir}/{self.slug}.md"

    @property
    def default_shloka_type(self) -> str:
        """Value that fills in `data-type=` on a bare `<div class="shloka">`
        (one that doesn't already carry its own data-type=) — the
        chapter's own meta.yaml wins over the text's. This ONLY ever
        touches already-explicit shloka divs; see default_class for
        naked/undived text."""
        return str(self.meta.get("default_shloka_type", "")).strip() or self.text.default_shloka_type

    @property
    def default_class(self) -> str:
        """Class that any text NOT inside some `<div class="...">` (at
        any nesting level) is wrapped in, as if the author had written
        that div themselves — the chapter's own meta.yaml wins over the
        text's. Unset means naked text stays plain Markdown, unchanged
        (the default)."""
        return str(self.meta.get("default_class", "")).strip() or self.text.default_class

    @property
    def nav_label(self) -> str:
        if self.meta.get("chapter_name"):
            return str(self.meta["chapter_name"]).strip()

        try:
            n = int(self.slug)
            word = (
                str(self.text.meta.get("chapter_type", "")).strip()
                or self.text.section.default_chapter_word
            )
            return f"{word} {n}"
        except ValueError:
            return self.slug


def discover_texts(section: SectionConfig) -> list[Text]:
    texts = []
    src_root = section.texts_src
    if not src_root.exists():
        warn(f"{src_root} does not exist — no texts found for section '{section.dir}' "
             f"(did you run scripts/migrate_layout.py?)")
        return texts
    for d in sorted(p for p in src_root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        meta_path = find_meta_file(d)
        if not meta_path:
            warn(f"{d} has no meta.yaml/meta.yml — skipping this text")
            continue
        meta = read_meta(d)
        if meta.get("ignore"):
            print(f"Skipping {d} (ignore: true in meta.yaml)")
            continue
        if "title" not in meta:
            warn(f"{meta_path} has no 'title' — skipping this text")
            continue
        texts.append(Text(d.name, d, meta, section))
    texts.sort(key=lambda t: t.sort_key)
    return texts


def discover_chapters(text: Text) -> list[Chapter]:
    """A chapter is either a subdirectory of section files, or (if no
    directory of the same name exists) a single top-level .md file. A
    top-level .md file that duplicates a chapter directory's name is a
    leftover/error and is skipped in favour of the directory."""
    chapters: list[Chapter] = []
    dir_children = {d.name: d for d in text.dir.iterdir() if d.is_dir() and not d.name.startswith(".")}

    for name, d in dir_children.items():
        sections = sorted((f for f in d.glob("*.md")), key=lambda f: numeric_key(f.stem))
        if not sections:
            warn(f"chapter directory {d} contains no .md sections — skipping")
            continue
        chapter_meta = read_meta(d)  # optional meta.yaml/meta.yml inside the chapter dir (chapter_name, default_shloka_type, default_class, ...)
        chapters.append(Chapter(text, name, sections, chapter_meta))

    for f in text.dir.glob("*.md"):
        if f.stem in dir_children:
            warn(
                f"{f} duplicates chapter directory '{f.stem}/' in the same text "
                f"and will be IGNORED — the directory's sections are used instead. "
                f"This file should be removed from the source."
            )
            continue
        chapters.append(Chapter(text, f.stem, [f]))

    chapters.sort(key=lambda c: numeric_key(c.slug))
    return chapters


# ---------------------------------------------------------------------------
# Discovery: reference pages (topics / chandas / alankara) — always live
# under the one section configured with `topics: true` in site_config.yaml.
# ---------------------------------------------------------------------------

def title_sort_key_for_ref(fm, title, path):
    return title_order_sort_key(fm, title, path)


class NavListEntry:
    """A lightweight (title, link, sort_key) stand-in used only for the
    home page's/nav's विषयाः listing — for pages (like the chandas/alankara
    glossary pages) that have their own bespoke rendering path and so
    aren't full RefPage topic objects."""

    def __init__(self, title: str, rel_out_file: str, frontmatter: dict, source_for_warning: object = ""):
        self.title = title
        self.rel_out_file = rel_out_file
        self.frontmatter = frontmatter
        self._source = source_for_warning

    @property
    def sort_key(self):
        return title_order_sort_key(self.frontmatter, self.title, self._source)


class RefPage:
    def __init__(self, kind: str, slug: str, path: Path, frontmatter: dict, body: str, rel_dir: str):
        self.kind = kind  # "topic"
        self.slug = slug
        self.path = path
        self.frontmatter = frontmatter
        self.body = body
        self.rel_dir = rel_dir  # e.g. "shastra/topics"
        self.title = str(frontmatter.get("title", slug)).strip()
        self.references: list["Reference"] = []  # filled in during the scan

    @property
    def sort_key(self):
        return title_order_sort_key(self.frontmatter, self.title, self.path)

    @property
    def rel_out_file(self) -> str:
        return f"{self.rel_dir}/{self.slug}.md"

    @property
    def out_file(self) -> Path:
        return DOCS / self.rel_out_file


class Reference:
    """One occurrence of a topic/meter/alankara tag inside a chapter page."""

    def __init__(self, title_text: str, chapter: Chapter, anchor: str, preview: str):
        self.title_text = title_text
        self.chapter = chapter
        self.anchor = anchor
        self.preview = preview


def discover_ref_pages(kind: str, folder: Path, rel_dir: str, exclude: set[str] = frozenset()) -> dict[str, RefPage]:
    pages: dict[str, RefPage] = {}
    if not folder.exists():
        return pages
    for f in sorted(folder.glob("*.md")):
        if f.name in exclude:
            continue
        text = f.read_text(encoding="utf-8")
        fm, body = split_frontmatter(text)
        title = str(fm.get("title", "")).strip()
        if not title:
            warn(f"{f} has no 'title' in frontmatter — skipping")
            continue
        if title in pages:
            warn(f"duplicate title '{title}' between {pages[title].path} and {f}")
            continue
        pages[title] = RefPage(kind, f.stem, f, fm, body, rel_dir)
    return pages


# ---------------------------------------------------------------------------
# Chandas / alankara glossary tables
# ---------------------------------------------------------------------------
#
# topics/chandas.md and topics/alankara.md (under the topics-carrying
# section) are each a single hand-maintained page containing one or more
# HTML <table>s (one row per meter/alankara). A row counts as a matchable
# glossary entry if it carries an HTML comment `<!-- chandas-name -->` /
# `<!-- alankara-name -->` anywhere among that <tr>'s direct children —
# that comment is only a marker (its exact wording isn't otherwise used);
# the entry's canonical name is the exact text of that row's first <td>,
# and must match `chandas:`/`alankara:` frontmatter or
# `data-chandas=`/`data-alankara=` attributes exactly.
#
# These tables are small, well-formed, hand-authored HTML, so — unlike the
# shloka/commentary scanning above — using BeautifulSoup here (full parse
# + re-serialize) is safe and simpler than hand-rolling it.
#
# For every marked row the script: (1) auto-generates a detail page (the
# row's own remaining columns + every shloka across the codebase tagging
# it) that is intentionally NOT added to nav — it's only reachable by
# clicking the entry's name in the table; (2) turns that first cell's text
# into a link to the detail page, in the copy written to docs/.

GLOSSARY_MARKER = {"chandas": "chandas-name", "alankara": "alankara-name"}
GLOSSARY_OUT_SUBDIR = {"chandas": "_chandas", "alankara": "_alankara"}

TABLE_BLOCK_RE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
SLUG_INVALID_RE = re.compile(r"[^0-9A-Za-z\u0900-\u097F]+")


def slugify(name: str) -> str:
    slug = SLUG_INVALID_RE.sub("-", name.strip()).strip("-")
    return slug or "entry"


class TableEntry:
    """One glossary row (a meter or an alankara) parsed out of
    topics/chandas.md or alankara.md."""

    def __init__(self, kind: str, title: str, slug: str, col_labels: list[str], col_html: list[str], rel_dir: str):
        self.kind = kind
        self.title = title
        self.slug = slug
        self.col_labels = col_labels  # header text for each remaining column
        self.col_html = col_html  # that row's inner HTML for each remaining column
        self.rel_dir = rel_dir
        self.references: list[Reference] = []
        self.listing_title = kind  # filled in by main() with the real chandas.md/alankara.md page title

    @property
    def listing_rel_file(self) -> str:
        """The chandas.md / alankara.md page this entry's row lives on —
        the "Up" target for this entry's own (nav-less) detail page."""
        return f"{self.rel_dir}/{self.kind}.md"

    @property
    def rel_out_file(self) -> str:
        return f"{self.rel_dir}/{GLOSSARY_OUT_SUBDIR[self.kind]}/{self.slug}.md"

    @property
    def out_file(self) -> Path:
        return DOCS / self.rel_out_file


def build_glossary_page(kind: str, path: Path, rel_dir: str) -> tuple[dict[str, TableEntry], str, dict]:
    """Returns (entries, rendered_body, page_frontmatter) for
    topics/{chandas,alankara}.md. `rendered_body` is the source body with
    every matched entry's name cell turned into a link to its (nav-less)
    detail page. `page_frontmatter` (title/order/etc.) is exposed so
    callers can list this page under विषयाः alongside regular topics,
    sorted/titled the same way."""
    if not path.exists():
        warn(f"{path} does not exist — no {kind} entries will be available")
        return {}, "", {}

    raw = path.read_text(encoding="utf-8")
    fm, body = split_frontmatter(raw)
    marker = GLOSSARY_MARKER[kind]
    entries: dict[str, TableEntry] = {}

    def repl_table(m: re.Match) -> str:
        soup = BeautifulSoup(m.group(0), "html.parser")
        table = soup.find("table")
        if table is None:
            return m.group(0)

        header_labels: list[str] = []
        first_row = table.find("tr")
        if first_row is not None and first_row.find("th"):
            header_labels = [th.get_text(strip=True) for th in first_row.find_all("th")]

        for tr in table.find_all("tr"):
            if tr.find("th"):
                continue  # header row
            has_marker = bool(
                tr.find_all(string=lambda s, marker=marker: isinstance(s, Comment) and marker in s)
            )
            if not has_marker:
                continue
            tds = tr.find_all("td", recursive=False)
            if not tds:
                continue
            name = tds[0].get_text(strip=True)
            if not name:
                warn(f"{path}: a row marked <!-- {marker} --> has an empty name cell — skipping")
                continue
            if name in entries:
                warn(f"{path}: duplicate {kind} entry '{name}' — keeping the first occurrence")
                continue

            slug = slugify(name)
            rest_labels = header_labels[1:1 + len(tds) - 1] if header_labels else []
            rest_html = ["".join(str(c) for c in td.contents).strip() for td in tds[1:]]
            entries[name] = TableEntry(kind, name, slug, rest_labels, rest_html, rel_dir)

            href = raw_html_href(f"{rel_dir}/{kind}.md", entries[name].rel_out_file)
            a_tag = soup.new_tag("a", href=href)
            for child in list(tds[0].children):
                a_tag.append(child.extract())
            tds[0].append(a_tag)

        return str(soup)

    new_body = TABLE_BLOCK_RE.sub(repl_table, body).strip()
    if not entries:
        warn(f"{path}: found no rows marked <!-- {marker} --> — no {kind} entries were registered")

    title = str(fm.get("title", kind)).strip()
    rendered = new_body if H1_RE.match(new_body) else f"# {title}\n\n{new_body}"
    return entries, rendered, fm


def render_glossary_entry_page(entry: TableEntry) -> str:
    topnav = render_topnav(entry.rel_out_file, entry.listing_rel_file, entry.listing_title)
    parts = [topnav, f"# {entry.title}", ""]
    for label, html in zip(entry.col_labels, entry.col_html):
        if not html:
            continue
        if label:
            parts.append(f"**{label}**")
            parts.append("")
        parts.append(html)
        parts.append("")
    if entry.references:
        parts.append(f"## {site_label('references_heading', 'सन्दर्भाः')}")
        parts.append("")
        for ref in entry.references:
            label = f"{ref.chapter.text.title} — {ref.chapter.nav_label}"
            link = rel_link(entry.rel_out_file, ref.chapter.rel_out_file) + f"#{ref.anchor}"
            parts.append(f"- [{ref.preview}]({link}) — {label}")
    parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Shloka extraction — walks the nesting-aware div tree from parse_divs(),
# looking for every <div class="shloka" ...> at any depth.
# ---------------------------------------------------------------------------
#
# Attributes read off a shloka div (all optional):
#   data-type      one of karika / nataka / dialog / ... (free-form; CSS
#                  keys off it). Falls back to the chapter's/text's
#                  default_shloka_type (meta.yaml) when omitted — and the
#                  resolved value is written back into the OUTPUT div
#                  (never into source) so `[data-type="..."]` CSS actually
#                  has something to match even when the author never
#                  wrote data-type at all.
#   data-chandas   meter name; falls back to the section's `chandas:`
#                  frontmatter (verse-per-file kavya-verse texts tag the
#                  meter once in frontmatter instead of per-div).
#   data-alankara  comma-separated alankara name(s); same frontmatter
#                  fallback (`alankara:`).
#   highlight="true"   optional; CSS renders a distinct highlight tint.
#                      Left exactly as authored — never rewritten.
#
# Legacy `data-meter=` is still read (with a warning) as a synonym for
# `data-chandas=`, in case migrate_layout.py hasn't been run over every
# file yet.

DATA_TYPE_INJECT_RE = None  # placeholder, unused — injection is done via splice, see below


def preview_text(raw: str, max_len: int = 60) -> str:
    text = raw.lstrip(">").strip()
    text = re.sub(r"\*\*|\*|_", "", text)
    text = re.sub(r"\s+", " ", text)
    first_line = text.split("।")[0].split("॥")[0].strip()
    if not first_line:
        first_line = text.strip()
    if len(first_line) > max_len:
        first_line = first_line[:max_len].rstrip() + "…"
    return first_line


class Shloka:
    def __init__(self, chandas: str, alankaras: list[str], preview: str, data_type: str, highlight: bool):
        self.chandas = chandas
        self.alankaras = alankaras
        self.preview = preview
        self.data_type = data_type
        self.highlight = highlight


def extract_shlokas(
    body: str, fm_chandas: str, fm_alankaras: list[str], default_shloka_type: str, start_index: int = 0,
    source_for_warning: object = "",
) -> tuple[str, list[Shloka], int]:
    """Find every <div class="shloka"> in `body` at any nesting depth,
    inject an id="..." attribute for the Shloka Table to link to (see
    below for why this is an id= on the div itself, not a separate
    anchor tag), inject a resolved data-type="..." attribute into the
    ones that didn't specify their own (from `default_shloka_type` — see
    Chapter.default_shloka_type; this ONLY ever touches an already-explicit
    <div class="shloka">, never naked text — see process_content_sections'
    `default_class` for that), and return (modified_body, [Shloka, ...],
    next_index).

    `start_index` lets callers number shlokas contiguously across every
    section in a chapter (ids must be chapter-unique, since all sections
    end up concatenated onto a single generated chapter page and the
    Shloka Table numbers verses chapter-wide, not per-section).
    """
    tree = parse_divs(body)
    shlokas: list[Shloka] = []
    splices: list[tuple[int, int, str]] = []
    counter = start_index

    def visit(nodes: list[DivNode]):
        nonlocal counter
        for node in nodes:
            if node.base_cls != "shloka":
                visit(node.children)  # keep looking, however deep the shloka is nested
                continue

            counter += 1
            attrs = parse_attrs(node.attrs_str)

            data_type = attrs.get("data-type", "").strip() or default_shloka_type

            chandas = attrs.get("data-chandas", "").strip() or fm_chandas

            alankaras = (
                [a.strip() for a in attrs["data-alankara"].split(",") if a.strip()]
                if attrs.get("data-alankara")
                else list(fm_alankaras)
            )
            highlight = attrs.get("highlight", "").strip().lower() == "true"

            inner = body[node.tag_end:node.inner_end]
            anchor = f"s{counter}"
            shlokas.append(Shloka(chandas, alankaras, preview_text(inner), data_type, highlight))

            # id= goes directly on the shloka div, NOT a separate
            # <a id="..."></a> tag on its own line before it: a lone <a>
            # is inline HTML, so Python-Markdown wraps a line containing
            # only that in its own <p>, which then carries the theme's
            # default paragraph margin — a real, visible gap before every
            # single shloka for no reason. A div's own id= attribute is a
            # perfectly valid link target (#s1 still works identically)
            # and adds no extra element/margin at all.
            splices.append((node.tag_end - 1, node.tag_end - 1, f' id="{anchor}"'))
            if data_type and not attrs.get("data-type", "").strip():
                # inject the resolved default right before the tag's closing '>'
                splices.append((node.tag_end - 1, node.tag_end - 1, f' data-type="{data_type}"'))
            # a shloka div is a leaf for our purposes — don't recurse into it

    visit(tree)
    new_body = apply_splices(body, splices)
    return new_body, shlokas, counter


# ---------------------------------------------------------------------------
# "Labeled hideable sections" — commentary-type content blocks, driven by
# scripts/gloss_types.yaml (see that file for the full convention). Most
# of these are <div class="gloss" data-type="...">, but a data-type entry
# can instead declare class: vada (claim/refute) — see gloss_types.yaml.
# Sections declared with reposition: true are extracted from wherever
# they were written and reinserted, in the config file's order, right
# after the section's shloka (or appended at the end if there's no
# shloka). Everything else is labeled/marked hideable in place, never
# moved. Also tree-based, for the same reason as extract_shlokas: a
# commentary div can legitimately sit inside a structural wrapper (e.g.
# dialog-block), and a flat regex would mispair it.

GLOSS_CLASS = "gloss"  # default div class for a gloss_types.yaml entry when it doesn't set class:

# Two INDEPENDENT concerns, each its own CSS class — see gloss_types.yaml
# for the full convention:
#   TOGGLEABLE_CLASS  - this div is a member of the global Show/Hide
#                       toggle group at all (drives whether the button
#                       even appears — button shows iff >=1 div on the
#                       page carries this class — and whether the button
#                       has any effect on this div once clicked).
#   HIDDEN_INITIAL_CLASS - this div starts hidden on page load. Purely
#                       about initial display; has no bearing on whether
#                       the div is a toggle-group member. A div can be
#                       TOGGLEABLE without HIDDEN_INITIAL (visible on
#                       load, but the button can still hide it), or (in
#                       principle) HIDDEN_INITIAL without TOGGLEABLE —
#                       though nothing currently produces that combination,
#                       since a div that's permanently hidden with no way
#                       to reveal it would be pointless.
TOGGLEABLE_CLASS = "sv-toggleable"
HIDDEN_INITIAL_CLASS = "sv-hidden-default"


def load_gloss_types_config() -> dict:
    if not GLOSS_TYPES_CONFIG_PATH.exists():
        warn(f"{GLOSS_TYPES_CONFIG_PATH} not found — no gloss data-types will be labeled/hideable")
        return {}
    data = yaml.safe_load(GLOSS_TYPES_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    by_type: dict[str, dict] = {}
    for entry in data.get("types", []):
        data_type = str(entry.get("data_type", "")).strip().lower()
        if data_type:
            by_type[data_type] = entry
    return by_type


GLOSS_TYPES_BY_KEY = load_gloss_types_config()
REPOSITION_ORDER = [t for t, cfg in GLOSS_TYPES_BY_KEY.items() if cfg.get("reposition")]

# Every div class that gloss_types.yaml routes some data-type through —
# "gloss" is always included (the default/implicit class), plus whatever
# other class: values (e.g. "vada") appear in the config. A <div> whose
# base class isn't in this set is left alone as structural content (and,
# per gloss_types.yaml, also breaks notes-attachment for whatever follows
# it — see the "notes" cascade in process_content_sections below).
RECOGNIZED_DIV_CLASSES = {GLOSS_CLASS} | {
    str(cfg.get("class", GLOSS_CLASS)).strip().lower() for cfg in GLOSS_TYPES_BY_KEY.values()
}

TOGGLE_HIDE_RE = re.compile(r'\btoggle-hide\s*=\s*"(true|false)"', re.IGNORECASE)


def commentary_class(type_key: str) -> str:
    cfg = GLOSS_TYPES_BY_KEY.get(type_key)
    return str(cfg.get("class", GLOSS_CLASS)).strip().lower() if cfg else GLOSS_CLASS


def commentary_label(type_key: str, attrs: str, label_overrides: dict[str, str] | None = None) -> str:
    cfg = GLOSS_TYPES_BY_KEY.get(type_key)
    if not cfg:
        return ""
    # a per-book gloss_labels: override (meta.yaml) only ever replaces a
    # fixed `label`, never `label_from_attr` — that's already per-instance.
    if label_overrides and type_key in label_overrides:
        return str(label_overrides[type_key]).strip()
    if cfg.get("label_from_attr"):
        return parse_attrs(attrs).get(cfg["label_from_attr"], "").strip()
    return str(cfg.get("label", "") or "").strip()


def commentary_toggleable(type_key: str, attrs: str, force_hidden: bool = False) -> bool:
    """Is this instance a member of the global Show/Hide toggle group at
    all? An explicit instance-level toggle-hide="true"/"false" always
    wins — "true" opts this one instance IN (regardless of its type's own
    hideable:), "false" opts it OUT entirely (always visible, completely
    ignoring the button — see gloss_types.yaml). `force_hidden` is the
    notes-attachment cascade (see process_content_sections): a "notes"
    div forced hidden by a preceding hidden element must itself become
    toggleable, or it could never be revealed at all."""
    m = TOGGLE_HIDE_RE.search(attrs)
    if m:
        return m.group(1).lower() == "true"
    if force_hidden:
        return True
    cfg = GLOSS_TYPES_BY_KEY.get(type_key)
    return bool(cfg and cfg.get("hideable", True))


def commentary_hidden_initial(type_key: str, attrs: str, force_hidden: bool = False) -> bool:
    """Does this instance start hidden on page load? Only meaningful for
    a div that's actually toggleable (see commentary_toggleable) — this
    function doesn't check that itself, callers gate on it. An instance's
    own toggle-hide="true"/"false" always wins (and self-selects "start
    hidden" / "start visible" respectively); otherwise a "notes" cascade
    force_hidden wins next; otherwise falls back to the type's own
    hidden_by_default in gloss_types.yaml."""
    m = TOGGLE_HIDE_RE.search(attrs)
    if m:
        return m.group(1).lower() == "true"
    if force_hidden:
        return True
    cfg = GLOSS_TYPES_BY_KEY.get(type_key)
    return bool(cfg and cfg.get("hidden_by_default"))


def render_commentary_div(
    cls_raw: str, type_key: str, attrs: str, content: str,
    label_overrides: dict[str, str] | None = None, force_hidden: bool = False,
) -> tuple[str, bool]:
    """Returns (rendered_html, hidden_initial) — `hidden_initial` is
    exposed so callers can track it for the notes-attachment cascade (see
    process_content_sections)."""
    label = commentary_label(type_key, attrs, label_overrides)
    classes = cls_raw.strip()
    toggleable = commentary_toggleable(type_key, attrs, force_hidden)
    hidden_initial = toggleable and commentary_hidden_initial(type_key, attrs, force_hidden)
    if toggleable:
        classes = f"{classes} {TOGGLEABLE_CLASS}"
        if hidden_initial:
            classes = f"{classes} {HIDDEN_INITIAL_CLASS}"
    inner = content.strip()
    rendered = f"<u>{label}</u> – {inner}" if label else inner
    # data-type is re-emitted (data-name and any other original attribute
    # is intentionally dropped — it was only ever needed to resolve the
    # label above, at build time; CSS keys off data-type, not those).
    type_attr = f' data-type="{type_key}"' if type_key else ""
    return f'<div class="{classes}"{type_attr}>\n\n{rendered}\n\n</div>', hidden_initial


def resolve_default_class(default_class: str) -> tuple[str, str]:
    """`default_class` in meta.yaml names either a gloss/vada data-type
    (e.g. "vritti") or a literal structural class (e.g. "dialog-block") —
    returns (div_class_to_synthesize, type_key), so wrap_gaps can build
    the right synthetic div either way without content authors needing
    to think about the gloss/data-type split at all."""
    value = default_class.strip().lower()
    if not value:
        return "", ""
    if value in GLOSS_TYPES_BY_KEY:
        return GLOSS_CLASS, value
    return value, ""


def process_content_sections(
    body: str, default_class: str = "", source_for_warning: object = "",
    label_overrides: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Returns (body_with_repositioned_sections_removed, reordered_html).

    `default_class`, when set, makes THAT the chapter-wide default for
    content: any run of text that isn't inside some other `<div>` (at any
    nesting level) is treated exactly as if the author had written that
    div themselves — same hidden/reposition/label handling as an explicit
    div of that type, with no difference in outcome. This is what makes
    e.g. `default_class: vritti` mean "the whole chapter is वृत्ति prose
    by default; only explicitly-tagged blocks (shloka/karika, other gloss
    data-types, ...) are anything else" — matching how these texts
    actually alternate root-verse and prose, without needing a
    `<div class="gloss" data-type="vritti">` wrapped around every single
    paragraph. See resolve_default_class() for how a value is decided to
    be a gloss data-type vs. a literal structural class.

    Unset (the default), body text outside of any div is left as plain
    Markdown, unchanged — the pre-existing behavior.

    This is a *different* mechanism from `default_shloka_type` (see
    extract_shlokas), which only fills in `data-type=` on an *already
    explicit* `<div class="shloka">` lacking one — that one only ever
    concerns shloka divs, never naked text.
    """
    tree = parse_divs(body)
    splices: list[tuple[int, int, str]] = []
    by_type: dict[str, list[tuple[str, str]]] = {}
    wrap_div_class, wrap_type_key = resolve_default_class(default_class)

    def handle_matched(
        cls_raw: str, type_key: str, attrs: str, content: str, start: int, end: int,
        pad: bool = False, force_hidden: bool = False,
    ) -> bool:
        """Returns whether this div started hidden — used by visit() to
        feed the notes-attachment cascade (see gloss_types.yaml)."""
        cfg = GLOSS_TYPES_BY_KEY.get(type_key)
        if cfg and cfg.get("reposition"):
            by_type.setdefault(type_key, []).append((attrs, content))
            splices.append((start, end, ""))
            # still resolve initial hidden-ness for cascade-tracking
            # purposes even though this div is rendered later, at its new
            # position, by the REPOSITION_ORDER pass below (which calls
            # commentary_hidden_initial identically) — reposition:true
            # types are never "notes" themselves, so this never affects
            # their own rendering, only what a sibling "notes" div sees.
            toggleable = commentary_toggleable(type_key, attrs, force_hidden)
            return toggleable and commentary_hidden_initial(type_key, attrs, force_hidden)
        rendered, hidden = render_commentary_div(cls_raw, type_key, attrs, content, label_overrides, force_hidden)
        if pad:
            # a gap-wrapped synthetic div (see wrap_gaps) swallows all
            # of the original whitespace between it and its neighbors
            # as part of the splice — re-add a blank line on each
            # side so it doesn't end up glued directly against an
            # adjacent </div><div...> with no blank line between
            # them, which risks the two not being parsed as separate
            # block-level HTML.
            rendered = f"\n\n{rendered}\n\n"
        splices.append((start, end, rendered))
        return hidden

    def wrap_gaps(start: int, end: int, nodes: list[DivNode]) -> None:
        """Any non-whitespace text directly inside [start, end) that
        ISN'T covered by one of `nodes` (this level's div children,
        already known to be non-overlapping and in order) gets treated as
        a synthetic default_class div, run through the exact same
        handling as a real one (including reposition, if default_class
        happens to name a reposition: true gloss data-type)."""
        if not wrap_div_class:
            return
        cursor = start
        for n in nodes + [None]:
            gap_end = n.start if n is not None else end
            gap = body[cursor:gap_end]
            if gap.strip():
                handle_matched(wrap_div_class, wrap_type_key, "", gap, cursor, gap_end, pad=True)
            cursor = n.end if n is not None else end

    def visit(nodes: list[DivNode], parent_start: int, parent_end: int):
        wrap_gaps(parent_start, parent_end, nodes)
        last_hidden = False  # notes-attachment cascade — see gloss_types.yaml
        for node in nodes:
            if node.base_cls == "shloka":
                # shloka is its own leaf unit, handled entirely and
                # separately by extract_shlokas() afterward — its inner
                # text is already explicitly typed by being inside a
                # <div class="shloka">, so it must NOT be re-wrapped in
                # default_class (there'd be nothing bounding it from the
                # inside, since a shloka div has no div children of its
                # own — every character of it would otherwise look like
                # "naked" text to wrap_gaps). Left completely untouched
                # here; it still correctly bounds the gaps around it,
                # since it's one of `nodes`. A shloka is never hidden, so
                # it always resets the notes-attachment chain.
                last_hidden = False
                continue
            is_glosslike = node.base_cls in RECOGNIZED_DIV_CLASSES
            type_key = parse_attrs(node.attrs_str).get("data-type", "").strip().lower() if is_glosslike else ""
            matched = is_glosslike or bool(TOGGLE_HIDE_RE.search(node.attrs_str))
            if not matched:
                visit(node.children, node.tag_end, node.inner_end)  # structural divs (dialog-block, ...) — look inside, but leave as-is
                # an unrecognized structural div — including a bare
                # <div></div> with no class at all — breaks the
                # notes-attachment chain (see gloss_types.yaml: this is
                # the "dummy div" convention for detaching a note from
                # whatever precedes it).
                last_hidden = False
                continue
            if is_glosslike and type_key and type_key not in GLOSS_TYPES_BY_KEY:
                warn(f"{source_for_warning}: <div class=\"{node.base_cls}\" data-type=\"{type_key}\"> — "
                     f"'{type_key}' isn't declared in {GLOSS_TYPES_CONFIG_PATH.name} (no label/hide/reposition "
                     f"will apply to it, only toggle-hide= if set explicitly)")
            content = body[node.tag_end:node.inner_end]
            force_hidden = (
                type_key == "notes" and last_hidden and not TOGGLE_HIDE_RE.search(node.attrs_str)
            )
            last_hidden = handle_matched(
                node.cls, type_key, node.attrs_str, content, node.start, node.end, force_hidden=force_hidden
            )
            # a matched gloss/vada div is opaque — don't recurse into it

    visit(tree, 0, len(body))
    body = apply_splices(body, splices)
    reordered_html = "\n\n".join(
        render_commentary_div(commentary_class(type_key), type_key, attrs, content, label_overrides)[0]
        for type_key in REPOSITION_ORDER
        for attrs, content in by_type.get(type_key, [])
    )
    return body, reordered_html


def insert_reordered_sections(body: str, reordered: str) -> str:
    """Splices `reordered` right after the section's (last) shloka, or
    appends it at the end of the section if there's no shloka at all
    (e.g. shastra sections, or a kavya-play section with none of these
    commentary blocks). Uses the same tree parser so it finds the true
    last shloka regardless of nesting."""
    if not reordered:
        return body.strip()
    tree = parse_divs(body)
    shloka_ends = [n.end for n in walk_divs(tree) if n.base_cls == "shloka"]
    if shloka_ends:
        cut = max(shloka_ends)
        return f"{body[:cut].strip()}\n\n{reordered}\n\n{body[cut:].strip()}".strip()
    return f"{body.strip()}\n\n{reordered}".strip()


# ---------------------------------------------------------------------------
# Top navbar: just "Home" + "Up / TOC" (see render_topnav). Replaces the
# previous full list of top-level section tabs (navigation.tabs is turned
# off in build_mkdocs_static below) — every generated page gets a small
# two-button bar computed at build time (a plain relative link, no JS
# needed) pointing at the site home and at whatever TOC makes sense for
# that specific page (the containing text's TOC for a chapter page, the
# containing section's landing page for a text's own TOC page, etc).
# ---------------------------------------------------------------------------

def render_topnav(current_rel_file: str, up_target_rel_file: str | None, up_label: str | None) -> str:
    home_link = rel_link(current_rel_file, "index.md")
    parts = [f"[{site_label('home_button_label', 'मुखपृष्ठम्')}]({home_link})"]
    if up_target_rel_file:
        up_link = rel_link(current_rel_file, up_target_rel_file)
        parts.append(f'[⬆ {up_label}]({up_link})')
    inner = " · ".join(parts)
    return f'<div class="sv-topnav">\n\n{inner}\n\n</div>\n'


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

ASSETS_SRC = ROOT / "assets"
ASSETS_OUT = DOCS / "assets"


def clean_output():
    for section in SECTIONS:
        if section.out_dir.exists():
            shutil.rmtree(section.out_dir)
    if ASSETS_OUT.exists():
        shutil.rmtree(ASSETS_OUT)
    index_md = DOCS / "index.md"
    if index_md.exists():
        index_md.unlink()
    DOCS.mkdir(parents=True, exist_ok=True)


def copy_assets() -> int:
    """Mirrors `assets/` (repo root — audio/*.mp3 today, anything else
    later) into `docs/assets/`, so MkDocs serves it as static files.
    Unlike everything else this script writes, these files are never
    parsed as Markdown or linked from nav/home cards on their own — they
    only exist to be linked (or embedded, e.g. `<audio src=...>`) FROM a
    regular page, with a plain relative path (`rel_link` works fine for
    this — assets aren't clean-URL-rewritten the way *.md pages are, so
    no special-casing is needed there). Dotfiles (.DS_Store, .gitkeep,
    ...) are skipped. Returns the number of files copied."""
    if not ASSETS_SRC.exists():
        return 0

    def ignore_dotfiles(_dir: str, names: list[str]) -> list[str]:
        return [n for n in names if n.startswith(".")]

    shutil.copytree(ASSETS_SRC, ASSETS_OUT, ignore=ignore_dotfiles)
    return sum(1 for p in ASSETS_OUT.rglob("*") if p.is_file())


def write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


DIV_OPEN_ANY_RE = re.compile(r"<div\b([^>]*)>")


def enable_markdown_in_divs(text: str) -> str:
    """MkDocs' md_in_html extension only processes Markdown syntax (bold,
    links, etc.) *inside* a raw <div> if that div carries markdown="1" (or
    has a blank line right after its opening tag, which none of our
    sources do). Without this, things like **bold karika text** would
    render as literal asterisks. This is purely an output-side annotation
    (added only to the generated docs/ copies, never to the source .md
    files) so content authors don't need to remember to add it."""

    def repl(m: re.Match) -> str:
        attrs = m.group(1)
        if "markdown=" in attrs:
            return m.group(0)
        return f"<div{attrs} markdown=\"1\">"

    return DIV_OPEN_ANY_RE.sub(repl, text)


BRACKET_ATTR_SPAN_RE = re.compile(
    r'(?:\[(?P<bracketed>[^\]\n]+)\]|(?P<bare>\([^)\n]+\)))'
    r'\s*\.?\s*\{:\s*\.(?P<cls>[a-zA-Z0-9_-]+)\s*\}'
)


def convert_bracket_attr_spans(text: str) -> str:
    """The kavya prose/play sources mark stage directions with a bracket +
    inline-attribute-list convention, e.g. `[(प्रविश्य)].{: .action}` (and,
    inconsistently, sometimes without the brackets: `(निष्क्रान्ता){: .action}`).
    Standard Markdown's attr_list extension only attaches `{: .class}` to
    already-recognized inline elements (links, emphasis, code) — not to
    bare bracketed/parenthesized text — so as written this would render as
    literal brackets. This converts both forms straight into
    `<span class="...">` before Markdown ever sees them, so the existing
    source convention works without content authors needing to change it."""

    def repl(m: re.Match) -> str:
        content = m.group("bracketed") if m.group("bracketed") is not None else m.group("bare")
        return f'<span class="{m.group("cls")}">{content}</span>'

    return BRACKET_ATTR_SPAN_RE.sub(repl, text)


def write_md(path: Path, content: str):
    """Like write(), but also enables md_in_html on every <div> so inline
    Markdown (bold, links, etc.) inside content blocks renders correctly.
    Only ever used for generated docs/**/*.md files, never for mkdocs.yml."""
    content = convert_bracket_attr_spans(content)
    content = enable_markdown_in_divs(content)
    write(path, content)


# ---------------------------------------------------------------------------
# Section (शास्त्रम्/काव्यम्/...) + text + chapter page rendering
# ---------------------------------------------------------------------------

def build_domain_index_page(section: SectionConfig, texts: list[Text], topic_nav_entries: list) -> str:
    """A section's own landing page — mirrors its home-page card (texts,
    then topics if this is the topics-carrying section), just as a full
    page rather than a card. This is the "Up" target for every text's own
    TOC page, and (via the "मुखपृष्ठम्" button) reachable from anywhere."""
    rel_file = f"{section.dir}/index.md"
    lines = [render_topnav(rel_file, None, None), f"# {section.h1_label}", ""]
    lines.append(f"## {section.h2_text_label}")
    lines.append("")
    for t in texts:
        target = f"{t.rel_out_dir}/index.md"
        lines.append(f"- [{t.title}]({rel_link(rel_file, target)})")
    lines.append("")
    if section.has_topics and topic_nav_entries:
        lines.append(f"## {section.h2_topics_label}")
        lines.append("")
        for entry in topic_nav_entries:
            lines.append(f"- [{entry.title}]({rel_link(rel_file, entry.rel_out_file)})")
        lines.append("")
    return "\n".join(lines)


def build_topics_index_page(section: SectionConfig, topic_nav_entries: list) -> str:
    """Dedicated विषयाः landing page — the "Up" target for every individual
    topic page and for the chandas/alankara glossary listing pages, so
    going "up" from inside a topic lands you back among *other topics*,
    not back among the texts (which is a different, unrelated listing one
    level further up, at the section's own domain index page)."""
    rel_file = f"{section.dir}/topics/index.md"
    up_target = f"{section.dir}/index.md"
    lines = [
        render_topnav(rel_file, up_target, section.h1_label),
        f"# {section.h2_topics_label}",
        "",
    ]
    for entry in topic_nav_entries:
        lines.append(f"- [{entry.title}]({rel_link(rel_file, entry.rel_out_file)})")
    lines.append("")
    return "\n".join(lines)


def build_text_index_page(text: Text) -> str:
    rel_file = f"{text.rel_out_dir}/index.md"
    up_target = f"{text.section.dir}/index.md"
    lines = [render_topnav(rel_file, up_target, text.section.h1_label), f"# {text.title}", ""]
    if text.author:
        lines += [f"**{site_label('author_label', 'कर्ता:')}** {text.author}", ""]
    chapters_label = str(text.meta.get("chapters", "")).strip() or "अध्यायाः / भागाः"
    lines.append(f"## {chapters_label}")
    lines.append("")
    for ch in text.chapters:
        lines.append(f"- [{ch.nav_label}]({ch.slug}.md)")
    lines.append("")
    return "\n".join(lines)


SECTION_HEADING_RE = re.compile(r"^\s*#\s+(.+)$", re.MULTILINE)


def section_label(fm: dict, body: str, fallback_stem: str) -> str:
    """Best-effort human-readable label for a section, used in back-links:
    prefer an explicit `ref:` frontmatter string, then the section's own
    '# heading', then fall back to its filename. (Formerly shloka_num:/
    karika_num: — replaced by the single `ref:` string; those old keys
    are no longer read.)"""
    if fm.get("ref"):
        return str(fm["ref"])
    m = SECTION_HEADING_RE.search(body)
    if m:
        return m.group(1).strip()
    return fallback_stem


TOPIC_LINK_TMPL = "- [{title}]({link})"


def build_shloka_table(
    chapter: Chapter, all_shlokas: list[Shloka], chandas: dict[str, "TableEntry"], alankaras: dict[str, "TableEntry"]
) -> list[str]:
    """Renders the श्लोकसूची table shared by both kavya and shastra
    chapter pages — one row per shloka in `all_shlokas` (1-indexed =>
    anchor "#s{i}", matching extract_shlokas' numbering), linking its
    meter/alankaras to their glossary pages where recognized. Returns []
    if there are no shlokas at all (nothing to show)."""
    def link_chandas(m: str) -> str:
        if not m:
            return "—"
        if m not in chandas:
            return m
        return f"[{m}]({rel_link(chapter.rel_out_file, chandas[m].rel_out_file)})"

    def link_alankaras(items: list[str]) -> str:
        if not items:
            return "—"
        out = []
        for a in items:
            if a in alankaras:
                out.append(f"[{a}]({rel_link(chapter.rel_out_file, alankaras[a].rel_out_file)})")
            else:
                out.append(a)
        return ", ".join(out)

    if not all_shlokas:
        return []
    table_lines = [f"## {site_label('shloka_list_heading', 'श्लोकसूची')}", "", "| श्लोकः | छन्दः | अलङ्काराः |", "| --- | --- | --- |"]
    for i, sh in enumerate(all_shlokas, start=1):
        table_lines.append(
            f"| [{sh.preview}](#s{i}) | {link_chandas(sh.chandas)} | {link_alankaras(sh.alankaras)} |"
        )
    table_lines += ["", "---", ""]
    return table_lines


def render_shastra_chapter(
    chapter: Chapter, topics: dict[str, RefPage], chandas: dict[str, "TableEntry"], alankaras: dict[str, "TableEntry"]
) -> tuple[str, list[Shloka]]:
    """Returns (rendered_markdown, all_shlokas_in_anchor_order). Shastra
    chapters get the same श्लोकसूची table as kavya ones (see
    build_shloka_table) whenever the chapter has any shlokas at all —
    shown after the सम्बद्धाः विषयाः back-links, at the end of the page."""
    seen_topics: list[str] = []
    body_parts = []
    all_shlokas: list[Shloka] = []
    shloka_counter = 0
    for i, section in enumerate(chapter.sections):
        raw = section.read_text(encoding="utf-8")
        fm, body = split_frontmatter(raw)
        label = section_label(fm, body, section.stem)
        for t in as_list(fm.get("topics")):
            if t not in topics:
                warn(f"{section} references unknown topic '{t}' (no matching topics/*.md title)")
                continue
            if t not in seen_topics:
                seen_topics.append(t)
            anchor = f"sec{i+1}"
            topics[t].references.append(Reference(t, chapter, anchor, label))
        anchor = f"sec{i+1}"
        body, reordered = process_content_sections(
            body, chapter.default_class, source_for_warning=section, label_overrides=chapter.text.gloss_labels
        )
        # shastra sections aren't laid out with a Shloka Table the way
        # kavya ones are, but they can still carry `<div class="shloka">`
        # blocks (e.g. shastra-karika/shastra-vada texts) — resolve
        # data-type via the chapter's default_shloka_type the same way
        # kavya does, and number them contiguously across the chapter so
        # each gets a unique #s{n} anchor for chandas/alankara back-refs.
        fm_chandas = str(fm.get("chandas", "")).strip()
        body, shlokas, shloka_counter = extract_shlokas(
            body, fm_chandas, as_list(fm.get("alankara")), chapter.default_shloka_type,
            shloka_counter, source_for_warning=section,
        )
        for sh in shlokas:
            if sh.chandas and sh.chandas not in chandas:
                warn(f"{section} references unknown meter '{sh.chandas}' (no matching <!-- chandas-name --> row in the chandas glossary)")
            for a in sh.alankaras:
                if a not in alankaras:
                    warn(f"{section} references unknown alankara '{a}' (no matching <!-- alankara-name --> row in the alankara glossary)")
        all_shlokas.extend(shlokas)
        section_content = f'<div id="{anchor}"></div>\n\n{insert_reordered_sections(body, reordered)}'
        body_parts.append(section_content)

    header = []
    if seen_topics:
        header.append(f"## {site_label('related_topics_heading', 'सम्बद्धाः विषयाः')}")
        header.append("")
        for t in seen_topics:
            link = rel_link(chapter.rel_out_file, topics[t].rel_out_file)
            header.append(TOPIC_LINK_TMPL.format(title=t, link=link))
        header.append("")
        header.append("---")
        header.append("")

    up_target = f"{chapter.text.rel_out_dir}/index.md"
    topnav = render_topnav(chapter.rel_out_file, up_target, chapter.text.title)
    title_line = f"# {chapter.text.title} — {chapter.nav_label}"
    table_lines = build_shloka_table(chapter, all_shlokas, chandas, alankaras)
    content = "\n".join([topnav, title_line, ""] + header + body_parts + [""] + table_lines) + "\n"
    return content, all_shlokas


def render_kavya_chapter(
    chapter: Chapter, chandas: dict[str, "TableEntry"], alankaras: dict[str, "TableEntry"]
) -> tuple[str, list[Shloka]]:
    """Returns (rendered_markdown, all_shlokas_in_anchor_order). The same
    `all_shlokas` list (1-indexed => anchor "s{i}") is used both for the
    Shloka Table on this page and for recording back-references on the
    corresponding chandas/alankara pages, so anchors always line up."""
    body_parts = []
    all_shlokas: list[Shloka] = []
    counter = 0
    for section in chapter.sections:
        raw = section.read_text(encoding="utf-8")
        fm, body = split_frontmatter(raw)
        fm_chandas = str(fm.get("chandas", "")).strip()
        fm_alankaras = as_list(fm.get("alankara"))
        body, reordered = process_content_sections(
            body, chapter.default_class, source_for_warning=section, label_overrides=chapter.text.gloss_labels
        )
        new_body, shlokas, counter = extract_shlokas(
            body, fm_chandas, fm_alankaras, chapter.default_shloka_type, counter, source_for_warning=section
        )
        for sh in shlokas:
            if sh.chandas and sh.chandas not in chandas:
                warn(f"{section} references unknown meter '{sh.chandas}' (no matching <!-- chandas-name --> row in the chandas glossary)")
            for a in sh.alankaras:
                if a not in alankaras:
                    warn(f"{section} references unknown alankara '{a}' (no matching <!-- alankara-name --> row in the alankara glossary)")
        all_shlokas.extend(shlokas)
        body_parts.append(insert_reordered_sections(new_body, reordered))

    up_target = f"{chapter.text.rel_out_dir}/index.md"
    topnav = render_topnav(chapter.rel_out_file, up_target, chapter.text.title)
    title_line = f"# {chapter.text.title} — {chapter.nav_label}"
    table_lines = build_shloka_table(chapter, all_shlokas, chandas, alankaras)
    content = "\n".join([topnav, title_line, ""] + body_parts + [""] + table_lines) + "\n"
    return content, all_shlokas


def record_shloka_references(chapter: Chapter, chandas: dict, alankaras: dict, shlokas_with_index: list[tuple[int, Shloka]]):
    """Registers each shloka's data-chandas=/data-alankara= (or its
    section's chandas:/alankara: frontmatter fallback) against the
    matching chandas/alankara glossary entry, so that entry's own page
    lists this chapter under सन्दर्भाः. Shared by both kavya and shastra
    chapters — a shastra shloka (e.g. in a shastra-karika/shastra-vada
    text) can cite a meter/alankara exactly like a kavya one."""
    for i, sh in shlokas_with_index:
        anchor = f"s{i}"
        if sh.chandas and sh.chandas in chandas:
            chandas[sh.chandas].references.append(Reference(sh.chandas, chapter, anchor, sh.preview))
        for a in sh.alankaras:
            if a in alankaras:
                alankaras[a].references.append(Reference(a, chapter, anchor, sh.preview))


H1_RE = re.compile(r"^\s*#\s+\S")


def render_ref_page(page: RefPage) -> str:
    # "Up" goes to the विषयाः listing (other topics), NOT to the section's
    # texts listing one level further up — those are a different, sibling
    # menu, not this topic's parent.
    up_target = f"{TOPICS_SECTION.dir}/topics/index.md" if TOPICS_SECTION else None
    up_label = TOPICS_SECTION.h2_topics_label if TOPICS_SECTION else None
    parts = [render_topnav(page.rel_out_file, up_target, up_label)]
    body = page.body.strip()
    if not H1_RE.match(body):
        # only add a title heading if the source body doesn't already
        # start with one (some reference pages, like the topics/*.md
        # samples, already include their own '# ...' heading).
        parts.append(f"# {page.title}")
        parts.append("")
    parts.append(body)
    if page.references:
        parts.append("")
        parts.append(f"## {site_label('references_heading', 'सन्दर्भाः')}")
        parts.append("")
        for ref in page.references:
            label = f"{ref.chapter.text.title} — {ref.chapter.nav_label}"
            link = rel_link(page.rel_out_file, ref.chapter.rel_out_file) + f"#{ref.anchor}"
            parts.append(f"- [{ref.preview}]({link}) — {label}")
    parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Home page — one card per configured content section (see site_config.yaml
# content_sections:), each listing that section's texts (and, for the
# topics-carrying section, its विषयाः too). Cards are plain <div
# class="sv-home-card">...</div> — docs/stylesheets/custom.css draws the
# box; add a new section to site_config.yaml and its card just appears.
# ---------------------------------------------------------------------------

def build_home_page(
    sections_with_texts: list[tuple[SectionConfig, list[Text]]],
    topic_nav_entries: list,
) -> str:
    home_title = site_label("home_title", "मुखपृष्ठम्")
    lines = [f"# {home_title}", "", '<div class="sv-home-cards" markdown="1">', ""]

    for section, texts in sections_with_texts:
        lines.append('<div class="sv-home-card" markdown="1">')
        lines.append("")
        lines.append(f"## {section.h1_label}")
        lines.append("")
        lines.append(f"### {section.h2_text_label}")
        lines.append("")
        for t in texts:
            lines.append(f"- [{t.title}]({t.rel_out_dir}/index.md)")
        lines.append("")
        if section.has_topics and topic_nav_entries:
            lines.append(f"### {section.h2_topics_label}")
            lines.append("")
            for entry in topic_nav_entries:
                lines.append(f"- [{entry.title}]({entry.rel_out_file})")
            lines.append("")
        lines.append("</div>")
        lines.append("")

    lines.append("</div>")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# mkdocs.yml — static settings + auto-generated nav
# ---------------------------------------------------------------------------

NAV_HEADER = """\
# THIS FILE IS AUTO-GENERATED by scripts/generate_indices.py — do not edit
# the `nav:` block by hand, it will be overwritten on the next run. Static
# settings below `nav:` are safe to edit; the script only rewrites the
# `nav:` list itself each time it runs.
"""

MKDOCS_STATIC_TMPL = """\
site_name: {site_name}
docs_dir: docs

theme:
  name: material
  language: en
  features:
    # navigation.tabs is deliberately OFF: the top bar is just the two
    # buttons rendered by render_topnav() (Home / Up-to-TOC) on every
    # page, not a tab per top-level section — see site update notes.
    - navigation.instant
    - navigation.indexes
    - navigation.top
    - toc.follow
    - search.suggest
  palette:
    - scheme: default
      primary: {primary}
      accent: {accent}

extra_css:
  - stylesheets/custom.css

extra_javascript:
  - javascripts/notes-toggle.js

markdown_extensions:
  - attr_list
  - md_in_html
  - tables
  - def_list
  - footnotes
  - admonition
  - pymdownx.details
  - pymdownx.superfences:
      # Enables ```mermaid fenced code blocks (flowcharts, sequence
      # diagrams, etc.) anywhere in any source .md file — Material for
      # MkDocs renders them client-side, no extra_javascript needed.
      custom_fences:
        - name: mermaid
          class: mermaid
          format: !!python/name:pymdownx.superfences.fence_code_format
  - toc:
      permalink: true

# The chandas/alankara detail pages under topics/_chandas/ and
# topics/_alankara/ are intentionally not linked from nav (only reachable
# by clicking an entry's name in topics/chandas.md / alankara.md), so tell
# MkDocs not to warn/fail on them under --strict.
validation:
  nav:
    omitted_files: ignore
"""


def build_mkdocs_static() -> str:
    """site_name/palette come from scripts/site_config.yaml (falls back to
    sensible defaults if that file is missing or a key is absent)."""
    theme_cfg = SITE_CONFIG.get("theme", {}) or {}
    return MKDOCS_STATIC_TMPL.format(
        site_name=SITE_CONFIG.get("site_name") or "साहित्यशास्त्रम्",
        primary=theme_cfg.get("primary") or "deep orange",
        accent=theme_cfg.get("accent") or "amber",
    )


def yaml_dump_nav(nav) -> str:
    return yaml.dump({"nav": nav}, allow_unicode=True, sort_keys=False, default_flow_style=False)


def build_nav(
    sections_with_texts: list[tuple[SectionConfig, list[Text]]],
    topic_nav_entries: list,
) -> list:
    def text_nav(t: Text):
        entry = [{site_label("intro_nav_label", "परिचयः"): f"{t.rel_out_dir}/index.md"}]
        for ch in t.chapters:
            entry.append({ch.nav_label: ch.rel_out_file})
        return {t.title: entry}

    nav = [{site_label("home_nav_label", "मुखपृष्ठम्"): "index.md"}]
    for section, texts in sections_with_texts:
        # Give the section an explicit landing page as its own first
        # entry — without one, MkDocs makes any nav group link to the
        # first descendant page it finds, which looks like the section
        # only shows its first text.
        entries = [
            {section.h1_label: f"{section.dir}/index.md"},
            {section.h2_text_label: [text_nav(t) for t in texts]},
        ]
        if section.has_topics and topic_nav_entries:
            entries.append(
                {section.h2_topics_label: [
                    {section.h2_topics_label: f"{section.dir}/topics/index.md"},
                    *({e.title: e.rel_out_file} for e in topic_nav_entries),
                ]}
            )
        nav.append({section.h1_label: entries})
    return nav


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------

def main():
    if not SECTIONS:
        print("No content_sections configured in site_config.yaml — nothing to build.", file=sys.stderr)
        sys.exit(1)

    clean_output()
    n_assets = copy_assets()

    topics: dict[str, RefPage] = {}
    chandas: dict[str, TableEntry] = {}
    alankaras: dict[str, TableEntry] = {}
    topic_nav_entries: list = []
    topics_rel_dir = f"{TOPICS_SECTION.dir}/topics" if TOPICS_SECTION else ""

    if TOPICS_SECTION:
        topics = discover_ref_pages(
            "topic", TOPICS_SECTION.topics_src, topics_rel_dir, exclude={"chandas.md", "alankara.md"}
        )
        chandas, chandas_body, chandas_page_fm = build_glossary_page(
            "chandas", TOPICS_SECTION.topics_src / "chandas.md", topics_rel_dir
        )
        alankaras, alankara_body, alankara_page_fm = build_glossary_page(
            "alankara", TOPICS_SECTION.topics_src / "alankara.md", topics_rel_dir
        )

        # विषयाः listing: regular topics plus the chandas/alankara glossary
        # pages themselves, all sorted together the same way (order:
        # frontmatter, falling back to title).
        topic_nav_entries = list(topics.values())
        topic_nav_entries.append(
            NavListEntry(
                str(chandas_page_fm.get("title", "chandas")).strip(),
                f"{topics_rel_dir}/chandas.md",
                chandas_page_fm,
                TOPICS_SECTION.topics_src / "chandas.md",
            )
        )
        topic_nav_entries.append(
            NavListEntry(
                str(alankara_page_fm.get("title", "alankara")).strip(),
                f"{topics_rel_dir}/alankara.md",
                alankara_page_fm,
                TOPICS_SECTION.topics_src / "alankara.md",
            )
        )
        topic_nav_entries.sort(key=lambda e: e.sort_key)

    # --- discover every configured section's texts + chapters ------------
    sections_with_texts: list[tuple[SectionConfig, list[Text]]] = []
    for section in SECTIONS:
        texts = discover_texts(section)
        for t in texts:
            expected_types = (
                (SHASTRA_TEXT_TYPES if section is TOPICS_SECTION else KAVYA_PROSE_TYPES | KAVYA_VERSE_TYPES)
            )
            if t.type not in expected_types:
                warn(f"{t.dir}/meta.yaml has unrecognized type '{t.type}' (expected one of {sorted(expected_types)})")
            t.chapters = discover_chapters(t)
        sections_with_texts.append((section, texts))

    # --- render chapters + text index pages for every section ------------
    # Whether a text renders "shastra-style" (topic back-links,
    # paragraph-flow sections) or "kavya-style" (shloka table, meter/
    # alankara linking) is driven by the text's own `type:` — not by which
    # section it's filed under — so a future section can freely mix either
    # kind of text.
    for section, texts in sections_with_texts:
        for t in texts:
            for ch in t.chapters:
                if t.type in SHASTRA_TEXT_TYPES:
                    content, all_shlokas = render_shastra_chapter(ch, topics, chandas, alankaras)
                    write_md(ch.out_file, content)
                    record_shloka_references(ch, chandas, alankaras, list(enumerate(all_shlokas, start=1)))
                elif t.type in KAVYA_PROSE_TYPES | KAVYA_VERSE_TYPES:
                    content, all_shlokas = render_kavya_chapter(ch, chandas, alankaras)
                    write_md(ch.out_file, content)
                    record_shloka_references(ch, chandas, alankaras, list(enumerate(all_shlokas, start=1)))
                else:
                    # unrecognized type already warned about above — treat
                    # it like a kavya-verse text (no topic back-links) so
                    # the build still completes.
                    content, all_shlokas = render_kavya_chapter(ch, chandas, alankaras)
                    write_md(ch.out_file, content)
                    record_shloka_references(ch, chandas, alankaras, list(enumerate(all_shlokas, start=1)))
            write_md(t.out_dir / "index.md", build_text_index_page(t))

        write_md(DOCS / f"{section.dir}/index.md", build_domain_index_page(section, texts, topic_nav_entries))

    # --- topics index page (the "Up" target for individual topic pages
    # and for the chandas/alankara glossary listing pages) ----------------
    if TOPICS_SECTION and topic_nav_entries:
        write_md(
            DOCS / f"{TOPICS_SECTION.dir}/topics/index.md",
            build_topics_index_page(TOPICS_SECTION, topic_nav_entries),
        )

    # --- topic pages: write with injected back-links ----------------------
    for title, page in topics.items():
        write_md(page.out_file, render_ref_page(page))

    # --- chandas/alankara glossary pages + their (nav-less) detail pages --
    if TOPICS_SECTION:
        chandas_rel = f"{topics_rel_dir}/chandas.md"
        alankara_rel = f"{topics_rel_dir}/alankara.md"
        # "Up" from a glossary listing page goes to विषयाः (other topics),
        # matching every individual topic page — not to the texts listing.
        up_target = f"{TOPICS_SECTION.dir}/topics/index.md"
        up_label = TOPICS_SECTION.h2_topics_label
        chandas_title = str(chandas_page_fm.get("title", "chandas")).strip()
        alankara_title = str(alankara_page_fm.get("title", "alankara")).strip()
        write_md(DOCS / chandas_rel, render_topnav(chandas_rel, up_target, up_label) + "\n" + chandas_body)
        write_md(DOCS / alankara_rel, render_topnav(alankara_rel, up_target, up_label) + "\n" + alankara_body)
        for entry in chandas.values():
            entry.listing_title = chandas_title
            write_md(entry.out_file, render_glossary_entry_page(entry))
        for entry in alankaras.values():
            entry.listing_title = alankara_title
            write_md(entry.out_file, render_glossary_entry_page(entry))

    # --- home page ---------------------------------------------------------
    write_md(DOCS / "index.md", build_home_page(sections_with_texts, topic_nav_entries))

    # --- mkdocs.yml (nav auto-generated, static settings preserved) -------
    nav = build_nav(sections_with_texts, topic_nav_entries)
    mkdocs_yml = NAV_HEADER + "\n" + yaml_dump_nav(nav) + "\n" + build_mkdocs_static()
    write(ROOT / "mkdocs.yml", mkdocs_yml)

    n_texts = sum(len(texts) for _, texts in sections_with_texts)
    print(f"\nDone. {n_texts} text(s) across {len(SECTIONS)} section(s), "
          f"{len(topics)} topic(s), {len(chandas)} meter(s), {len(alankaras)} alankara(s), "
          f"{n_assets} asset file(s).")
    if WARNINGS:
        print(f"\n{len(WARNINGS)} warning(s) were printed above — please review.", file=sys.stderr)


if __name__ == "__main__":
    main()
