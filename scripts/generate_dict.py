#!/usr/bin/env python3
"""
generate_dict.py
==================

Walks every book/chapter with a `dict:` block in its meta.yaml and
writes dict/<folder>/<book>/<chapter>.txt (+ <chapter>-full.txt when
`chapter_key` is set) for the external dictionary-generation workflow.
Reads SOURCE .md files directly (never docs/), exactly like
generate_indices.py does — this script and that one are independent,
each doing its own pass over the same source tree; neither depends on
the other having run first. Import-only reuse of generate_indices.py
(discovery: SECTIONS/discover_texts/discover_chapters/Text/Chapter/
split_frontmatter/expand_gloss_shorthand) plus dict_extract.py
(<dict>/<dictref> extraction) and dict_render.py (.action/gloss
rendering, shloka key generation).

Any malformed <dict>/<dictref>/shloka-key input raises (DictSyntaxError
/ ShlokaKeyError propagate straight out of main() with a non-zero exit
code) — there's too much content to eyeball, so a broken build with a
precise file/reason is far safer than silently generating wrong or
partial dictionary data. See dict_extract.py's own docstring for
exactly which cases are fatal.

STATUS — both notes-format and shloka-format are implemented (including
each format's chapter_key full-chapter entry). Confirmed via live
testing against MG's real content (not just the doc's worked example):
  - Whenever `shloka_key_prefix` is set on a dict.type: shloka chapter,
    every shloka's own generated "e:NAME-..." key is prepended to its
    own record's "+" line (as an extra headword, ahead of its syns) —
    this doubles as the lookup target for the bword:// links used in
    the chapter_key full-chapter view (see wrap_marker_with_link,
    which strips the "e:" prefix for the href specifically). So
    shloka_key_prefix now matters independently of chapter_key — set
    it whenever you want per-shloka keys at all, whether or not you
    also want the aggregated full-chapter view.
  - "++" is exactly the अन्वयः-type gloss's own content (tags stripped,
    whitespace collapsed to single spaces); omitted entirely when no
    अन्वयः gloss exists for that shloka.
  - A shloka's own gloss divs are everything between its <div
    class="shloka"> and the NEXT such div (or end of the section) —
    only applies for dict.type: shloka (a shloka inside a dict.type:
    notes chapter gets no special treatment at all — see
    render_structural_divs in dict_render.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

import generate_indices as gi
import dict_extract as de
import dict_render as dr

DICT_ROOT = gi.ROOT / "dict"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class DictConfig:
    """This chapter's own `dict:` block, plus its book's `dict.folder`.
    A chapter with no `dict:` block at all (or no `type:` in it) is not
    dict-enabled — see is_enabled."""

    def __init__(self, text: gi.Text, chapter: gi.Chapter):
        book_dict = text.meta.get("dict") or {}
        if not isinstance(book_dict, dict):
            raise ValueError(f"{text.dir}/meta.yaml: `dict:` must be a mapping (e.g. `dict:\\n  folder: kavya`), got {book_dict!r}")
        self.folder = str(book_dict.get("folder", "")).strip()

        block = chapter.meta.get("dict") or {}
        if not isinstance(block, dict):
            raise ValueError(
                f"{text.dir}/{chapter.slug}/meta.yaml: `dict:` must be a mapping "
                f"(e.g. `dict:\\n  type: shloka\\n  shloka_key_prefix: ...`), got {block!r}"
            )
        self.type = str(block.get("type", "")).strip().lower()
        self.title = str(block.get("title", "")).strip()
        self.skip = gi.as_list(block.get("skip"))
        self.auto_shloka = bool(block.get("auto_shloka", True))
        self.chapter_key = str(block.get("chapter_key", "")).strip()
        self.shloka_key_prefix = str(block.get("shloka_key_prefix", "")).strip()

    @property
    def is_enabled(self) -> bool:
        return bool(self.type)


def out_dir_for(config: DictConfig, text: gi.Text) -> Path:
    return DICT_ROOT / config.folder / text.slug


# ---------------------------------------------------------------------------
# Notes format
# ---------------------------------------------------------------------------

def notes_record(syns: list[str], entry_text: str) -> str:
    return f"- {';'.join(syns)}\n{entry_text}"


def process_notes_chapter(text: gi.Text, chapter: gi.Chapter, config: DictConfig) -> None:
    records: list[str] = []
    full_chapter_parts: list[str] = []  # site-displayed body of every section, for chapter_key

    for section in chapter.sections:
        raw = section.read_text(encoding="utf-8")
        fm, body = gi.split_frontmatter(raw)
        body = gi.expand_gloss_shorthand(body, text.effective_gloss_types, source_for_warning=section)
        site_body, captures = de.extract_dict_and_ref_tags(body, source_for_warning=section)

        for cap in captures:
            if not cap.syns:
                gi.warn(f"{section}: <dict> entry with no syns= — skipping (nothing to key it by)")
                continue
            entry_text = dr.render_notes_entry(cap.raw_content, text.effective_gloss_types, source_for_warning=section)
            records.append(notes_record(cap.syns, entry_text))

        if config.chapter_key:
            full_chapter_parts.append(site_body.strip())

    out_dir = out_dir_for(config, text)
    out_dir.mkdir(parents=True, exist_ok=True)

    header = [f"HEADER:title={text.title} {config.title}", "HEADER:type=notes", f"HEADER:skip={';'.join(config.skip)}"]
    content = "\n".join(header) + "\n" + "\n\n".join(records) + ("\n" if records else "")
    out_path = out_dir / f"{chapter.slug}.txt"
    out_path.write_text(content, encoding="utf-8")
    print(f"wrote {out_path} ({len(records)} record(s))")

    if config.chapter_key:
        full_body = "\n\n".join(full_chapter_parts)
        full_entry = dr.render_notes_entry(full_body, text.effective_gloss_types, source_for_warning=chapter.text.dir)
        full_content = "\n".join(header) + "\n" + notes_record([config.chapter_key], full_entry) + "\n"
        full_path = out_dir / f"{chapter.slug}-full.txt"
        full_path.write_text(full_content, encoding="utf-8")
        print(f"wrote {full_path} (1 record)")


# ---------------------------------------------------------------------------
# Shloka format
# ---------------------------------------------------------------------------

def wrap_marker_with_link(shloka_text: str, key: str) -> str:
    """The chapter_key full-chapter view's own transform: the shloka's
    LAST ॥...॥ marker gets wrapped in a bword:// link to its key — per
    the spec's own example, the href drops the "e:" prefix that
    shloka_dict_key always returns (`e:KS-03-05` -> `bword://KS-03-05`)."""
    href_key = key[2:] if key.startswith("e:") else key
    matches = list(dr.MARKER_RE.finditer(shloka_text))
    m = matches[-1]
    return f'{shloka_text[:m.start()]}<a href="bword://{href_key}">{m.group(0)}</a>{shloka_text[m.end():]}'


def process_shloka_chapter(text: gi.Text, chapter: gi.Chapter, config: DictConfig) -> None:
    records: list[str] = []
    full_chapter_parts: list[str] = []  # for chapter_key, back-to-back shloka text (linked if shloka_key_prefix set)

    if config.chapter_key and not config.shloka_key_prefix:
        gi.warn(
            f"{text.dir}/{chapter.slug}: chapter_key is set but shloka_key_prefix is not — the full-chapter "
            f"entry will still be generated, but with no <a href=\"bword://...\"> links on any shloka"
        )

    for section in chapter.sections:
        raw = section.read_text(encoding="utf-8")
        fm, body = gi.split_frontmatter(raw)
        body = gi.expand_gloss_shorthand(body, text.effective_gloss_types, source_for_warning=section)
        # dict.type: shloka doesn't use <dict> tags at all (synonyms/skip come from
        # frontmatter or the shloka div's own attrs) — but a bare <dictref> can still
        # appear (e.g. right after a shloka's own ॥...॥ marker, inside its div), so
        # this call still matters, purely for that.
        body, captures = de.extract_dict_and_ref_tags(body, source_for_warning=section)
        if captures:
            gi.warn(
                f"{section}: <dict> tag found in a dict.type: shloka chapter — shloka format doesn't use "
                f"<dict> tags (synonyms/skip come from frontmatter or the shloka div's own attrs); ignored"
            )

        fm_dict = fm.get("dict") or {}
        file_syns = gi.as_list(fm_dict.get("syns"))
        file_skip = gi.as_list(fm_dict.get("skip"))

        tree = gi.parse_divs(body)
        shloka_nodes = [n for n in tree if n.base_cls == "shloka"]

        for i, node in enumerate(shloka_nodes):
            attrs = gi.parse_attrs(node.attrs_str)
            div_syns = de.as_syn_list(attrs.get("syns", ""))
            div_skip = de.as_syn_list(attrs.get("skip", ""))
            syns = div_syns or file_syns
            skip = div_skip or file_skip
            if not syns:
                gi.warn(f"{section}: shloka with no syns= (div attr or frontmatter) — skipping (nothing to key it by)")
                continue

            shloka_raw = body[node.tag_end:node.inner_end]
            _, shloka_text = de.resolve_dictrefs_in_text(shloka_raw, source_for_warning=section)
            shloka_text = shloka_text.strip()

            key = None
            if config.shloka_key_prefix:
                key = dr.shloka_dict_key(shloka_raw, config.shloka_key_prefix, source_for_warning=section)

            group_start = node.end
            group_end = shloka_nodes[i + 1].start if i + 1 < len(shloka_nodes) else len(body)
            group_text = body[group_start:group_end]
            anvaya, blocks = dr.render_shloka_group(group_text, text.effective_gloss_types, source_for_warning=section)

            # the key doubles as a headword so a bword:// link elsewhere (e.g. the
            # full-chapter view below) can resolve straight to this record
            record_syns = [key] + syns if key else syns
            records.append(dr.shloka_record(shloka_text, skip, record_syns, anvaya, blocks))

            if config.chapter_key:
                if key:
                    full_chapter_parts.append(wrap_marker_with_link(shloka_text, key))
                else:
                    full_chapter_parts.append(shloka_text)

    out_dir = out_dir_for(config, text)
    out_dir.mkdir(parents=True, exist_ok=True)

    header = [f"HEADER:title={text.title} {config.title}", "HEADER:type=shloka", f"HEADER:skip={';'.join(config.skip)}"]
    if hasattr(config, 'auto_shloka') and not config.auto_shloka:
        header.append(f"HEADER:auto_shloka=false")
    content = "\n".join(header) + "\n" + "\n\n".join(records) + ("\n" if records else "")
    out_path = out_dir / f"{chapter.slug}.txt"
    out_path.write_text(content, encoding="utf-8")
    print(f"wrote {out_path} ({len(records)} record(s))")

    if config.chapter_key:
        # "always of type notes" (spec) — the full-chapter view is a single
        # notes-style record, even for a dict.type: shloka chapter.
        full_header = [f"HEADER:title={text.title} {config.title}", "HEADER:type=notes", f"HEADER:skip={';'.join(config.skip)}"]
        full_body = "\n\n".join(full_chapter_parts)
        full_content = "\n".join(full_header) + "\n" + notes_record([config.chapter_key], full_body) + "\n"
        full_path = out_dir / f"{chapter.slug}-full.txt"
        full_path.write_text(full_content, encoding="utf-8")
        print(f"wrote {full_path} (1 record)")


# ---------------------------------------------------------------------------
# Chapter dispatch
# ---------------------------------------------------------------------------

def process_chapter(text: gi.Text, chapter: gi.Chapter) -> None:
    config = DictConfig(text, chapter)
    if not config.is_enabled:
        return
    if not config.folder:
        gi.warn(f"{text.dir}: dict-enabled chapter {chapter.slug} but book meta.yaml has no dict.folder — skipping")
        return

    if config.type == "notes":
        process_notes_chapter(text, chapter, config)
    elif config.type == "shloka":
        process_shloka_chapter(text, chapter, config)
    else:
        gi.warn(f"{text.dir}/{chapter.slug}: unknown dict.type '{config.type}' (expected 'notes' or 'shloka') — skipping")


def main() -> int:
    any_enabled = False
    for section in gi.SECTIONS:
        for text in gi.discover_texts(section):
            for chapter in gi.discover_chapters(text):
                try:
                    config = DictConfig(text, chapter)
                    if config.is_enabled:
                        any_enabled = True
                    process_chapter(text, chapter)
                except Exception as e:
                    print(f"\nFAILED while processing {text.dir}/{chapter.slug}: {e}", file=sys.stderr)
                    raise

    if not any_enabled:
        print("No dict-enabled chapters found (no chapter meta.yaml has a dict: block with a type:).")

    if gi.WARNINGS:
        print(f"\n{len(gi.WARNINGS)} warning(s) — see above.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
