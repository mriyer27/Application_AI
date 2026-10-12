"""Form model and in-page helpers (Playwright sync API).

``inspect_form`` runs one script in the page that walks the application form's
controls and returns plain descriptors; Python turns them into ``FormField``
objects with stable keys. Fill / read-back helpers operate on those fields.

Heuristics (documented limitations):
* the "application form" is the ``<form>`` with the most controls, or the whole
  document if there is no ``<form>``; forms inside iframes are not inspected;
* labels come from ``aria-labelledby``, ``<label for>``, a wrapping ``<label>``,
  ``aria-label``, nearby question text, ``placeholder``, ``title``, then ``name``;
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

    def describe(self) -> dict[str, Any]:
        """Metadata persisted with the prepared form (no locators, no values)."""
        return {
            "key": self.key,
            "type": self.type,
            "required": self.required,
            "options": [o.label for o in self.options],
            "accept": self.accept,
            "visible": self.visible,
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

_SNAPSHOT_JS = "(markers) => {" + _COMMON_JS + r"""
  const body = document.body;
  const text = body ? (body.innerText || '') : '';
  const iframes = Array.from(document.querySelectorAll('iframe')).map(f => f.getAttribute('src') || '').filter(Boolean);
  const scripts = Array.from(document.querySelectorAll('script[src]')).map(s => s.getAttribute('src') || '');
  const found = markers.filter(sel => { try { return !!document.querySelector(sel); } catch (e) { return false; } });
  const pw = Array.from(document.querySelectorAll('input[type="password"]')).filter(vis).length;
  const controls = document.querySelectorAll('input:not([type="hidden"]), select, textarea').length;
  const chrome = 'header, nav, footer, [role="banner"], [role="navigation"], [role="contentinfo"]';
  const mainEl = Array.from(document.querySelectorAll('main, [role="main"]')).find(vis);
  let mainText = mainEl ? (mainEl.innerText || '') : text;
  if (!mainEl) {
    for (const el of document.querySelectorAll(chrome)) {
      const t = (el.innerText || '').trim();
      if (t && !el.querySelector('form, input, select, textarea')) mainText = mainText.replace(t, ' ');
    }
  }
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

_INSPECT_JS = "() => {" + _COMMON_JS + r"""
  const byIds = (ids) => clean((ids || '').split(/\s+/).filter(Boolean)
      .map(id => textOf(document.getElementById(id))).join(' '));
  const labelFor = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${q(el.id)}"]`);
      if (l && textOf(l)) return textOf(l);
    }
    const wrap = el.closest('label');
    if (wrap && textOf(wrap)) return textOf(wrap);
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
      let last = '';
      for (const c of node.children) {
        if (c === child || c.contains(child)) break;
        if (c.querySelector('input, select, textarea') || c.matches('input, select, textarea, script, style')) continue;
        const t = textOf(c);
        if (t) last = t;
      }
      if (last) return last;
      if (node.tagName === 'FORM' || node.tagName === 'BODY') break;
    }
    return '';
  };
  const groupQuestion = (el) => {
    const fs = el.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg && textOf(lg)) return textOf(lg); }
    const rg = el.closest('[role="radiogroup"], [role="group"]');
    if (rg) {
      const t = clean(rg.getAttribute('aria-label')) || byIds(rg.getAttribute('aria-labelledby'));
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
      const optLabel = labelFor(el) || clean(el.getAttribute('aria-label')) || el.value || '';
      const shared = name && root.querySelectorAll(`input[type="${type}"][name="${q(name)}"]`).length > 1;
      if (type === 'radio' || shared) {
        const gkey = type + '|' + name;
        let g = groups.get(gkey);
        if (!g) {
          const question = groupQuestion(el);
          g = {index, tag, type: type === 'radio' ? 'radio' : 'checkbox_group', name, id: null,
               label: question || name, required: false, options: [], is_file: false,
               locator: `input[type="${type}"][name="${q(name)}"]`, placeholder: null, accept: null,
               visible: false};
          groups.set(gkey, g);
          out.push(g);
        }
        g.required = g.required || isRequired(el, g.label);
        g.visible = g.visible || visible;
        g.options.push({label: optLabel, value: el.value, locator: selectorOf(el)});
        return;
      }
      const label = labelFor(el) || clean(el.getAttribute('aria-label')) || byIds(el.getAttribute('aria-labelledby'))
          || nearbyText(el, 3) || clean(el.getAttribute('title')) || name;
      if (!visible && !isRequired(el, label)) return;
      out.push({index, tag, type: 'checkbox', name, id: el.id || null, label, required: isRequired(el, label),
                options: [], is_file: false, locator: selectorOf(el), placeholder: null, accept: null, visible});
      return;
    }
    const visible = vis(el);
    const label = byIds(el.getAttribute('aria-labelledby')) || labelFor(el) || clean(el.getAttribute('aria-label'))
        || nearbyText(el, 3) || clean(el.getAttribute('placeholder')) || clean(el.getAttribute('title')) || name;
    if (!visible && type !== 'file' && !isRequired(el, label)) return;
    let options = [];
    if (tag === 'select') {
      options = Array.from(el.options).filter(o => !o.disabled && o.value !== '')
          .map(o => ({label: clean(o.textContent), value: o.value, locator: null}));
    }
    out.push({index, tag, type, name, id: el.id || null, label, required: isRequired(el, label), options,
              is_file: type === 'file', locator: selectorOf(el), placeholder: el.getAttribute('placeholder'),
              accept: el.getAttribute('accept'), visible: visible || type === 'file'});
  });
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

