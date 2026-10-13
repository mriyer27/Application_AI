"""Form model and in-page helpers (Playwright sync API).

``inspect_form`` runs one script in the page that walks the application form's
controls and returns plain descriptors; Python turns them into ``FormField``
objects with stable keys. Fill / read-back helpers operate on those fields.

Heuristics (documented limitations):
* the "application form" is the ``<form>`` with the most controls, or the whole
  document if there is no ``<form>``; forms inside iframes are not inspected;
* labels come from ``aria-labelledby``, ``<label for>``, a wrapping ``<label>``,
  ``aria-label``, nearby question text, ``placeholder``, ``title``, then ``name``;
* help text (``FormField.help_text``) comes from ``aria-describedby`` /
  ``aria-details`` / ``aria-description`` of the control and of its fieldset /
  radiogroup; from ``.help`` / ``.hint`` / ``.description`` / ``small`` ...
  elements in the same question container or right next to it (also when they
  are hidden, e.g. tooltips); and from every other *visible* text in the
  question container (the largest ancestor whose controls all belong to the
  field) and in up to a few text-only elements right before / after it. Label,
  legend and option texts, buttons, required / optional markers and character
  counters are left out; the scan stops at another control, another field's
  label, a heading, a fieldset or a button, so the next question is not read as
  this one's help text. Text in an adjacent *heading* is a section title and is
  not read;
* page snapshots contain only rendered, visible text (see ``_VISIBLE_TEXT_JS``);
* invisible controls are skipped (honeypots), except file inputs and styled
  radios/checkboxes whose label is visible; invisible *required* controls are
  reported with ``visible=False`` so they are routed to you;
* password fields are never inspected or filled;
* the submit control must be labelled exactly "Submit application", "Submit",
  "Apply" or "Send application" (case-insensitive); a page whose only button
  is e.g. "Continue" / "Next" / "Review" is a multi-step wizard this tool does
  not drive, so no control is returned and the submission goes to you.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from job_agent.browser.detection import MARKER_SELECTORS, PageSnapshot
from job_agent.browser.errors import FillError

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page

TEXT_TYPES = frozenset({"text", "email", "tel", "url", "number", "date", "month", "week", "time",
                        "datetime-local", "textarea"})
CHOICE_TYPES = frozenset({"select", "select-multiple", "radio", "checkbox", "checkbox_group"})
TRUTHY = frozenset({"yes", "true", "checked", "check", "1", "on", "y", "agree", "i agree"})
FALSY = frozenset({"no", "false", "unchecked", "uncheck", "0", "off", "n", ""})


@dataclass(frozen=True)
class FieldOption:
    label: str
    value: str
    locator: str | None = None  # radios / checkbox-group members only


@dataclass(frozen=True)
class FormField:
    key: str
    label: str
    type: str
    required: bool = False
    options: tuple[FieldOption, ...] = ()
    is_file: bool = False
    locator: str = ""
    name: str | None = None
    element_id: str | None = None
    placeholder: str | None = None
    accept: str | None = None
    visible: bool = True
    #: Help / hint / description text attached to the control or its question (``aria-describedby``,
    #: ``.help`` / ``.hint`` / ``.description`` / ``small`` next to it). Legal wording here makes the
    #: field a legal declaration (``field_mapping.field_is_legal``).
    help_text: str | None = None

    def describe(self) -> dict[str, Any]:
        """Metadata persisted with the prepared form (no locators, no values)."""
        return {
            "key": self.key,
            "label": self.label,
            "placeholder": self.placeholder,
            "type": self.type,
            "required": self.required,
            "options": [o.label for o in self.options],
            "accept": self.accept,
            "visible": self.visible,
            "help_text": self.help_text,
        }


@dataclass(frozen=True)
class SubmitControl:
    locator: str
    text: str
    form_action: str | None = None
    form_method: str | None = None


def norm(text: str | None) -> str:
    return " ".join((text or "").casefold().split())


def match_option(answer: str, options: tuple[FieldOption, ...] | list[FieldOption]) -> FieldOption | None:
    """Exact (case/whitespace-insensitive) match on an option's label or value; None if absent or ambiguous."""
    target = norm(answer)
    if not target:
        return None
    by_label = [o for o in options if norm(o.label) == target]
    if len(by_label) == 1:
        return by_label[0]
    if len(by_label) > 1:
        return None
    by_value = [o for o in options if norm(o.value) == target]
    return by_value[0] if len(by_value) == 1 else None


# --------------------------------------------------------------------------- #
# In-page scripts
# --------------------------------------------------------------------------- #
_COMMON_JS = r"""
const clean = (t) => (t || '').replace(/\s+/g, ' ').trim();
const vis = (el) => {
  if (!el || !el.isConnected) return false;
  const s = getComputedStyle(el);
  if (s.display === 'none' || s.visibility === 'hidden') return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
};
const textOf = (node) => {
  if (!node) return '';
  const c = node.cloneNode(true);
  c.querySelectorAll('select, option, input, textarea, button, script, style, noscript').forEach(n => n.remove());
  return clean(c.textContent);
};
const q = (s) => String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
const chooseRoot = () => {
  const forms = Array.from(document.querySelectorAll('form')).filter(f => !f.closest('[role="search"]'));
  let best = null, bestCount = 0;
  for (const f of forms) {
    const n = f.querySelectorAll('input:not([type="hidden"]), select, textarea').length;
    if (n > bestCount) { best = f; bestCount = n; }
  }
  return best || document;
};
"""

#: Rendered, *visible* text only. A text node counts when its element is displayed, not
#: ``visibility:hidden``, has an effective (multiplied) opacity of at least 0.1, a font size
#: of at least 6px, a box of real size inside the page (not off-screen, not clipped away by
#: ``overflow`` / ``clip`` / ``clip-path`` of itself or an ancestor), is not covered by an
#: opaque element, and its colour differs from its background (contrast >= 1.25).
_VISIBLE_TEXT_JS = r"""
const VT_SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'SELECT', 'TEXTAREA', 'IFRAME', 'OBJECT',
                         'EMBED', 'HEAD', 'TITLE']);
const _st = new Map();
const st = (el) => { let s = _st.get(el); if (!s) { s = getComputedStyle(el); _st.set(el, s); } return s; };
const up = (n) => n.parentElement || (n.parentNode && n.parentNode.host) || null;
const within = (a, b) => { for (let n = b; n; n = up(n)) if (n === a) return true; return false; };
const DE = document.documentElement, BD = document.body;
const VW = innerWidth, VH = innerHeight;
const rootClips = (axis) => [DE, BD].some(e => e && ['hidden', 'clip'].includes(st(e)[axis]));
const DOC_H = Math.max(DE.scrollHeight, BD ? BD.scrollHeight : 0, VH);
// Viewport coordinates of what can be seen of the page: the viewport width, the document height.
const PAGE = {l: -scrollX, r: VW - scrollX, t: rootClips('overflowY') ? 0 : -scrollY,
              b: rootClips('overflowY') ? VH : DOC_H - scrollY};
const VIEW = {l: 0, r: VW, t: 0, b: VH};
const _op = new Map();
const filterOpacity = (f) => {
  let v = 1; const re = /opacity\(\s*([\d.]+)(%?)\s*\)/g; let m;
  while ((m = re.exec(f || ''))) v *= m[2] ? parseFloat(m[1]) / 100 : parseFloat(m[1]);
  return v;
};
const opacityOf = (el) => {
  if (!el || el.nodeType !== 1) return 1;
  let v = _op.get(el);
  if (v === undefined) {
    const s = st(el), own = parseFloat(s.opacity);
    v = (isNaN(own) ? 1 : own) * filterOpacity(s.filter) * opacityOf(up(el));
    _op.set(el, v);
  }
  return v;
};
const rgba = (c) => {
  const m = /^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:\s*[,/]\s*([\d.]+)(%?))?\s*\)$/.exec(c || '');
  if (!m) return null;
  const a = m[4] === undefined ? 1 : (m[5] ? parseFloat(m[4]) / 100 : parseFloat(m[4]));
  return [+m[1], +m[2], +m[3], a];
};
const lum = (c) => {
  const f = (x) => { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); };
  return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]);
};
const backgroundOf = (el) => {  // composited background colour behind el's text; null = image / unknown
  const layers = [];
  for (let a = el; a; a = up(a)) {
    const s = st(a);
    if (s.backgroundImage && s.backgroundImage !== 'none') return null;
    const c = rgba(s.backgroundColor);
    if (c && c[3] > 0) { layers.push(c); if (c[3] >= 0.99) break; }
  }
  let out = [255, 255, 255];
  for (let i = layers.length - 1; i >= 0; i--) {
    const [r, g, b, a] = layers[i];
    out = [r * a + out[0] * (1 - a), g * a + out[1] * (1 - a), b * a + out[2] * (1 - a)];
  }
  return out;
};
const colorHidden = (el) => {
  const s = st(el);
  const fg = rgba(s.webkitTextFillColor) || rgba(s.color);
  if (!fg) return false;
  if (fg[3] < 0.1) return true;
  if ((s.textShadow && s.textShadow !== 'none') || parseFloat(s.webkitTextStrokeWidth) > 0) return false;
  const bg = backgroundOf(el);
  if (!bg) return false;
  const a = fg[3], eff = [0, 1, 2].map(i => fg[i] * a + bg[i] * (1 - a));
  const l1 = lum(eff), l2 = lum(bg);
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05) < 1.25;
};
const lenOf = (v, ref) => {
  v = String(v || '0').trim();
  return v.endsWith('%') ? parseFloat(v) / 100 * ref : (parseFloat(v) || 0);
};
const clipHides = (el, s) => {
  if ((s.position === 'absolute' || s.position === 'fixed') && s.clip && s.clip !== 'auto') {
    const m = /rect\(([^)]*)\)/.exec(s.clip);
    if (m) {
      const p = m[1].split(/[\s,]+/).filter(Boolean), r = el.getBoundingClientRect();
      if (p.length === 4) {
        const v = (x, d) => x === 'auto' ? d : parseFloat(x);
        if (v(p[2], r.height) - v(p[0], 0) < 4 || v(p[1], r.width) - v(p[3], 0) < 2) return true;
      }
    }
  }
  const cp = s.clipPath;
  if (!cp || cp === 'none') return false;
  const r = el.getBoundingClientRect(), W = r.width, H = r.height;
  let m;
  if ((m = /^inset\(([^)]*)\)/.exec(cp))) {
    const p = m[1].split(/\s+round\s+/)[0].trim().split(/\s+/);
    const t = lenOf(p[0], H), rr = lenOf(p[1] ?? p[0], W);
    const b = lenOf(p[2] ?? p[0], H), l = lenOf(p[3] ?? p[1] ?? p[0], W);
    return H - t - b < 4 || W - l - rr < 2;
  }
  if ((m = /^circle\(\s*([-\d.]+(?:px|%))/.exec(cp))) return lenOf(m[1], Math.hypot(W, H) / Math.SQRT2) < 2;
  if ((m = /^ellipse\(\s*([-\d.]+(?:px|%))\s+([-\d.]+(?:px|%))/.exec(cp))) {
    return lenOf(m[1], W) < 2 || lenOf(m[2], H) < 4;
  }
  if (/^(circle|ellipse)\(/.test(cp)) return false;  // keyword radius (closest-side, ...)
  if ((m = /^polygon\(([^)]*)\)/.exec(cp))) {
    const pts = m[1].replace(/^\s*(nonzero|evenodd)\s*,/, '').split(',').map(pt => pt.trim().split(/\s+/));
    let area = 0;
    for (let i = 0; i < pts.length; i++) {
      const [x1, y1] = pts[i], [x2, y2] = pts[(i + 1) % pts.length];
      area += lenOf(x1, W) * lenOf(y2, H) - lenOf(x2, W) * lenOf(y1, H);
    }
    return Math.abs(area) / 2 < 16;
  }
  return true;  // url(), path(), ...: cannot be measured, assume it may hide the text
};
const _reg = new Map();
// The part of the viewport (viewport coordinates) where el's content can be painted; null = clipped away.
const regionOf = (el) => {
  if (!el || el === DE || el === BD || el.nodeType !== 1) return PAGE;
  if (_reg.has(el)) return _reg.get(el);
  const s = st(el);
  let out;
  if (s.position === 'fixed') out = VIEW;
  else if (s.position === 'absolute') {
    let cb = up(el);
    while (cb && cb !== DE && cb !== BD && st(cb).position === 'static') cb = up(cb);
    out = regionOf(cb);
  } else out = regionOf(up(el));
  if (out && clipHides(el, s)) out = null;
  const paint = /paint|strict|content/.test(s.contain || '');
  if (out && (s.overflowX !== 'visible' || s.overflowY !== 'visible' || paint)) {
    const r = el.getBoundingClientRect();
    const scrolls = (o) => o === 'auto' || o === 'scroll';
    let {l, r: rr, t, b} = out;
    if (s.overflowX !== 'visible' || paint) {
      if (scrolls(s.overflowX) && !paint) l = Math.max(l, r.left - el.scrollLeft);
      else { l = Math.max(l, r.left); rr = Math.min(rr, r.right); }
    }
    if (s.overflowY !== 'visible' || paint) {
      if (scrolls(s.overflowY) && !paint) t = Math.max(t, r.top - el.scrollTop);
      else { t = Math.max(t, r.top); b = Math.min(b, r.bottom); }
    }
    out = (rr - l >= 1 && b - t >= 1) ? {l, r: rr, t, b} : null;
  }
  _reg.set(el, out);
  return out;
};
const opaqueBox = (a) => {
  if (opacityOf(a) < 0.9) return false;
  const s = st(a);
  if (s.visibility !== 'visible') return false;
  if (/^(IMG|VIDEO|CANVAS|IFRAME|OBJECT|EMBED)$/.test(a.tagName) || a.tagName.toLowerCase() === 'svg') return true;
  if (s.backgroundImage && s.backgroundImage !== 'none') return true;
  const c = rgba(s.backgroundColor);
  return !!c && c[3] >= 0.9;
};
const occluded = (el, x, y) => {
  if (st(el).pointerEvents === 'none') return false;  // hit testing would look through it
  let hit = document.elementFromPoint(x, y);
  while (hit && hit.shadowRoot) {
    const inner = hit.shadowRoot.elementFromPoint(x, y);
    if (!inner || inner === hit) break;
    hit = inner;
  }
  if (!hit || within(el, hit) || within(hit, el)) return false;
  for (let a = hit; a && !within(a, el); a = up(a)) if (opaqueBox(a)) return true;
  return false;
};
const textShown = (node) => {
  const el = up(node);
  if (!el || !el.isConnected) return false;
  const s = st(el);
  if (s.visibility !== 'visible' || parseFloat(s.fontSize) < 6 || opacityOf(el) < 0.1) return false;
  if (el.checkVisibility && !el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return false;
  const region = regionOf(el);
  if (!region) return false;
  const range = document.createRange();
  range.selectNodeContents(node);
  let shown = false;
  for (const b of range.getClientRects()) {
    if (b.width < 1 || b.height < 4) continue;
    const l = Math.max(b.left, region.l), r = Math.min(b.right, region.r);
    const t = Math.max(b.top, region.t), bt = Math.min(b.bottom, region.b);
    if (r - l < 1 || bt - t < 4) continue;
    const cx = (l + r) / 2, cy = (t + bt) / 2;
    if (cx >= 0 && cx < VW && cy >= 0 && cy < VH && occluded(el, cx, cy)) continue;
    shown = true;
    break;
  }
  return shown && !colorHidden(el);
};
const _tv = new Map();
const INLINE = /^(inline|contents|ruby)/;
const blockOf = (el) => { let b = el; while (b && b !== DE && INLINE.test(st(b).display)) b = up(b); return b; };
const visibleText = (root, skipSel, skipEl) => {
  const parts = [];
  let last = null, nodes = 0, size = 0;
  const visit = (node) => {
    if (nodes > 60000 || size > 40000) return;
    if (node.nodeType === 3) {
      nodes++;
      const data = node.data;
      if (!data.trim()) { parts.push(' '); return; }
      let ok = _tv.get(node);
      if (ok === undefined) { ok = textShown(node); _tv.set(node, ok); }
      if (!ok) return;
      const blk = blockOf(up(node));
      if (blk !== last) { parts.push('\n'); last = blk; }
      parts.push(data);
      size += data.length;
      return;
    }
    if (node.nodeType === 1) {
      if (VT_SKIP.has(node.tagName)) return;
      if (node.tagName === 'BR') { parts.push('\n'); return; }
      if (skipSel && node.matches(skipSel) && !node.querySelector('form, input, select, textarea')) return;
      if (skipEl && skipEl(node)) return;
      if (st(node).display === 'none') return;
      if (node.shadowRoot) visit(node.shadowRoot);
    } else if (node.nodeType !== 11) return;
    for (let c = node.firstChild; c; c = c.nextSibling) visit(c);
  };
  if (root) visit(root);
  return parts.join('').split('\n').map(l => l.replace(/[ \t\f\v\r ]+/g, ' ').trim()).filter(Boolean).join('\n');
};
"""

_SNAPSHOT_JS = "(markers) => {" + _COMMON_JS + _VISIBLE_TEXT_JS + r"""
  const body = document.body;
  const iframes = Array.from(document.querySelectorAll('iframe')).map(f => f.getAttribute('src') || '').filter(Boolean);
  const scripts = Array.from(document.querySelectorAll('script[src]')).map(s => s.getAttribute('src') || '');
  const found = markers.filter(sel => { try { return !!document.querySelector(sel); } catch (e) { return false; } });
  const pw = Array.from(document.querySelectorAll('input[type="password"]')).filter(vis).length;
  const controls = document.querySelectorAll('input:not([type="hidden"]), select, textarea').length;
  const chrome = 'header, nav, footer, [role="banner"], [role="navigation"], [role="contentinfo"]';
  const text = visibleText(body, null);
  const mainEl = Array.from(document.querySelectorAll('main, [role="main"]')).find(vis);
  const mainText = mainEl ? visibleText(mainEl, null) : visibleText(body, chrome);
  const skip = ['hidden', 'submit', 'button', 'reset', 'image', 'search'];
  const visibleControls = Array.from(document.querySelectorAll('input, select, textarea'))
      .filter(el => !(el.tagName === 'INPUT' && skip.includes((el.getAttribute('type') || 'text').toLowerCase())))
      .filter(el => !el.closest('[role="search"]') && !el.closest(chrome) && vis(el))
      .map(el => el.getAttribute('name') || el.id || '').filter(Boolean);
  return {url: location.href, title: document.title || '', text: text.slice(0, 20000),
          iframe_srcs: iframes.slice(0, 50), script_srcs: scripts.slice(0, 300), markers: found,
          visible_password_fields: pw, form_control_count: controls, main_text: mainText.slice(0, 20000),
          visible_controls: Array.from(new Set(visibleControls)).slice(0, 300)};
}"""

_INSPECT_JS = "() => {" + _COMMON_JS + _VISIBLE_TEXT_JS + r"""
  const idEls = (ids) => (ids || '').split(/\s+/).filter(Boolean).map(id => document.getElementById(id))
      .filter(Boolean);
  const byIds = (ids) => clean(idEls(ids).map(textOf).join(' '));
  // Elements a field's question / option labels are read from, recorded while the label is read (SRC).
  // They are never another field's help text, and help text never extends past another field's label.
  let SRC = null;
  const note = (el) => { if (SRC && el) SRC.push(el); };
  const labelledBy = (ids) => {
    const els = idEls(ids);
    const t = clean(els.map(textOf).join(' '));
    if (t) els.forEach(note);
    return t;
  };
  const labelFor = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${q(el.id)}"]`);
      if (l && textOf(l)) { note(l); return textOf(l); }
    }
    const wrap = el.closest('label');
    if (wrap && textOf(wrap)) { note(wrap); return textOf(wrap); }
    return '';
  };
  const labelVisible = (el) => {
    if (el.id) { const l = document.querySelector(`label[for="${q(el.id)}"]`); if (vis(l)) return true; }
    return vis(el.closest('label'));
  };
  // Text of the nearest preceding control-free element in an ancestor (question text).
  const nearbyText = (el, maxDepth) => {
    let child = el, node = el.parentElement;
    for (let d = 0; node && d < maxDepth; d++, child = node, node = node.parentElement) {
      let last = '', lastEl = null;
      for (const c of node.children) {
        if (c === child || c.contains(child)) break;
        if (c.querySelector('input, select, textarea') || c.matches('input, select, textarea, script, style')) continue;
        const t = textOf(c);
        if (t) { last = t; lastEl = c; }
      }
      if (last) { note(lastEl); return last; }
      if (node.tagName === 'FORM' || node.tagName === 'BODY') break;
    }
    return '';
  };
  const groupQuestion = (el) => {
    const fs = el.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg && textOf(lg)) { note(lg); return textOf(lg); } }
    const rg = el.closest('[role="radiogroup"], [role="group"]');
    if (rg) {
      const t = clean(rg.getAttribute('aria-label')) || labelledBy(rg.getAttribute('aria-labelledby'));
      if (t) return t;
    }
    const wrap = el.closest('label') || el;
    return nearbyText(wrap, 6);
  };
  const xpathOf = (el) => {
    const parts = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      let i = 1;
      for (let s = n.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === n.tagName) i++;
      parts.unshift(`${n.tagName.toLowerCase()}[${i}]`);
    }
    return '/' + parts.join('/');
  };
  const selectorOf = (el) => {
    const tag = el.tagName.toLowerCase();
    if (el.id && document.querySelectorAll(`[id="${q(el.id)}"]`).length === 1) return `[id="${q(el.id)}"]`;
    const name = el.getAttribute('name');
    if (name) {
      const s = `${tag}[name="${q(name)}"]`;
      if (document.querySelectorAll(s).length === 1) return s;
    }
    return 'xpath=' + xpathOf(el);
  };
  const isRequired = (el, label) => el.required || el.getAttribute('aria-required') === 'true'
      || /(\*|✱|\(required\))\s*$/i.test(label || '');
  const root = chooseRoot();
  const out = [];
  const groups = new Map();
  const members = new Map();  // output entry -> its controls
  const srcOf = new Map();  // output entry -> elements its question / option labels come from
  const skippedSrc = [];  // label elements of controls that are not reported (invisible, optional)
  const els = Array.from(root.querySelectorAll('input, select, textarea'));
  els.forEach((el, index) => {
    const tag = el.tagName.toLowerCase();
    let type = tag === 'input' ? (el.getAttribute('type') || 'text').toLowerCase() : tag;
    if (tag === 'select' && el.multiple) type = 'select-multiple';
    if (['hidden', 'submit', 'button', 'reset', 'image', 'password', 'search', 'range', 'color'].includes(type)) return;
    if (el.disabled || el.closest('[role="search"]')) return;
    const name = el.getAttribute('name') || '';
    if (type === 'radio' || type === 'checkbox') {
      const visible = vis(el) || labelVisible(el);
      const shared = name && root.querySelectorAll(`input[type="${type}"][name="${q(name)}"]`).length > 1;
      if (type === 'radio' || shared) {
        const gkey = type + '|' + name;
        let g = groups.get(gkey);
        if (!g) {
          SRC = [];
          const question = groupQuestion(el);
          g = {index, tag, type: type === 'radio' ? 'radio' : 'checkbox_group', name, id: null,
               label: question || name, required: false, options: [], is_file: false,
               locator: `input[type="${type}"][name="${q(name)}"]`, placeholder: null, accept: null,
               visible: false};
          groups.set(gkey, g);
          out.push(g);
          members.set(g, []);
          srcOf.set(g, SRC);
        }
        SRC = srcOf.get(g);
        const optLabel = labelFor(el) || clean(el.getAttribute('aria-label')) || el.value || '';
        SRC = null;
        members.get(g).push(el);
        g.required = g.required || isRequired(el, g.label);
        g.visible = g.visible || visible;
        g.options.push({label: optLabel, value: el.value, locator: selectorOf(el)});
        return;
      }
      SRC = [];
      const label = labelFor(el) || clean(el.getAttribute('aria-label'))
          || labelledBy(el.getAttribute('aria-labelledby')) || nearbyText(el, 3) || clean(el.getAttribute('title'))
          || name;
      const src = SRC;
      SRC = null;
      if (!visible && !isRequired(el, label)) { skippedSrc.push(...src); return; }
      const entry = {index, tag, type: 'checkbox', name, id: el.id || null, label, required: isRequired(el, label),
                     options: [], is_file: false, locator: selectorOf(el), placeholder: null, accept: null, visible};
      out.push(entry);
      members.set(entry, [el]);
      srcOf.set(entry, src);
      return;
    }
    const visible = vis(el);
    SRC = [];
    const label = labelledBy(el.getAttribute('aria-labelledby')) || labelFor(el) || clean(el.getAttribute('aria-label'))
        || nearbyText(el, 3) || clean(el.getAttribute('placeholder')) || clean(el.getAttribute('title')) || name;
    const src = SRC;
    SRC = null;
    if (!visible && type !== 'file' && !isRequired(el, label)) { skippedSrc.push(...src); return; }
    let options = [];
    if (tag === 'select') {
      options = Array.from(el.options).filter(o => !o.disabled && o.value !== '')
          .map(o => ({label: clean(o.textContent), value: o.value, locator: null}));
    }
    const entry = {index, tag, type, name, id: el.id || null, label, required: isRequired(el, label), options,
                   is_file: type === 'file', locator: selectorOf(el), placeholder: el.getAttribute('placeholder'),
                   accept: el.getAttribute('accept'), visible: visible || type === 'file'};
    out.push(entry);
    members.set(entry, [el]);
    srcOf.set(entry, src);
  });
  // ---- help text ----
  // (1) aria-describedby / aria-details / aria-description of the control and of its fieldset / radiogroup;
  // (2) hint / help / description elements in the question box or next to it (also when hidden: tooltips);
  // (3) every other VISIBLE text in the question box: the largest ancestor whose controls are all this field's
  //     (without labels / legends / option labels, buttons and other fields' labels);
  // (4) visible text of up to a few text-only elements right before / after the box, stopping at another
  //     control, another field's label, a heading, a fieldset or a button.
  const CONTROLS = 'input:not([type="hidden"]), select, textarea';
  const WIDGET = '[role="combobox"], [role="listbox"], [role="radiogroup"], [role="group"], [role="textbox"], '
      + '[role="searchbox"], [role="spinbutton"], [role="slider"], [contenteditable="true"], [contenteditable=""]';
  const ANY_CONTROL = CONTROLS + ', ' + WIDGET + ', [role="checkbox"], [role="radio"], [role="switch"]';
  const BOUNDARY = 'fieldset, legend, button, [role="button"], h1, h2, h3, h4, h5, h6, [role="heading"], form, hr';
  const NOT_HELP = 'button, [role="button"]';
  //: Required / optional markers and character counters are not help text.
  const NOISE = /^(?:[*✱]+|\(?\s*(?:required|optional)\s*\)?|\d+\s*\/\s*\d+(?:\s*char\w*)?)$/i;
  const HELP_SEL = ['.help', '.hint', '.description', '.help-text', '.helper-text', '.help-block', '.form-text',
      '.instructions', '[class*="help"]', '[class*="hint"]', '[class*="description"]', '[class*="instruction"]',
      '[id$="-help"]', '[id$="_help"]', '[id$="-hint"]', '[id$="_hint"]', '[id$="-description"]',
      '[id$="_description"]', '[id$="-desc"]', 'small', '[role="note"]'].join(', ');
  const described = (el) => [byIds(el.getAttribute('aria-describedby')), byIds(el.getAttribute('aria-details')),
                             clean(el.getAttribute('aria-description'))];
  const describedEls = (el) => [...idEls(el.getAttribute('aria-describedby')),
                                ...idEls(el.getAttribute('aria-details'))];
  const ownLabels = (el) => {
    const outL = [];
    if (el.id) document.querySelectorAll(`label[for="${q(el.id)}"]`).forEach(l => outL.push(l));
    const w = el.closest('label');
    if (w) outL.push(w);
    return outL;
  };
  const FIELD_EL = new Set();
  const ALL_SRC = new Set(skippedSrc);
  for (const [entry, ms] of members) {
    ms.forEach(m => { FIELD_EL.add(m); ownLabels(m).forEach(l => ALL_SRC.add(l)); });
    (srcOf.get(entry) || []).forEach(e => ALL_SRC.add(e));
  }
  root.querySelectorAll('label').forEach(l => { if (l.control) ALL_SRC.add(l); });
  // The largest ancestor below the form whose controls all belong to this field.
  const questionBox = (ms) => {
    const set = new Set(ms);
    let node = ms[0];
    for (let d = 0; d < 6; d++) {
      const p = node.parentElement;
      if (!p || p === root || p === document.body || p === document.documentElement || p.tagName === 'FORM') break;
      if (!Array.from(p.querySelectorAll(CONTROLS)).every(c => set.has(c))) break;
      if (Array.from(p.querySelectorAll(WIDGET)).some(w => !ms.some(m => w.contains(m)))) break;  // custom widget
      if ((p.textContent || '').length > 3000) break;
      node = p;
    }
    return node;
  };
  const hasControl = (x) => x.matches(ANY_CONTROL) || !!x.querySelector(ANY_CONTROL);
  // A control / custom widget that is not one of the reported fields (its label is not recorded).
  const foreignControl = (x) => hasControl(x)
      && !(x.matches(CONTROLS) ? [x] : Array.from(x.querySelectorAll(CONTROLS))).some(c => FIELD_EL.has(c));
  const helpFor = (entry) => {
    const ms = members.get(entry);
    const texts = [];
    const own = new Set(srcOf.get(entry) || []);
    const skipEls = new Set();
    for (const m of ms) {
      ownLabels(m).forEach(l => own.add(l));
      texts.push(...described(m));
      describedEls(m).forEach(e => skipEls.add(e));
    }
    const grp = ms[0].closest('fieldset, [role="radiogroup"], [role="group"]');
    if (grp && Array.from(grp.querySelectorAll(CONTROLS)).every(c => ms.includes(c))) {
      texts.push(...described(grp));
      describedEls(grp).forEach(e => skipEls.add(e));
      const lg = grp.tagName === 'FIELDSET' ? grp.querySelector('legend') : null;
      if (lg) own.add(lg);
    }
    const added = [];
    const take = (h) => {
      if (h.querySelector(CONTROLS) || h.closest('label') || added.some(a => a.contains(h))) return;
      for (const o of own) if (o === h || o.contains(h)) return;
      added.push(h);
      texts.push(textOf(h));
    };
    const skip = (n) => own.has(n) || ALL_SRC.has(n) || skipEls.has(n) || added.includes(n) || n.matches(NOT_HELP);
    const visibleLines = (x) => visibleText(x, null, skip).split('\n');
    const otherLabel = (x) => {
      for (const s of ALL_SRC) if (!own.has(s) && (s === x || x.contains(s))) return true;
      return false;
    };
    const box = questionBox(ms);
    if (!ms.includes(box)) {
      box.querySelectorAll(HELP_SEL).forEach(take);
      texts.push(...visibleLines(box));
    }
    const sib = (n, dir) => {
      for (let x = n[dir], i = 0; x && i < 4; x = x[dir], i++) {
        if (x.matches(BOUNDARY) || hasControl(x) || x.querySelector('fieldset, legend, form, h1, h2, h3, h4, h5, h6, '
                                                                    + '[role="heading"]')) break;
        if (own.has(x)) continue;
        if (otherLabel(x) || (x.tagName === 'LABEL' && x.control)) break;
        // text right before a custom widget / unreported control is that control's question
        if (dir === 'nextElementSibling' && x.nextElementSibling && foreignControl(x.nextElementSibling)) break;
        if (x.matches(HELP_SEL)) { take(x); continue; }
        const lines = visibleLines(x);
        if (lines.some(Boolean)) { added.push(x); texts.push(...lines); }
      }
    };
    sib(box, 'nextElementSibling');
    sib(box, 'previousElementSibling');
    const seen = new Set([clean(entry.label).toLowerCase()]);
    for (const o of entry.options || []) seen.add(clean(o.label).toLowerCase());
    const uniq = [];
    for (const t of texts.map(clean)) {
      if (t && !NOISE.test(t) && !seen.has(t.toLowerCase())) { seen.add(t.toLowerCase()); uniq.push(t); }
    }
    return uniq.join(' ').slice(0, 1000);
  };
  for (const entry of out) {
    try { entry.help_text = helpFor(entry) || null; } catch (e) { entry.help_text = 'help text could not be read'; }
  }
  return out;
}"""

_VALIDATION_JS = "(includeConstraints) => {" + _COMMON_JS + r"""
  const sels = ['[role="alert"]', '.error', '.errors', '.error-message', '.field-error', '.invalid-feedback',
                '.form-error', '.help-block.error', '[class*="error-text"]', '[class*="errorMessage"]',
                '[data-error]', '.validation-error', '.parsley-errors-list li'];
  const seen = new Set();
  const out = [];
  for (const sel of sels) {
    for (const el of document.querySelectorAll(sel)) {
      if (!vis(el)) continue;
      const t = clean(el.innerText || el.textContent);
      if (!t || t.length > 300 || seen.has(t)) continue;
      seen.add(t); out.push(t);
    }
  }
  for (const el of document.querySelectorAll('[aria-invalid="true"]')) {
    if (!vis(el)) continue;
    const name = clean(el.getAttribute('aria-label')) || el.getAttribute('name') || el.id || 'field';
    const t = `${name}: marked invalid`;
    if (!seen.has(t)) { seen.add(t); out.push(t); }
  }
  if (includeConstraints) {
    const root = chooseRoot();
    for (const el of root.querySelectorAll('input:not([type="hidden"]), select, textarea')) {
      if (el.disabled || !el.willValidate || el.checkValidity()) continue;
      const name = el.getAttribute('name') || el.id || 'field';
      const t = `${name}: ${el.validationMessage || 'invalid value'}`;
      if (!seen.has(t)) { seen.add(t); out.push(t); }
    }
  }
  return out.slice(0, 50);
}"""

#: Labels accepted for the final submit control (exact, case-insensitive; trailing punctuation/arrow allowed).
SUBMIT_LABEL_RE = re.compile(r"^\s*(?:submit(?: application)?|apply|send application)\s*[.!>\u203a\u00bb\u2192]*\s*$",
                             re.I)

_SUBMIT_JS = "() => {" + _COMMON_JS + r"""
  const SUBMIT_LABEL = /^\s*(?:submit(?: application)?|apply|send application)\s*[.!>\u203a\u00bb\u2192]*\s*$/i;
  const root = chooseRoot();
  const scope = root === document ? document : root;
  const cands = Array.from(scope.querySelectorAll('button, input[type="submit"], [role="button"]'))
      .filter(el => vis(el) && !el.disabled);
  const label = (el) => clean(el.innerText || el.value || el.getAttribute('aria-label') || '');
  const isSubmit = (el) => (el.tagName === 'INPUT' && el.type === 'submit')
      || (el.tagName === 'BUTTON' && (el.getAttribute('type') || 'submit').toLowerCase() === 'submit' && root !== document);
  // Only an explicitly labelled final-submit control; never "any submit button" (wizards: Continue / Next).
  const finalLabel = (el) => SUBMIT_LABEL.test(label(el));
  const pick = cands.find(el => isSubmit(el) && finalLabel(el)) || cands.find(finalLabel);
  if (!pick) return null;
  const form = pick.form || (root !== document ? root : null);
  const id = pick.id && document.querySelectorAll(`[id="${q(pick.id)}"]`).length === 1 ? pick.id : null;
  let locator;
  if (id) locator = `[id="${q(id)}"]`;
  else {
    const all = Array.from(document.querySelectorAll(pick.tagName.toLowerCase()));
    locator = `${pick.tagName.toLowerCase()} >> nth=${all.indexOf(pick)}`;
  }
  return {locator, text: label(pick), form_action: form ? (form.action || null) : null,
          form_method: form ? (form.getAttribute('method') || 'get').toLowerCase() : null};
}"""


# --------------------------------------------------------------------------- #
# Playwright helpers
# --------------------------------------------------------------------------- #
def take_snapshot(page: "Page", status: int | None = None) -> PageSnapshot:
    raw = page.evaluate(_SNAPSHOT_JS, list(MARKER_SELECTORS))
    return PageSnapshot(
        url=str(raw.get("url") or page.url),
        title=str(raw.get("title") or ""),
        text=str(raw.get("text") or ""),
        iframe_srcs=tuple(raw.get("iframe_srcs") or ()),
        script_srcs=tuple(raw.get("script_srcs") or ()),
        markers=tuple(raw.get("markers") or ()),
        visible_password_fields=int(raw.get("visible_password_fields") or 0),
        form_control_count=int(raw.get("form_control_count") or 0),
        status=status,
        main_text=str(raw.get("main_text") or ""),
        main_measured=True,
        visible_controls=tuple(str(x) for x in (raw.get("visible_controls") or ())),
    )


def inspect_form(page: "Page") -> list[FormField]:
    """All fillable controls of the application form, in document order."""
    raw = page.evaluate(_INSPECT_JS)
    return build_fields(raw or [])


_SLUG_RE = re.compile(r"[^a-z0-9]+")
_REQUIRED_MARK = re.compile(r"\s*(\*|✱|\(required\))\s*$", re.I)


def clean_label(label: str) -> str:
    text = " ".join((label or "").split())
    while True:
        stripped = _REQUIRED_MARK.sub("", text)
        if stripped == text:
            break
        text = stripped
    return text[:300]


def build_fields(raw: list[dict[str, Any]]) -> list[FormField]:
    """Turn raw descriptors into ``FormField`` objects with unique, stable keys."""
    fields: list[FormField] = []
    used: set[str] = set()
    for i, r in enumerate(raw):
        name = (r.get("name") or "").strip() or None
        element_id = (r.get("id") or "").strip() or None
        raw_label = clean_label(str(r.get("label") or ""))
        label = raw_label or (name or element_id or f"Field {i + 1}")
        if name:
            base = name
        elif element_id:
            base = element_id
        else:
            slug = _SLUG_RE.sub("-", raw_label.lower()).strip("-")[:48]
            base = f"label-{slug}" if slug else f"field-{i + 1}"
        key, n = base, 2
        while key in used:
            key = f"{base}__{n}"
            n += 1
        used.add(key)
        options = tuple(
            FieldOption(label=clean_label(str(o.get("label") or "")) or str(o.get("value") or ""),
                        value=str(o.get("value") or ""), locator=o.get("locator"))
            for o in (r.get("options") or [])
        )
        ftype = str(r.get("type") or "text")
        fields.append(FormField(
            key=key, label=label, type=ftype, required=bool(r.get("required")), options=options,
            is_file=bool(r.get("is_file")) or ftype == "file", locator=str(r.get("locator") or ""), name=name,
            element_id=element_id, placeholder=r.get("placeholder") or None, accept=r.get("accept") or None,
            visible=bool(r.get("visible", True)),
            help_text=" ".join(str(r.get("help_text") or "").split())[:1000] or None,
        ))
    return fields


def visible_validation_errors(page: "Page", *, include_constraints: bool = False) -> list[str]:
    return [str(x) for x in (page.evaluate(_VALIDATION_JS, include_constraints) or [])]


def is_final_submit_label(text: str | None) -> bool:
    """Is ``text`` one of the accepted final-submit labels (see ``SUBMIT_LABEL_RE``)?"""
    return bool(SUBMIT_LABEL_RE.match(" ".join((text or "").split())))


def find_submit_control(page: "Page") -> SubmitControl | None:
    """The form's explicitly labelled final submit control, or None (wizard / unsupported form)."""
    raw = page.evaluate(_SUBMIT_JS)
    if not raw or not is_final_submit_label(str(raw.get("text") or "")):
        return None
    return SubmitControl(locator=str(raw["locator"]), text=str(raw.get("text") or ""),
                         form_action=raw.get("form_action"), form_method=raw.get("form_method"))


def fill_field(page: "Page", fld: FormField, value: str, *, timeout_ms: float = 10_000) -> None:
    """Enter ``value`` into ``fld``. Raises ``FillError`` when it cannot be set."""
    from playwright.sync_api import Error as PlaywrightError

    try:
        if fld.type in TEXT_TYPES:
            page.locator(fld.locator).first.fill(value, timeout=timeout_ms)
        elif fld.type in {"select", "select-multiple"}:
            option = match_option(value, fld.options)
            if option is None:
                raise FillError(f"{fld.key}: {value!r} is not one of the available options")
            page.locator(fld.locator).first.select_option(value=option.value, timeout=timeout_ms)
        elif fld.type == "radio":
            option = match_option(value, fld.options)
            if option is None or not option.locator:
                raise FillError(f"{fld.key}: {value!r} is not one of the available options")
            _set_checked(page, option.locator, True, timeout_ms)
        elif fld.type == "checkbox":
            wanted = norm(value)
            if wanted not in TRUTHY | FALSY:
                raise FillError(f"{fld.key}: checkbox answer must be yes/no, got {value!r}")
            _set_checked(page, fld.locator, wanted in TRUTHY, timeout_ms)
        elif fld.type == "checkbox_group":
            wanted_opts = []
            for part in [p for p in value.split(";") if p.strip()]:
                option = match_option(part, fld.options)
                if option is None or not option.locator:
                    raise FillError(f"{fld.key}: {part.strip()!r} is not one of the available options")
                wanted_opts.append(option)
            for option in fld.options:
                if option.locator:
                    _set_checked(page, option.locator, option in wanted_opts, timeout_ms)
        else:
            raise FillError(f"{fld.key}: unsupported field type {fld.type!r}")
    except PlaywrightError as exc:
        raise FillError(f"{fld.key}: could not enter value ({first_line(exc)})") from exc


def attach_file(page: "Page", fld: FormField, path: Path, *, timeout_ms: float = 10_000) -> None:
    from playwright.sync_api import Error as PlaywrightError

    if not fld.is_file:
        raise FillError(f"{fld.key} is not a file input")
    try:
        page.locator(fld.locator).first.set_input_files(str(path), timeout=timeout_ms)
    except PlaywrightError as exc:
        raise FillError(f"{fld.key}: could not attach file ({first_line(exc)})") from exc


def read_field_value(page: "Page", fld: FormField) -> str | None:
    """Current value as shown to the employer (option *label* for choices)."""
    from playwright.sync_api import Error as PlaywrightError

    try:
        if fld.is_file:
            return page.locator(fld.locator).first.evaluate("el => el.files && el.files.length ? el.files[0].name : ''")
        if fld.type in TEXT_TYPES:
            return page.locator(fld.locator).first.input_value()
        if fld.type in {"select", "select-multiple"}:
            value = page.locator(fld.locator).first.evaluate(
                "el => Array.from(el.selectedOptions).map(o => o.value).join('\\u0001')")
            chosen = [v for v in value.split("\u0001") if v]
            labels = [next((o.label for o in fld.options if o.value == v), v) for v in chosen]
            return "; ".join(labels)
        if fld.type == "checkbox":
            return "yes" if page.locator(fld.locator).first.is_checked() else "no"
        if fld.type in {"radio", "checkbox_group"}:
            checked = [o.label for o in fld.options if o.locator and page.locator(o.locator).first.is_checked()]
            return "; ".join(checked)
    except PlaywrightError as exc:
        raise FillError(f"{fld.key}: could not read value back ({first_line(exc)})") from exc
    return None


def value_matches(fld: FormField, expected: str, actual: str | None) -> bool:
    if actual is None:
        return False
    if fld.is_file:
        return norm(Path(expected).name) == norm(actual)
    if fld.type == "checkbox":
        return (norm(expected) in TRUTHY) == (norm(actual) in TRUTHY)
    if fld.type in {"checkbox_group", "select-multiple"}:
        return sorted(norm(p) for p in expected.split(";") if p.strip()) == sorted(
            norm(p) for p in actual.split(";") if p.strip())
    if fld.type in {"select", "radio"}:
        option = match_option(expected, fld.options)
        return option is not None and norm(actual) == norm(option.label)
    return actual == expected or norm(actual) == norm(expected)


def _set_checked(page: "Page", locator: str, checked: bool, timeout_ms: float) -> None:
    from playwright.sync_api import Error as PlaywrightError

    loc = page.locator(locator).first
    try:
        loc.set_checked(checked, timeout=min(timeout_ms, 3000))
        return
    except PlaywrightError:
        pass  # styled controls: native input hidden; fall back to its label / a DOM click
    if loc.is_checked() == checked:
        return
    loc.evaluate("el => { const l = (el.id && document.querySelector(`label[for=\"${CSS.escape(el.id)}\"]`)) "
                 "|| el.closest('label'); (l || el).click(); }")
    if loc.is_checked() != checked:
        raise FillError(f"could not {'check' if checked else 'uncheck'} {locator}")


def first_line(exc: BaseException) -> str:
    return str(exc).strip().splitlines()[0][:200] if str(exc).strip() else type(exc).__name__


def snapshot_from_dict(data: dict[str, Any]) -> PageSnapshot:
    """Helper for tests / fakes."""
    return PageSnapshot(**{k: (tuple(v) if isinstance(v, list) else v) for k, v in data.items()})


__all__ = [
    "CHOICE_TYPES", "TEXT_TYPES", "FieldOption", "FormField", "SubmitControl", "attach_file", "build_fields",
    "SUBMIT_LABEL_RE", "clean_label", "fill_field", "find_submit_control", "inspect_form", "is_final_submit_label",
    "match_option", "norm",
    "read_field_value", "snapshot_from_dict", "take_snapshot", "value_matches", "visible_validation_errors",
]

