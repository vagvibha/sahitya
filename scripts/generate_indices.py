#!/usr/bin/env python3
"""
generate_indices.py
====================

Pre-build generation script for the साहित्यशास्त्रम् MkDocs site.

What it does, in order:

1.  Reads `meta.yaml`/`meta.yml` for every text under `shastra/` and `kavya/`,
    and (optionally) for every individual chapter directory (`chapter_name`).
2.  Reads every topic page under `shastra/topics/` (their Devanagari
    `title:` frontmatter is the canonical key other files point back to via
    `topics:`), plus the two glossary pages `shastra/topics/chandas.md` and
    `shastra/topics/alankara.md`, each an HTML `<table>` of meters/alankaras
    (see `build_glossary_page` for the exact convention).
3.  Walks every chapter directory under each text, concatenates its section
    files into a single generated chapter page, and along the way:
      - collects every `topics:` reference (for shastra sections) so the
        chapter page can show back-links, and so each topic page can list
        every section that cites it;
      - extracts every `<div class="shloka">`/`<div class="shloka-play">`
        (attributes `data-meter=` and `data-alankara=`) and every
        verse-level `meter:`/`alankara:` frontmatter pair, builds a
        per-chapter Shloka Table, and records each occurrence against the
        matching row in the chandas/alankara glossary.
4.  Writes the generated chapter pages, per-text landing pages, topic pages,
    the two glossary pages, and one auto-generated (nav-less) detail page
    per meter/alankara into `docs/` (source files under `shastra/` and
    `kavya/` at the repo root are never modified).
5.  Writes `docs/index.md` (home page).
6.  Writes `mkdocs.yml`, including an auto-generated `nav:` block that
    reflects whatever texts/topics currently exist, so nav never needs to
    be hand-maintained.

This script is idempotent: it always starts by deleting only the generated
output directories (`docs/shastra`, `docs/kavya`, `docs/index.md` and
`mkdocs.yml`), never the hand-maintained `docs/stylesheets/`,
`docs/javascripts/`, or the `shastra/`/`kavya/` sources.

Requires: PyYAML, beautifulsoup4
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
SHASTRA_SRC = ROOT / "shastra"
KAVYA_SRC = ROOT / "kavya"
DOCS = ROOT / "docs"
SHASTRA_OUT = DOCS / "shastra"
KAVYA_OUT = DOCS / "kavya"

RESERVED_SHASTRA_DIRS = {"topics"}
META_FILENAMES = ("meta.yaml", "meta.yml")

KAVYA_PROSE_TYPES = {"kavya-play", "kavya-prose"}
KAVYA_VERSE_TYPES = {"kavya-verse"}
SHASTRA_TEXT_TYPES = {"shastra-karika", "shastra-vada"}

WARNINGS: list[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"WARNING: {msg}", file=sys.stderr)


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
# Numeric-aware sorting for chapter/section filenames (01, 02, ... 10, ...)
# ---------------------------------------------------------------------------

def numeric_key(stem: str):
    try:
        return (0, int(stem))
    except ValueError:
        return (1, stem)


def rel_link(from_rel_file: str, to_rel_file: str) -> str:
    """Relative link from the page at `from_rel_file` to `to_rel_file`,
    both given as paths relative to the docs root (e.g.
    'shastra/topics/_chandas/anustubh.md'). Using real relative-path math
    here (rather than assuming both pages sit at the same depth) matters
    once pages can live at different nesting depths, as the glossary
    detail pages now do.

    NOTE: this form (a plain relative path ending in .md) only works
    inside genuine Markdown link syntax `[text](...)` — MkDocs' own build
    rewrites those `.md` links into the correct clean-URL form for you.
    It does NOT work inside literal HTML `<a href="...">` (e.g. hand-built
    inside a raw <table>), because MkDocs never touches raw HTML — use
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
# Discovery: texts (shastra/<text>, kavya/<text>)
# ---------------------------------------------------------------------------

class Text:
    def __init__(self, slug: str, directory: Path, meta: dict, domain: str):
        self.slug = slug
        self.dir = directory
        self.meta = meta
        self.domain = domain  # "shastra" | "kavya"
        self.title = str(meta.get("title", slug)).strip()
        self.author = str(meta.get("author", "")).strip()
        self.type = str(meta.get("type", "")).strip()
        self.chapters: list["Chapter"] = []

    @property
    def out_dir(self) -> Path:
        return (SHASTRA_OUT if self.domain == "shastra" else KAVYA_OUT) / self.slug

    @property
    def rel_out_dir(self) -> str:
        return f"{self.domain}/{self.slug}"


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
    def nav_label(self) -> str:
        if self.meta.get("chapter_name"):
            return str(self.meta["chapter_name"]).strip()
        try:
            n = int(self.slug)
            word = str(self.text.meta.get("chapter_type", "")).strip() or (
                "अध्यायः" if self.text.domain == "shastra" else "भागः"
            )
            return f"{word} {n}"
        except ValueError:
            return self.slug


def discover_texts(src_root: Path, domain: str) -> list[Text]:
    texts = []
    if not src_root.exists():
        return texts
    for d in sorted(p for p in src_root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        if domain == "shastra" and d.name in RESERVED_SHASTRA_DIRS:
            continue
        meta_path = find_meta_file(d)
        if not meta_path:
            warn(f"{d} has no meta.yaml/meta.yml — skipping this text")
            continue
        meta = read_meta(d)
        if "title" not in meta:
            warn(f"{meta_path} has no 'title' — skipping this text")
            continue
        texts.append(Text(d.name, d, meta, domain))
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
        chapter_meta = read_meta(d)  # optional meta.yaml/meta.yml inside the chapter dir (chapter_name, ...)
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
# Discovery: reference pages (topics / chandas / alankara)
# ---------------------------------------------------------------------------

class RefPage:
    def __init__(self, kind: str, slug: str, path: Path, frontmatter: dict, body: str):
        self.kind = kind  # "topic"
        self.slug = slug
        self.path = path
        self.frontmatter = frontmatter
        self.body = body
        self.title = str(frontmatter.get("title", slug)).strip()
        self.references: list["Reference"] = []  # filled in during the scan

    @property
    def rel_out_file(self) -> str:
        return f"shastra/topics/{self.slug}.md"

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


def discover_ref_pages(kind: str, folder: Path, exclude: set[str] = frozenset()) -> dict[str, RefPage]:
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
        pages[title] = RefPage(kind, f.stem, f, fm, body)
    return pages


# ---------------------------------------------------------------------------
# Chandas / alankara glossary tables
# ---------------------------------------------------------------------------
#
# shastra/topics/chandas.md and shastra/topics/alankara.md are each a single
# hand-maintained page containing one or more HTML <table>s (one row per
# meter/alankara). A row counts as a matchable glossary entry if it carries
# an HTML comment `<!-- chandas-name -->` / `<!-- alankara-name -->`
# anywhere among that <tr>'s direct children — that comment is only a
# marker (its exact wording isn't otherwise used); the entry's canonical
# name is the exact text of that row's first <td>, and must match
# `meter:`/`alankara:` frontmatter or `data-meter=`/`data-alankara=`
# attributes exactly.
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
    shastra/topics/chandas.md or alankara.md."""

    def __init__(self, kind: str, title: str, slug: str, col_labels: list[str], col_html: list[str]):
        self.kind = kind
        self.title = title
        self.slug = slug
        self.col_labels = col_labels  # header text for each remaining column
        self.col_html = col_html  # that row's inner HTML for each remaining column
        self.references: list[Reference] = []

    @property
    def rel_out_file(self) -> str:
        return f"shastra/topics/{GLOSSARY_OUT_SUBDIR[self.kind]}/{self.slug}.md"

    @property
    def out_file(self) -> Path:
        return DOCS / self.rel_out_file


def build_glossary_page(kind: str, path: Path) -> tuple[dict[str, TableEntry], str]:
    """Returns (entries, rendered_body) for shastra/topics/{chandas,alankara}.md.
    `rendered_body` is the source body with every matched entry's name cell
    turned into a link to its (nav-less) detail page."""
    if not path.exists():
        warn(f"{path} does not exist — no {kind} entries will be available")
        return {}, ""

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
            has_marker = any(isinstance(c, Comment) and marker in c for c in tr.children)
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
            entries[name] = TableEntry(kind, name, slug, rest_labels, rest_html)

            href = raw_html_href(f"shastra/topics/{kind}.md", entries[name].rel_out_file)
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
    return entries, rendered


def render_glossary_entry_page(entry: TableEntry) -> str:
    parts = [f"# {entry.title}", ""]
    for label, html in zip(entry.col_labels, entry.col_html):
        if not html:
            continue
        if label:
            parts.append(f"**{label}**")
            parts.append("")
        parts.append(html)
        parts.append("")
    if entry.references:
        parts.append("## सन्दर्भाः")
        parts.append("")
        for ref in entry.references:
            label = f"{ref.chapter.text.title} — {ref.chapter.nav_label}"
            link = rel_link(entry.rel_out_file, ref.chapter.rel_out_file) + f"#{ref.anchor}"
            parts.append(f"- [{ref.preview}]({link}) — {label}")
    parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# HTML scanning helpers (tolerant of minor malformed markup in sources)
# ---------------------------------------------------------------------------

ATTR_RE = re.compile(r'([a-zA-Z\-]+)\s*=\s*"([^"]*)"')
SHLOKA_RE = re.compile(r'<div\s+class="(?:shloka|shloka-play)"([^>]*)>(.*?)</div\s*>?', re.IGNORECASE | re.DOTALL)


def parse_attrs(attr_str: str) -> dict:
    return {m.group(1): m.group(2) for m in ATTR_RE.finditer(attr_str)}


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
    def __init__(self, meter: str, alankaras: list[str], preview: str):
        self.meter = meter
        self.alankaras = alankaras
        self.preview = preview


def extract_shlokas(
    body: str, fm_meter: str, fm_alankaras: list[str], start_index: int = 0
) -> tuple[str, list[Shloka], int]:
    """Find every <div class="shloka"> in `body`, insert an anchor <a id=...>
    right before each one, and return (modified_body, [Shloka, ...], next_index).

    `start_index` lets callers number shlokas contiguously across every
    section in a chapter (anchors must be chapter-unique, since all
    sections end up concatenated onto a single generated chapter page and
    the Shloka Table numbers verses chapter-wide, not per-section).

    For a shloka div that has no data-meter/data-alankara of its own (the
    verse-only text style), the file-level frontmatter meter/alankara is
    used instead — this covers kavya-verse texts where one file == one
    shloka tagged only in frontmatter.
    """
    shlokas: list[Shloka] = []
    counter = start_index

    def repl(m: re.Match) -> str:
        nonlocal counter
        counter += 1
        attrs = parse_attrs(m.group(1))
        inner = m.group(2)
        meter = attrs.get("data-meter", "").strip() or fm_meter
        alankaras = (
            [a.strip() for a in attrs["data-alankara"].split(",") if a.strip()]
            if attrs.get("data-alankara")
            else list(fm_alankaras)
        )
        anchor = f"s{counter}"
        shlokas.append(Shloka(meter, alankaras, preview_text(inner)))
        return f'<a id="{anchor}"></a>\n{m.group(0)}'

    new_body = SHLOKA_RE.sub(repl, body)
    return new_body, shlokas, counter


TOPIC_LINK_TMPL = "- [{title}]({link})"

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


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------

def clean_output():
    if SHASTRA_OUT.exists():
        shutil.rmtree(SHASTRA_OUT)
    if KAVYA_OUT.exists():
        shutil.rmtree(KAVYA_OUT)
    index_md = DOCS / "index.md"
    if index_md.exists():
        index_md.unlink()
    DOCS.mkdir(parents=True, exist_ok=True)


def write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_md(path: Path, content: str):
    """Like write(), but also enables md_in_html on every <div> so inline
    Markdown (bold, links, etc.) inside content blocks renders correctly.
    Only ever used for generated docs/**/*.md files, never for mkdocs.yml."""
    content = convert_bracket_attr_spans(content)
    content = enable_markdown_in_divs(content)
    write(path, content)


def build_domain_index_page(title: str, domain_index_rel: str, texts: list[Text]) -> str:
    lines = [f"# {title}", ""]
    for t in texts:
        target = f"{t.rel_out_dir}/index.md"
        lines.append(f"- [{t.title}]({rel_link(domain_index_rel, target)})")
    lines.append("")
    return "\n".join(lines)


def build_text_index_page(text: Text) -> str:
    lines = [f"# {text.title}", ""]
    if text.author:
        lines += [f"**कर्ता:** {text.author}", ""]
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
    prefer an explicit karika_num, then the section's own '# heading',
    then fall back to its filename."""
    if fm.get("karika_num"):
        return str(fm["karika_num"])
    m = SECTION_HEADING_RE.search(body)
    if m:
        return m.group(1).strip()
    return fallback_stem


def render_shastra_chapter(chapter: Chapter, topics: dict[str, RefPage]) -> str:
    seen_topics: list[str] = []
    body_parts = []
    for i, section in enumerate(chapter.sections):
        raw = section.read_text(encoding="utf-8")
        fm, body = split_frontmatter(raw)
        label = section_label(fm, body, section.stem)
        for t in as_list(fm.get("topics")) or as_list(fm.get("topic")):
            if fm.get("topic") and not fm.get("topics"):
                warn(f"{section} uses 'topic:' instead of 'topics:' — please rename it (treating it as 'topics:' for now)")
            if t not in topics:
                warn(f"{section} references unknown topic '{t}' (no matching shastra/topics/*.md title)")
                continue
            if t not in seen_topics:
                seen_topics.append(t)
            anchor = f"sec{i+1}"
            topics[t].references.append(Reference(t, chapter, anchor, label))
        anchor = f"sec{i+1}"
        body_parts.append(f'<a id="{anchor}"></a>\n\n{body.strip()}')

    header = []
    if seen_topics:
        header.append("## सम्बद्धाः विषयाः")
        header.append("")
        for t in seen_topics:
            link = rel_link(chapter.rel_out_file, topics[t].rel_out_file)
            header.append(TOPIC_LINK_TMPL.format(title=t, link=link))
        header.append("")
        header.append("---")
        header.append("")

    title_line = f"# {chapter.text.title} — {chapter.nav_label}"
    return "\n".join([title_line, ""] + header + body_parts) + "\n"


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
        fm_meter = str(fm.get("meter", "")).strip()
        fm_alankaras = as_list(fm.get("alankara"))
        new_body, shlokas, counter = extract_shlokas(body, fm_meter, fm_alankaras, counter)
        for sh in shlokas:
            if sh.meter and sh.meter not in chandas:
                warn(f"{section} references unknown meter '{sh.meter}' (no matching <!-- chandas-name --> row in shastra/topics/chandas.md)")
            for a in sh.alankaras:
                if a not in alankaras:
                    warn(f"{section} references unknown alankara '{a}' (no matching <!-- alankara-name --> row in shastra/topics/alankara.md)")
        all_shlokas.extend(shlokas)
        body_parts.append(new_body.strip())

    def link_meter(m: str) -> str:
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

    table_lines = []
    if all_shlokas:
        table_lines.append("## श्लोकसूची")
        table_lines.append("")
        table_lines.append("| श्लोकः | छन्दः | अलङ्काराः |")
        table_lines.append("| --- | --- | --- |")
        for i, sh in enumerate(all_shlokas, start=1):
            table_lines.append(
                f"| [{sh.preview}](#s{i}) | {link_meter(sh.meter)} | {link_alankaras(sh.alankaras)} |"
            )
        table_lines.append("")
        table_lines.append("---")
        table_lines.append("")

    title_line = f"# {chapter.text.title} — {chapter.nav_label}"
    content = "\n".join([title_line, ""] + body_parts + [""] + table_lines) + "\n"
    return content, all_shlokas


def record_kavya_references(chapter: Chapter, chandas: dict, alankaras: dict, shlokas_with_index: list[tuple[int, Shloka]]):
    for i, sh in shlokas_with_index:
        anchor = f"s{i}"
        if sh.meter and sh.meter in chandas:
            chandas[sh.meter].references.append(Reference(sh.meter, chapter, anchor, sh.preview))
        for a in sh.alankaras:
            if a in alankaras:
                alankaras[a].references.append(Reference(a, chapter, anchor, sh.preview))


H1_RE = re.compile(r"^\s*#\s+\S")


def render_ref_page(page: RefPage) -> str:
    parts = []
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
        parts.append("## सन्दर्भाः")
        parts.append("")
        for ref in page.references:
            label = f"{ref.chapter.text.title} — {ref.chapter.nav_label}"
            link = rel_link(page.rel_out_file, ref.chapter.rel_out_file) + f"#{ref.anchor}"
            parts.append(f"- [{ref.preview}]({link}) — {label}")
    parts.append("")
    return "\n".join(parts)


def build_home_page(
    shastra_texts: list[Text],
    kavya_texts: list[Text],
    topics: dict[str, RefPage],
) -> str:
    lines = ["# साहित्यशास्त्रम्", ""]

    lines.append("## ग्रन्थाः")
    lines.append("")
    for t in shastra_texts:
        lines.append(f"- [{t.title}]({t.rel_out_dir}/index.md)")
    lines.append("")

    lines.append("## विषयाः")
    lines.append("")
    for title, page in sorted(topics.items()):
        lines.append(f"- [{title}]({page.rel_out_file})")
    lines.append("- [छन्दांसि](shastra/topics/chandas.md)")
    lines.append("- [अलङ्काराः](shastra/topics/alankara.md)")
    lines.append("")

    lines.append("# काव्यानि")
    lines.append("")
    for t in kavya_texts:
        lines.append(f"- [{t.title}]({t.rel_out_dir}/index.md)")
    lines.append("")

    return "\n".join(lines)


NAV_HEADER = """\
# THIS FILE IS AUTO-GENERATED by scripts/generate_indices.py — do not edit
# the `nav:` block by hand, it will be overwritten on the next run. Static
# settings below `nav:` are safe to edit; the script only rewrites the
# `nav:` list itself each time it runs.
"""

MKDOCS_STATIC = """\
site_name: साहित्यशास्त्रम्
docs_dir: docs

theme:
  name: material
  language: en
  features:
    - navigation.instant
    - navigation.tabs
    - navigation.tabs.sticky
    - navigation.indexes
    - navigation.top
    - toc.follow
    - search.suggest
  palette:
    - scheme: default
      primary: deep orange
      accent: amber

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
  - pymdownx.superfences
  - toc:
      permalink: true

# The chandas/alankara detail pages under shastra/topics/_chandas/ and
# _alankara/ are intentionally not linked from nav (only reachable by
# clicking an entry's name in shastra/topics/chandas.md / alankara.md), so
# tell MkDocs not to warn/fail on them under --strict.
validation:
  nav:
    omitted_files: ignore
"""


def yaml_dump_nav(nav) -> str:
    return yaml.dump({"nav": nav}, allow_unicode=True, sort_keys=False, default_flow_style=False)


def build_nav(
    shastra_texts: list[Text],
    kavya_texts: list[Text],
    topics: dict[str, RefPage],
) -> list:
    def text_nav(t: Text):
        entry = [{"परिचयः": f"{t.rel_out_dir}/index.md"}]
        for ch in t.chapters:
            entry.append({ch.nav_label: ch.rel_out_file})
        return {t.title: entry}

    # A section like "शास्त्रम्"/"काव्यम्" has no page of its own — without
    # one, MkDocs makes the top nav tab link to the first descendant page
    # it finds (e.g. the first text's intro), which looks like "the tab
    # only shows the first text". Giving the section an explicit landing
    # page as its very first entry fixes that; naming it identically to
    # the section title also lets Material's navigation.indexes feature
    # treat it as that section's own overview.
    shastra_section = [
        {"शास्त्रम्": "shastra/index.md"},
        {"ग्रन्थाः": [text_nav(t) for t in shastra_texts]},
        {
            "विषयाः": [
                {title: p.rel_out_file} for title, p in sorted(topics.items())
            ]
            + [
                {"छन्दांसि": "shastra/topics/chandas.md"},
                {"अलङ्काराः": "shastra/topics/alankara.md"},
            ]
        },
    ]
    kavya_section = [{"काव्यम्": "kavya/index.md"}] + [text_nav(t) for t in kavya_texts]

    nav = [
        {"मुखपृष्ठम्": "index.md"},
        {"शास्त्रम्": shastra_section},
        {"काव्यम्": kavya_section},
    ]
    return nav


def main():
    clean_output()

    topics = discover_ref_pages(
        "topic", SHASTRA_SRC / "topics", exclude={"chandas.md", "alankara.md"}
    )
    chandas, chandas_body = build_glossary_page("chandas", SHASTRA_SRC / "topics" / "chandas.md")
    alankaras, alankara_body = build_glossary_page("alankara", SHASTRA_SRC / "topics" / "alankara.md")

    shastra_texts = discover_texts(SHASTRA_SRC, "shastra")
    kavya_texts = discover_texts(KAVYA_SRC, "kavya")

    for t in shastra_texts:
        if t.type not in SHASTRA_TEXT_TYPES:
            warn(f"{t.dir}/meta.yaml has unrecognized type '{t.type}' (expected one of {sorted(SHASTRA_TEXT_TYPES)})")
        t.chapters = discover_chapters(t)

    for t in kavya_texts:
        if t.type not in KAVYA_PROSE_TYPES | KAVYA_VERSE_TYPES:
            warn(f"{t.dir}/meta.yaml has unrecognized type '{t.type}' (expected one of {sorted(KAVYA_PROSE_TYPES | KAVYA_VERSE_TYPES)})")
        t.chapters = discover_chapters(t)

    # --- shastra: render chapters, collect topic references -------------
    for t in shastra_texts:
        for ch in t.chapters:
            content = render_shastra_chapter(ch, topics)
            write_md(ch.out_file, content)
        write_md(t.out_dir / "index.md", build_text_index_page(t))

    # --- kavya: render chapters, collect meter/alankara references -------
    for t in kavya_texts:
        for ch in t.chapters:
            content, all_shlokas = render_kavya_chapter(ch, chandas, alankaras)
            write_md(ch.out_file, content)
            indexed = list(enumerate(all_shlokas, start=1))
            record_kavya_references(ch, chandas, alankaras, indexed)

        write_md(t.out_dir / "index.md", build_text_index_page(t))

    # --- शास्त्रम्/काव्यम् landing pages (fixes the top-tab-jumps-to-first-text bug) --
    write_md(DOCS / "shastra/index.md", build_domain_index_page("शास्त्रम्", "shastra/index.md", shastra_texts))
    write_md(DOCS / "kavya/index.md", build_domain_index_page("काव्यम्", "kavya/index.md", kavya_texts))

    # --- topic pages: write with injected back-links ----------------------
    for title, page in topics.items():
        write_md(page.out_file, render_ref_page(page))

    # --- chandas/alankara glossary pages + their (nav-less) detail pages --
    write_md(DOCS / "shastra/topics/chandas.md", chandas_body)
    write_md(DOCS / "shastra/topics/alankara.md", alankara_body)
    for entry in chandas.values():
        write_md(entry.out_file, render_glossary_entry_page(entry))
    for entry in alankaras.values():
        write_md(entry.out_file, render_glossary_entry_page(entry))

    # --- home page ---------------------------------------------------------
    write_md(DOCS / "index.md", build_home_page(shastra_texts, kavya_texts, topics))

    # --- mkdocs.yml (nav auto-generated, static settings preserved) -------
    nav = build_nav(shastra_texts, kavya_texts, topics)
    mkdocs_yml = NAV_HEADER + "\n" + yaml_dump_nav(nav) + "\n" + MKDOCS_STATIC
    write(ROOT / "mkdocs.yml", mkdocs_yml)

    print(f"\nDone. {len(shastra_texts)} shastra text(s), {len(kavya_texts)} kavya text(s), "
          f"{len(topics)} topic(s), {len(chandas)} meter(s), {len(alankaras)} alankara(s).")
    if WARNINGS:
        print(f"\n{len(WARNINGS)} warning(s) were printed above — please review.", file=sys.stderr)


if __name__ == "__main__":
    main()
