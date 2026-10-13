"""Minimal, safe Markdown-to-HTML for report summaries.

Every piece of text is HTML-escaped first; only a small set of tags is ever
produced (headings, paragraphs, lists, tables, code, bold/italic, horizontal
rules and http(s) links with ``rel="noopener noreferrer"``). No raw HTML is
passed through, so report content (which can include untrusted job titles)
cannot inject markup or scripts.
"""

from __future__ import annotations

import re

from markupsafe import Markup, escape

MAX_CHARS = 500_000

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_UL = re.compile(r"^[-*+]\s+(.*)$")
_OL = re.compile(r"^\d{1,4}[.)]\s+(.*)$")
_HR = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")
# Applied to already-escaped text; the URL class excludes quotes, angle brackets and '*'.
_LINK = re.compile(r"\[([^\]\n]{1,300})\]\((https?://[^\s()<>\"'*]{1,2000})\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*(?=\S)([^*]+?)(?<=\S)\*(?![*\w])")


def _inline(text: str) -> str:
    """Escape ``text`` and apply inline formatting. Returns HTML."""
    parts = re.split(r"(`[^`\n]+`)", text)
    out = []
    for part in parts:
        if len(part) >= 2 and part.startswith("`") and part.endswith("`"):
            out.append(f"<code>{escape(part[1:-1])}</code>")
            continue
        html = str(escape(part))
        html = _BOLD.sub(r"<strong>\1</strong>", html)
        html = _ITALIC.sub(r"<em>\1</em>", html)
        html = _LINK.sub(r'<a href="\2" rel="noopener noreferrer" target="_blank">\1</a>', html)
        out.append(html)
    return "".join(out)


def _cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


def _table(lines: list[str]) -> str:
    rows = [ln for ln in lines]
    header: list[str] | None = None
    if len(rows) >= 2 and _TABLE_SEP.match(rows[1].strip()):
        header = _cells(rows[0])
        rows = rows[2:]
    html = ['<div class="table-wrap"><table class="md-table">']
    if header is not None:
        html.append("<thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in header) + "</tr></thead>")
    html.append("<tbody>")
    for r in rows:
        if _TABLE_SEP.match(r.strip()):
            continue
        html.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in _cells(r)) + "</tr>")
    html.append("</tbody></table></div>")
    return "".join(html)


def render_markdown(text: str | None) -> Markup:
    """Render a small, safe subset of Markdown."""
    source = (text or "")[:MAX_CHARS]
    out: list[str] = []
    paragraph: list[str] = []
    items: list[str] = []
    list_tag: str | None = None
    table: list[str] = []
    code: list[str] | None = None

    def flush() -> None:
        nonlocal list_tag
        if paragraph:
            out.append("<p>" + "<br>".join(_inline(p) for p in paragraph) + "</p>")
            paragraph.clear()
        if items and list_tag:
            out.append(f"<{list_tag}>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + f"</{list_tag}>")
            items.clear()
        list_tag = None
        if table:
            out.append(_table(table))
            table.clear()

    for raw in source.splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            if code is None:
                flush()
                code = []
            else:
                out.append("<pre><code>" + str(escape("\n".join(code))) + "</code></pre>")
                code = None
            continue
        if code is not None:
            code.append(raw)
            continue
        if not stripped:
            flush()
            continue
        if stripped.startswith("|"):
            if not table:
                flush()
            table.append(stripped)
            continue
        if table:
            flush()
        if m := _HEADING.match(stripped):
            flush()
            level = min(len(m.group(1)) + 1, 6)  # page <h1> is the page title
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            continue
        if _HR.match(stripped):
            flush()
            out.append("<hr>")
            continue
        for pattern, tag in ((_UL, "ul"), (_OL, "ol")):
            if m := pattern.match(stripped):
                if list_tag != tag:
                    flush()
                    list_tag = tag
                items.append(m.group(1))
                break
        else:
            if stripped.startswith(">"):
                flush()
                out.append(f"<blockquote>{_inline(stripped.lstrip('> '))}</blockquote>")
                continue
            if items:
                flush()
            paragraph.append(stripped)
    if code is not None:
        out.append("<pre><code>" + str(escape("\n".join(code))) + "</code></pre>")
    flush()
    return Markup("\n".join(out))
