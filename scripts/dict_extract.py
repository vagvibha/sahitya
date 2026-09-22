#!/usr/bin/env python3
"""
dict_extract.py
================

Shared, single-pass extraction of the `<dict>` / `<dictref>` markdown
tags that drive the dictionary-generation workflow (see
scripts/generate_dict.py) — imported by BOTH generate_dict.py (which
needs the captured dictionary content) and generate_indices.py (which
only needs the site-safe stripped body), so the two can never drift
out of sync on what these tags mean. Zero dependency on
generate_indices.py itself (own copy of apply_splices), so this module
can be unit-tested standalone.

Tags handled
------------
<dict syns="a, b, c" display="False">...content...</dict>
    Paired form. display defaults to True (content stays on the site;
    that SAME content becomes the dict entry, after further .action/
    gloss-div processing — see render_dict_entry in generate_dict.py,
    which is deliberately NOT this module's job, since that rendering
    differs between the notes and shloka formats). With
    display="False" the entire tag+content is stripped from the site
    and the captured inner text becomes the dict entry as-is. Not
    nestable — a second <dict> opened before the first is closed is a
    hard error.

<dict syns="a" entry="..."/>
    Self-closing shortcut for a trivial entry: there is no content to
    show on the site either way, `entry=` is the dict entry text
    directly. display=, if present at all, must be "False" — anything
    else is a hard error (there's nothing for a self-closing tag to
    display="True", so that combination can only be an authoring
    mistake).

<dictref text="..." ref="ref"/>
    Self-closing, may appear anywhere — inside or outside a <dict>
    block, inside a shloka div, etc. Always stripped from the site.
    Wherever it appears inside dict-entry text, it's rewritten as
    `<a href="bword://ref">text</a>`.

Error handling: every malformed/structurally-invalid case below (bad
nesting, an unclosed or unopened <dict>, a <dictref> missing text= or
ref=, a contradictory display= on a self-closing <dict/>) raises
DictSyntaxError rather than warning and limping on — there's too much
content here to expect anyone to eyeball every generated page, so a
build-breaking failure with a precise location is much safer than a
warning that's easy to miss in a long build log.

Run this file directly for a couple of quick self-tests:
    python3 dict_extract.py
"""
from __future__ import annotations

import re
from dataclasses import dataclass


class DictSyntaxError(ValueError):
    """Raised for any malformed <dict>/<dictref> markup. `source` is
    whatever the caller passed as source_for_warning (typically the
    source .md Path) — always included in str(e), so a caller that just
    lets this propagate still gets a usable error message."""
    def __init__(self, source: object, message: str):
        self.source = source
        self.message = message
        super().__init__(f"{source}: {message}")


# ---------------------------------------------------------------------------
# Small helpers (deliberately duplicated from generate_indices.py rather
# than imported, so this module has no import-order dependency on it)
# ---------------------------------------------------------------------------

ATTR_RE = re.compile(r'([a-zA-Z\-]+)\s*=\s*"([^"]*)"')


def parse_attrs(attr_str: str) -> dict:
    return {m.group(1): m.group(2) for m in ATTR_RE.finditer(attr_str)}


def as_syn_list(value: str) -> list[str]:
    """Split a syns=/skip= attribute value on comma OR semicolon —
    content in the wild has used both; ';' never legitimately appears
    inside a synonym itself, so accepting either is safe. Whitespace
    around each piece is trimmed."""
    return [p.strip() for p in re.split(r"[,;]", value) if p.strip()]


def apply_splices(text: str, splices: list[tuple[int, int, str]]) -> str:
    """(start, end, replacement) spans applied in one pass; (end==start)
    is a pure insertion. Same contract as generate_indices.py's own
    apply_splices."""
    splices = sorted(splices, key=lambda s: s[0])
    out = []
    pos = 0
    for s, e, repl in splices:
        if s < pos:
            raise ValueError(f"overlapping splice at {s} (previous ended at {pos})")
        out.append(text[pos:s])
        out.append(repl)
        pos = e
    out.append(text[pos:])
    return "".join(out)


# ---------------------------------------------------------------------------
# Tag grammar
# ---------------------------------------------------------------------------

DICT_OPEN_RE = re.compile(r'<dict\b((?:[^>"]|"[^"]*")*?)(/?)>')
DICT_CLOSE_RE = re.compile(r"</dict\s*>")
DICTREF_RE = re.compile(r'<dictref\b((?:[^>"]|"[^"]*")*)/>')


@dataclass
class DictCapture:
    """One <dict>-tag-sourced dictionary entry, still in RAW form (not
    yet run through .action/gloss/shloka-div rendering — see
    render_dict_entry in generate_dict.py). `raw_content` already has
    any <dictref> inside it resolved to its bword link form."""
    syns: list[str]
    raw_content: str
    display: bool       # False => this content was NOT shown on the site
    self_closing: bool
    start: int           # offset in the ORIGINAL body, for warnings/debugging


def _dictref_replacement(attrs_str: str, source_for_warning) -> tuple[str, str]:
    """(site_replacement, dict_replacement) for one <dictref/> match."""
    attrs = parse_attrs(attrs_str)
    text = attrs.get("text", "")
    ref = attrs.get("ref", "")
    if not text or not ref:
        raise DictSyntaxError(source_for_warning, "<dictref> missing text= or ref=")
    return "", f'<a href="bword://{ref}">{text}</a>'


def resolve_dictrefs_in_text(text: str, source_for_warning: object = "") -> tuple[str, str]:
    """For text OUTSIDE any <dict> block — e.g. a shloka's own raw text
    in a shloka-format chapter, which can carry a <dictref/> right after
    its ending ॥...॥ marker (see ks5-02.md). Returns (site_text,
    dict_text): the first with every <dictref/> stripped, the second
    with every <dictref/> resolved to its link form."""
    site_splices, dict_splices = [], []
    for m in DICTREF_RE.finditer(text):
        site_repl, dict_repl = _dictref_replacement(m.group(1), source_for_warning)
        site_splices.append((m.start(), m.end(), site_repl))
        dict_splices.append((m.start(), m.end(), dict_repl))
    site_text = apply_splices(text, site_splices) if site_splices else text
    dict_text = apply_splices(text, dict_splices) if dict_splices else text
    return site_text, dict_text


def extract_dict_and_ref_tags(
    body: str, source_for_warning: object = "",
) -> tuple[str, list[DictCapture]]:
    """One pass over a raw section body — run AFTER expand_gloss_shorthand
    (so shorthand gloss tags inside a <dict> block are already real
    <div class="gloss" data-type="..."> divs) but BEFORE
    process_content_sections/extract_shlokas (which must never see a
    <dict>/<dictref> tag at all).

    Returns (site_body, captures):
      site_body   — safe to hand straight to the rest of
                    generate_indices.py's existing pipeline.
      captures    — one DictCapture per <dict> tag (paired or self-
                    closing), in document order, for generate_dict.py.
                    A bare <dictref> NOT inside any <dict> block does
                    NOT produce a capture here (it's still stripped
                    from site_body) — that stray text belongs to
                    whatever OTHER extraction path covers it (e.g. a
                    shloka-format chapter's per-shloka extraction —
                    see resolve_dictrefs_in_text, used there instead).
    """
    tokens: list[tuple[int, int, str, str, bool]] = []
    for m in DICT_OPEN_RE.finditer(body):
        tokens.append((m.start(), m.end(), "dict_open", m.group(1), m.group(2) == "/"))
    for m in DICT_CLOSE_RE.finditer(body):
        tokens.append((m.start(), m.end(), "dict_close", "", False))
    for m in DICTREF_RE.finditer(body):
        tokens.append((m.start(), m.end(), "dictref", m.group(1), True))
    tokens.sort(key=lambda t: t[0])

    site_splices: list[tuple[int, int, str]] = []
    dictref_dict_repl: dict[tuple[int, int], str] = {}

    for start, end, kind, attrs_str, _ in tokens:
        if kind != "dictref":
            continue
        site_repl, dict_repl = _dictref_replacement(attrs_str, source_for_warning)
        site_splices.append((start, end, site_repl))
        dictref_dict_repl[(start, end)] = dict_repl

    def resolve_dictrefs(text: str, base_offset: int) -> str:
        local = [
            (s - base_offset, e - base_offset, repl)
            for (s, e), repl in dictref_dict_repl.items()
            if base_offset <= s and e <= base_offset + len(text)
        ]
        return apply_splices(text, local) if local else text

    captures: list[DictCapture] = []
    stack: list[tuple[int, int, str]] = []  # (start, end, attrs_str) of the open <dict>

    for start, end, kind, attrs_str, self_closing in tokens:
        if kind == "dictref":
            continue

        if kind == "dict_open" and self_closing:
            attrs = parse_attrs(attrs_str)
            if "display" in attrs and attrs["display"].strip().lower() != "false":
                raise DictSyntaxError(
                    source_for_warning,
                    f'self-closing <dict .../> with display="{attrs["display"]}" — a self-closing '
                    f'tag has no content to display; omit display= or set display="False"',
                )
            captures.append(DictCapture(
                as_syn_list(attrs.get("syns", "")), attrs.get("entry", ""),
                display=False, self_closing=True, start=start,
            ))
            site_splices.append((start, end, ""))
            continue

        if kind == "dict_open":  # paired open
            if stack:
                raise DictSyntaxError(
                    source_for_warning,
                    f"nested <dict> — one opened at offset {start} before the one opened at offset "
                    f"{stack[-1][0]} was closed (no nesting is supported)",
                )
            stack.append((start, end, attrs_str))
            continue

        # dict_close
        if not stack:
            raise DictSyntaxError(source_for_warning, "</dict> with no matching open <dict>")
        o_start, o_end, o_attrs_str = stack.pop()
        attrs = parse_attrs(o_attrs_str)
        display = attrs.get("display", "").strip().lower() != "false"
        raw_inner = resolve_dictrefs(body[o_end:start], o_end)
        captures.append(DictCapture(
            as_syn_list(attrs.get("syns", "")), raw_inner,
            display=display, self_closing=False, start=o_start,
        ))
        if display:
            site_splices.append((o_start, o_end, ""))
            site_splices.append((start, end, ""))
        else:
            site_splices.append((o_start, end, ""))

    if stack:
        o_start, _, _ = stack[0]
        raise DictSyntaxError(source_for_warning, f"<dict> opened at offset {o_start} was never closed")

    return apply_splices(body, site_splices), captures


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # 1. display=True (default) — content stays on site, becomes the entry
    body1 = (
        '**सूतः** – [(राजानं मृगं चावलोक्य)].{: .action} आयुष्मन् ।\n'
        '<dict syns="ज्या, अधिज्य, कार्मुक">\n'
        'text here\n'
        '<div class="gloss" data-type="notes">अधिज्यकार्मुके = ...</div>\n'
        '</dict>\n'
        'trailing text'
    )
    site1, caps1 = extract_dict_and_ref_tags(body1, "test1")
    assert "<dict" not in site1 and "</dict>" not in site1
    assert "text here" in site1  # content stayed
    assert len(caps1) == 1 and caps1[0].display is True
    assert caps1[0].syns == ["ज्या", "अधिज्य", "कार्मुक"]
    assert "text here" in caps1[0].raw_content

    # 2. display=False — content stripped from site, still captured
    body2 = 'before <dict syns="दा" display="False">त्वयि ददच्चक्षुः</dict> after'
    site2, caps2 = extract_dict_and_ref_tags(body2, "test2")
    assert site2 == "before  after", repr(site2)
    assert caps2[0].display is False and caps2[0].raw_content == "त्वयि ददच्चक्षुः"

    # 3. self-closing shortcut
    body3 = '<dict syns="सुभग" entry="सुभगसलिलावगाह, श्रवणसुभग"/>\nnext'
    site3, caps3 = extract_dict_and_ref_tags(body3, "test3")
    assert site3 == "\nnext", repr(site3)
    assert caps3[0].self_closing and caps3[0].raw_content == "सुभगसलिलावगाह, श्रवणसुभग"

    # 4. dictref inside a <dict> block gets resolved to a link; site loses it entirely
    body4 = '<dict syns="x">a <dictref text="इयेष" ref="iyeSha"/> b</dict>'
    site4, caps4 = extract_dict_and_ref_tags(body4, "test4")
    assert "dictref" not in site4 and "a  b" in site4
    assert caps4[0].raw_content == 'a <a href="bword://iyeSha">इयेष</a> b'

    # 5. bare dictref (shloka format path) via resolve_dictrefs_in_text
    shloka_text = 'मृगानुसारिणं...॥२॥ <dictref text="इयेष (link)" ref="iyeSha"/>'
    site5, dict5 = resolve_dictrefs_in_text(shloka_text, "test5")
    assert "dictref" not in site5
    assert '<a href="bword://iyeSha">इयेष (link)</a>' in dict5

    # 6. nesting is a hard build-breaking error
    body6 = '<dict syns="a"><dict syns="b">inner</dict></dict>'
    try:
        extract_dict_and_ref_tags(body6, "test6.md")
        raise AssertionError("expected DictSyntaxError for nested <dict>")
    except DictSyntaxError as e:
        assert "test6.md" in str(e) and "nested" in str(e)

    # 7. unclosed <dict>, mismatched close, bad dictref, bad self-closing display= — all fatal
    for body, needle in [
        ('<dict syns="a">never closed', "never closed"),
        ('stray </dict>', "no matching open"),
        ('<dictref text="x"/>', "text= or ref="),
        ('<dict syns="a" entry="b" display="True"/>', "self-closing"),
    ]:
        try:
            extract_dict_and_ref_tags(body, "test7.md")
            raise AssertionError(f"expected DictSyntaxError for: {body!r}")
        except DictSyntaxError as e:
            assert needle in str(e), str(e)

    print("\nAll self-tests passed (including error-path checks).")
