// cu_bundle.js: injected into every frame (Playwright init script) before page scripts run.
//
// One module does perception AND targeting so record-time validation and replay-time
// resolution can never disagree:
//   * snapshot(): accessibility-style view of the frame (roles, accessible names,
//     proximity labels, values, tables) with per-observation refs
//   * resolve(strategy): the ONLY way a target is found, at record time and at replay
//   * describe(el): fingerprint + candidate strategies, each validated to hit exactly el
//   * capture: human input during a handoff is described synchronously, before the
//     page navigates away, and reported through an exposed binding
// State lives in this closure; built-ins are captured up front so page scripts that
// patch prototypes cannot change our behaviour.
(() => {
  'use strict';
  if (window.__cu) return;

  const W = window;
  const D = document;
  const ArrayFrom = Array.from.bind(Array);
  const ObjKeys = Object.keys;
  const MapCtor = Map;
  const SetCtor = Set;
  const mathRandom = Math.random;
  const getStyle = W.getComputedStyle.bind(W);
  const DOC_ID = 'd' + mathRandom().toString(36).slice(2, 10) + Date.now().toString(36);

  const INTERACTIVE = new SetCtor(['link', 'button', 'textbox', 'combobox', 'listbox', 'checkbox', 'radio', 'slider', 'clickable']);
  const FORM_ROLES = new SetCtor(['textbox', 'combobox', 'listbox', 'checkbox', 'radio', 'slider']);
  const SKIP_TAGS = new SetCtor(['script', 'style', 'noscript', 'head', 'meta', 'template', 'link', 'title']);
  const MAX_ITEMS = 600;
  const MAX_TABLE_ROWS = 40;
  const UNSTABLE_ATTR = /(^ctl\d|_x[0-9a-f]{3,}|[0-9a-f]{8,}|\d{4,}|^[A-Za-z0-9_-]{24,}$)/i;
  const SECRET_FIELD = /(password|passcode|\bpin\b|ssn|social security|secret|security answer)/i;
  const MONEY = /^-?\$\s?\d{1,3}(,\d{3})*(\.\d{2})?$|^-?\$\s?\d+(\.\d{2})?$/;

  let refs = new MapCtor();
  let obsId = 0;

  // ------------------------------------------------------------------ text helpers
  const norm = (s) => String(s == null ? '' : s).replace(/[\s ]+/g, ' ').trim();
  const normKey = (s) => norm(s).toLowerCase().replace(/[\s:*]+$/, '');

  function inputType(el) {
    return (el.getAttribute('type') || 'text').toLowerCase();
  }

  function textOf(el, depth) {
    depth = depth || 0;
    if (!el || depth > 25) return '';
    let out = '';
    for (const node of el.childNodes) {
      if (node.nodeType === 3) {
        out += node.nodeValue;
      } else if (node.nodeType === 1) {
        const t = node.tagName.toLowerCase();
        if (SKIP_TAGS.has(t) || t === 'select' || t === 'textarea') continue;
        if (t === 'img') { out += ' ' + (node.getAttribute('alt') || '') + ' '; continue; }
        if (t === 'input') {
          const it = inputType(node);
          if (it === 'button' || it === 'submit' || it === 'reset') out += ' ' + (node.value || '') + ' ';
          continue;
        }
        if (t === 'br') { out += ' '; continue; }
        if (!isRendered(node)) continue;
        out += ' ' + textOf(node, depth + 1) + ' ';
      }
    }
    return out;
  }

  // ------------------------------------------------------------------ visibility
  function isRendered(el) {
    if (!el || !el.isConnected) return false;
    if (el.getClientRects().length === 0) {
      return getStyle(el).display === 'contents';
    }
    const st = getStyle(el);
    return st.visibility !== 'hidden' && st.visibility !== 'collapse';
  }

  function isVisible(el) {
    if (!isRendered(el)) return false;
    const st = getStyle(el);
    return parseFloat(st.opacity || '1') > 0;
  }

  // ------------------------------------------------------------------ roles & names
  function roleOf(el) {
    if (!el || el.nodeType !== 1) return null;
    const explicit = el.getAttribute('role');
    if (explicit) return explicit.split(/\s+/)[0].toLowerCase();
    const tag = el.tagName.toLowerCase();
    switch (tag) {
      case 'a':
      case 'area':
        return el.hasAttribute('href') ? 'link' : null;
      case 'button':
        return 'button';
      case 'input': {
        const t = inputType(el);
        if (t === 'hidden' || t === 'file') return null;
        if (t === 'button' || t === 'submit' || t === 'reset' || t === 'image') return 'button';
        if (t === 'checkbox') return 'checkbox';
        if (t === 'radio') return 'radio';
        if (t === 'range') return 'slider';
        return 'textbox';
      }
      case 'textarea':
        return 'textbox';
      case 'select':
        return el.multiple || el.size > 1 ? 'listbox' : 'combobox';
      case 'option':
        return 'option';
      case 'img':
        return el.getAttribute('alt') ? 'img' : null;
      case 'h1': case 'h2': case 'h3': case 'h4': case 'h5': case 'h6':
        return 'heading';
      case 'table':
        return 'table';
      case 'tr':
        return 'row';
      case 'th':
        return 'columnheader';
      case 'td':
        return 'cell';
      default:
        return el.hasAttribute('onclick') ? 'clickable' : null;
    }
  }

  function labelsFor(el) {
    let s = '';
    if (el.id) {
      try {
        for (const l of D.querySelectorAll('label[for="' + CSS.escape(el.id) + '"]')) s += ' ' + textOf(l);
      } catch (e) { /* ignore */ }
    }
    const wrap = el.closest && el.closest('label');
    if (wrap) s += ' ' + textOf(wrap);
    return norm(s);
  }

  function accName(el) {
    if (!el || el.nodeType !== 1) return '';
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const t = norm(lb.split(/\s+/).map((id) => { const n = D.getElementById(id); return n ? textOf(n) : ''; }).join(' '));
      if (t) return t;
    }
    const al = norm(el.getAttribute('aria-label'));
    if (al) return al;
    const tag = el.tagName.toLowerCase();
    const role = roleOf(el);
    if (tag === 'input') {
      const t = inputType(el);
      if (t === 'button' || t === 'submit' || t === 'reset') {
        return norm(el.value || (t === 'submit' ? 'Submit' : t === 'reset' ? 'Reset' : ''));
      }
      if (t === 'image') return norm(el.getAttribute('alt') || el.value || 'Submit');
    }
    if (tag === 'input' || tag === 'select' || tag === 'textarea') {
      const l = labelsFor(el);
      if (l) return l;
    }
    if (tag === 'img') return norm(el.getAttribute('alt'));
    if (role === 'link' || role === 'button' || role === 'heading' || role === 'cell' || role === 'columnheader' ||
        role === 'option' || role === 'clickable' || tag === 'label') {
      const t = norm(textOf(el));
      if (t) return t.slice(0, 160);
    }
    const title = norm(el.getAttribute('title'));
    if (title) return title;
    if (tag === 'input' || tag === 'textarea') return norm(el.getAttribute('placeholder'));
    return '';
  }

  // The text a human reads beside a control. Legacy layout tables put "Member #:" in
  // the previous cell; inline forms put it in the preceding text node.
  function proximityLabel(el) {
    if (!el || el.nodeType !== 1) return '';
    const isCell = el.tagName === 'TD' || el.tagName === 'TH';
    // Only form controls and value cells have "labels"; a link's neighbour is not its label.
    if (!isCell && !FORM_ROLES.has(roleOf(el))) return '';
    const cell = isCell ? el : el.closest('td,th');
    if (cell) {
      let prev = cell.previousElementSibling;
      while (prev) {
        if (!prev.querySelector('input,select,textarea,button')) {
          const t = norm(textOf(prev));
          if (t) {
            // For value cells only a label-looking neighbour counts ("Name:"), never data.
            if (!isCell || /:\s*$/.test(t)) return t.slice(0, 80);
            return '';
          }
        }
        prev = prev.previousElementSibling;
      }
      if (isCell) return '';
    }
    let n = el.previousSibling;
    let steps = 0;
    while (n && steps < 6) {
      let t = '';
      if (n.nodeType === 3) t = norm(n.nodeValue);
      else if (n.nodeType === 1 && !n.matches('input,select,textarea,button,br')) t = norm(textOf(n));
      if (t) return t.slice(0, 80);
      n = n.previousSibling;
      steps++;
    }
    return '';
  }

  function valueOf(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === 'select') {
      const o = el.options[el.selectedIndex];
      return o ? norm(o.text) : '';
    }
    if (tag === 'input' || tag === 'textarea') return el.value || '';
    return norm(el.innerText || el.textContent || '');
  }

  function isSecretField(el) {
    if (el.tagName === 'INPUT' && inputType(el) === 'password') return true;
    return SECRET_FIELD.test(accName(el) + ' ' + proximityLabel(el) + ' ' + (el.getAttribute('name') || ''));
  }

  // ------------------------------------------------------------------ tables
  function headerCells(table) {
    const row = table.rows && table.rows[0];
    return row ? ArrayFrom(row.cells).map((c) => norm(textOf(c))) : [];
  }

  function isKvTable(table) {
    if (!table.rows || table.rows.length < 1) return false;
    if (table.querySelector('table,input,select,textarea,button')) return false;
    for (const r of table.rows) {
      const n = r.cells.length;
      if (n < 2 || n % 2 !== 0) return false;
      for (let i = 0; i < n; i += 2) if (!/:\s*$/.test(norm(textOf(r.cells[i])))) return false;
    }
    return true;
  }

  function isDataTable(table) {
    if (!table.rows || table.rows.length < 2) return false;
    if (table.querySelector('table,input,select,textarea,button')) return false;
    if (isKvTable(table)) return false;
    const hdr = headerCells(table);
    if (hdr.length < 2 || hdr.some((h) => !h)) return false;
    for (const r of table.rows) if (r.cells.length !== hdr.length) return false;
    return true;
  }

  function tablesMatching(headers) {
    const want = headers.map(normKey);
    return ArrayFrom(D.querySelectorAll('table')).filter((t) => {
      if (!isVisible(t) || !isDataTable(t)) return false;
      const have = headerCells(t).map(normKey);
      return want.every((w) => have.includes(w));
    });
  }

  // ------------------------------------------------------------------ resolution
  function deepAll() {
    const out = [];
    const walk = (root) => {
      for (const el of root.querySelectorAll('*')) {
        out.push(el);
        if (el.shadowRoot) walk(el.shadowRoot);
      }
    };
    walk(D);
    return out;
  }

  function byRole(role) {
    return deepAll().filter((el) => roleOf(el) === role && isVisible(el));
  }

  function attrsMatch(el, attrs) {
    for (const k of ObjKeys(attrs)) {
      const want = attrs[k];
      if (k === 'href') {
        const h = el.getAttribute('href');
        if (h == null) return false;
        let p;
        try { p = new URL(h, D.baseURI).pathname; } catch (e) { return false; }
        if (p !== want) return false;
      } else if (el.getAttribute(k) !== want) {
        return false;
      }
    }
    return true;
  }

  function resolve(s) {
    switch (s.kind) {
      case 'role_name': {
        const k = normKey(s.name);
        return byRole(s.role).filter((el) => normKey(accName(el)) === k);
      }
      case 'label': {
        // Form controls: explicit label (accessible name) or the neighbouring text.
        // Cells: only the neighbouring "Label:" cell, never the cell's own text.
        const k = normKey(s.label);
        return byRole(s.role).filter((el) =>
          (FORM_ROLES.has(s.role) && normKey(accName(el)) === k) || normKey(proximityLabel(el)) === k);
      }
      case 'attr': {
        const tag = String(s.tag).toLowerCase();
        return deepAll().filter((el) => el.tagName.toLowerCase() === tag && isVisible(el) && attrsMatch(el, s.attrs));
      }
      case 'table':
        return tablesMatching(s.headers);
      case 'table_cell': {
        const out = [];
        for (const t of tablesMatching(s.headers)) {
          const hdr = headerCells(t).map(normKey);
          const kc = hdr.indexOf(normKey(s.row.column));
          const tc = hdr.indexOf(normKey(s.column));
          if (kc < 0 || tc < 0) continue;
          const key = normKey(s.row.equals);
          for (let i = 1; i < t.rows.length; i++) {
            const r = t.rows[i];
            if (r.cells[kc] && normKey(textOf(r.cells[kc])) === key && r.cells[tc]) out.push(r.cells[tc]);
          }
        }
        return out;
      }
      default:
        return [];
    }
  }

  // ------------------------------------------------------------------ grounding
  function stableAttrSets(el) {
    const sets = [];
    const name = el.getAttribute('name');
    if (name && !UNSTABLE_ATTR.test(name)) sets.push({ name });
    const href = el.getAttribute('href');
    if (href) {
      try {
        const p = new URL(href, D.baseURI).pathname;
        if (p && !/\d{4,}/.test(p)) sets.push({ href: p });
      } catch (e) { /* ignore */ }
    }
    const alt = el.getAttribute('alt');
    if (alt) sets.push({ alt });
    const id = el.getAttribute('id');
    if (id && !UNSTABLE_ATTR.test(id)) sets.push({ id });
    const title = el.getAttribute('title');
    if (title) sets.push({ title });
    return sets;
  }

  function cssPath(el) {
    const parts = [];
    let n = el;
    while (n && n.nodeType === 1 && n !== D.documentElement && parts.length < 12) {
      const tag = n.tagName.toLowerCase();
      let i = 1;
      let s = n.previousElementSibling;
      while (s) { if (s.tagName === n.tagName) i++; s = s.previousElementSibling; }
      parts.unshift(tag + ':nth-of-type(' + i + ')');
      n = n.parentElement;
    }
    return parts.join(' > ');
  }

  function rectOf(el) {
    const r = el.getBoundingClientRect();
    return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
  }

  function controlInfo(el) {
    const form = el.form || (el.closest && el.closest('form'));
    let action = null;
    if (form) {
      try { action = new URL(form.getAttribute('action') || D.location.href, D.baseURI).pathname; } catch (e) { action = null; }
    }
    return {
      role: roleOf(el),
      name: accName(el),
      tag: el.tagName.toLowerCase(),
      type: el.tagName === 'INPUT' ? inputType(el) : null,
      submits: !!form && ((el.tagName === 'INPUT' && (inputType(el) === 'submit' || inputType(el) === 'image')) ||
        (el.tagName === 'BUTTON' && (el.getAttribute('type') || 'submit') === 'submit')),
      form_action: action,
      form_method: form ? (form.getAttribute('method') || 'get').toLowerCase() : null,
      secret: isSecretField(el),
    };
  }

  function describe(el) {
    const role = roleOf(el);
    const name = accName(el);
    const label = proximityLabel(el);
    const candidates = [];
    const push = (strategy) => {
      let matches = [];
      try { matches = resolve(strategy); } catch (e) { matches = []; }
      candidates.push({ strategy, count: matches.length, unique: matches.length === 1 && matches[0] === el });
    };
    // A cell's "name" is its content, i.e. data; never target a cell by it.
    const nameable = role && role !== 'table' && role !== 'row' && role !== 'cell';
    if (nameable && name) push({ kind: 'role_name', role, name });
    if (role && label && (role === 'cell' || normKey(label) !== normKey(name))) push({ kind: 'label', role, label });
    const cell = el.closest && el.closest('td,th');
    const table = cell && cell.closest('table');
    if (table && isDataTable(table) && cell.parentElement !== table.rows[0]) {
      const hdr = headerCells(table);
      const col = cell.cellIndex;
      const row = cell.parentElement;
      for (let kc = 0; kc < hdr.length; kc++) {
        if (kc === col || !row.cells[kc]) continue;
        const keyText = norm(textOf(row.cells[kc]));
        if (!keyText || keyText.length > 60 || MONEY.test(keyText)) continue;
        push({ kind: 'table_cell', headers: [hdr[kc], hdr[col]], row: { column: hdr[kc], equals: keyText }, column: hdr[col] });
      }
    }
    if (el.tagName === 'TABLE' && isDataTable(el)) push({ kind: 'table', headers: headerCells(el) });
    for (const attrs of stableAttrSets(el)) push({ kind: 'attr', tag: el.tagName.toLowerCase(), attrs });
    return {
      role,
      name,
      label,
      tag: el.tagName.toLowerCase(),
      fingerprint: { tag: el.tagName.toLowerCase(), role, name, label, css_path: cssPath(el), bbox: rectOf(el) },
      candidates,
      control: controlInfo(el),
      in_data_table: !!(table && isDataTable(table)),
    };
  }

  // ------------------------------------------------------------------ snapshot
  function isRedColor(color) {
    const m = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(color || '');
    return !!m && +m[1] >= 170 && +m[2] <= 80 && +m[3] <= 80;
  }

  function textStyle(el) {
    if (!el) return { b: false, big: false, red: false };
    const st = getStyle(el);
    const weight = parseInt(st.fontWeight, 10) || 400;
    return { b: weight >= 600, big: parseFloat(st.fontSize) >= 16, red: isRedColor(st.color) };
  }

  function snapshot(opts) {
    opts = opts || {};
    refs = new MapCtor();
    obsId += 1;
    let n = opts.start || 0;
    const newRef = (el) => { n += 1; const r = 'e' + n; refs.set(r, el); return r; };
    const isFrameset = !!(D.body && D.body.tagName === 'FRAMESET');
    const items = [];
    const walk = (root) => {
      for (const node of root.childNodes) {
        if (items.length >= MAX_ITEMS) return;
        if (node.nodeType === 3) {
          const t = norm(node.nodeValue);
          if (t) {
            const item = Object.assign({ t: 'text', text: t.slice(0, 240) }, textStyle(node.parentElement));
            // Titles and red messages get refs so the model can cite them (finish / report_outcome).
            if ((item.b || item.big || item.red) && node.parentElement) item.ref = newRef(node.parentElement);
            items.push(item);
          }
          continue;
        }
        if (node.nodeType !== 1) continue;
        const el = node;
        const tag = el.tagName.toLowerCase();
        if (SKIP_TAGS.has(tag) || !isRendered(el)) continue;
        if (tag === 'table' && isKvTable(el)) {
          const pairs = [];
          for (const r of el.rows) {
            for (let i = 0; i + 1 < r.cells.length; i += 2) {
              const v = r.cells[i + 1];
              pairs.push({ label: norm(textOf(r.cells[i])), ref: newRef(v), text: norm(textOf(v)).slice(0, 200) });
            }
          }
          items.push({ t: 'kv', ref: newRef(el), pairs });
          continue;
        }
        if (tag === 'table' && isDataTable(el)) {
          const headers = headerCells(el);
          const rows = [];
          for (let i = 1; i < el.rows.length && rows.length < MAX_TABLE_ROWS; i++) {
            rows.push(ArrayFrom(el.rows[i].cells).map((c) => ({ ref: newRef(c), text: norm(textOf(c)).slice(0, 120) })));
          }
          items.push({ t: 'table', ref: newRef(el), headers, rows, total_rows: el.rows.length - 1 });
          continue;
        }
        const role = roleOf(el);
        if (role && INTERACTIVE.has(role)) {
          const item = { t: 'el', ref: newRef(el), role, name: accName(el), tag, rect: rectOf(el) };
          const label = proximityLabel(el);
          if (label && normKey(label) !== normKey(item.name)) item.label = label;
          if ((tag === 'input' || tag === 'textarea' || tag === 'select') && FORM_ROLES.has(role)) {
            item.secret = isSecretField(el);
            item.value = valueOf(el);
            if (tag === 'input') item.type = inputType(el);
          }
          if (tag === 'select') item.options = ArrayFrom(el.options).slice(0, 30).map((o) => norm(o.text));
          if (el.disabled) item.disabled = true;
          if (role === 'checkbox' || role === 'radio') item.checked = !!el.checked;
          items.push(item);
          continue;
        }
        walk(el);
        if (el.shadowRoot) walk(el.shadowRoot);
      }
    };
    if (!isFrameset && D.body) walk(D.body);
    return {
      doc_id: DOC_ID,
      obs: obsId,
      url: D.location.href,
      title: D.title,
      frameset: isFrameset,
      items,
      next: n,
    };
  }

  // Page identity without touching the ref registry (refs stay valid for the model).
  function pageState() {
    const isFrameset = !!(D.body && D.body.tagName === 'FRAMESET');
    const emph = [];
    if (!isFrameset && D.body) {
      const tw = D.createTreeWalker(D.body, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = tw.nextNode()) && emph.length < 40) {
        const t = norm(node.nodeValue);
        const parent = node.parentElement;
        if (!t || !parent || !isVisible(parent)) continue;
        const st = textStyle(parent);
        if (st.b || st.big) emph.push(t.slice(0, 120));
      }
    }
    return { doc_id: DOC_ID, url: D.location.href, frameset: isFrameset, emph, text: pageText() };
  }

  // ------------------------------------------------------------------ page text
  function pageText() {
    return D.body && D.body.tagName !== 'FRAMESET' ? norm(D.body.innerText || '') : '';
  }

  function redTexts() {
    const out = [];
    if (!D.body || D.body.tagName === 'FRAMESET') return out;
    for (const el of D.body.querySelectorAll('*')) {
      if (SKIP_TAGS.has(el.tagName.toLowerCase()) || !isVisible(el)) continue;
      let direct = '';
      for (const c of el.childNodes) if (c.nodeType === 3) direct += c.nodeValue;
      direct = norm(direct);
      if (direct && isRedColor(getStyle(el).color)) out.push(direct.slice(0, 240));
    }
    return out;
  }

  // Mark sensitive values so a screenshot `style` can paint over them (reaches inner frames).
  function markSensitive(labels) {
    const want = new SetCtor((labels || []).map(normKey));
    for (const el of D.querySelectorAll('[data-cu-mask]')) el.removeAttribute('data-cu-mask');
    if (!D.body || D.body.tagName === 'FRAMESET') return 0;
    let count = 0;
    const mark = (el) => { el.setAttribute('data-cu-mask', '1'); count++; };
    for (const el of D.body.querySelectorAll('input,textarea')) if (isSecretField(el)) mark(el);
    for (const cell of D.body.querySelectorAll('td,th')) {
      const t = norm(textOf(cell));
      if (MONEY.test(t)) { mark(cell); continue; }
      const lab = proximityLabel(cell);
      if (lab && want.has(normKey(lab))) mark(cell);
    }
    return count;
  }

  // Rectangles (frame-relative) of every visible occurrence of the given values, so
  // the caller can paint over substrings that share a text node with other text.
  function valueRects(values) {
    const wanted = (values || []).map((v) => String(v)).filter((v) => v.length >= 3);
    const out = [];
    if (!wanted.length || !D.body || D.body.tagName === 'FRAMESET') return out;
    const tw = D.createTreeWalker(D.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = tw.nextNode())) {
      const text = node.nodeValue || '';
      const lower = text.toLowerCase();
      for (const v of wanted) {
        const needle = v.toLowerCase();
        let at = lower.indexOf(needle);
        while (at >= 0) {
          const range = D.createRange();
          range.setStart(node, at);
          range.setEnd(node, at + v.length);
          for (const r of range.getClientRects()) {
            if (r.width > 0 && r.height > 0) out.push([r.x - 1, r.y - 1, r.width + 2, r.height + 2]);
          }
          at = lower.indexOf(needle, at + v.length);
        }
      }
    }
    for (const el of D.body.querySelectorAll('input,textarea')) {
      const val = String(el.value || '').toLowerCase();
      if (val && wanted.some((v) => val.includes(v.toLowerCase())) && isVisible(el)) out.push(rectOf(el));
    }
    return out;
  }

  // ------------------------------------------------------------------ capture (human input)
  function interactiveAncestor(node) {
    let n = node && node.nodeType === 1 ? node : node && node.parentElement;
    let depth = 0;
    while (n && depth < 8) {
      const r = roleOf(n);
      if (r && INTERACTIVE.has(r)) return n;
      n = n.parentElement;
      depth++;
    }
    return null;
  }

  function report(type, el, extra) {
    const fn = W.__cuReport;
    if (typeof fn !== 'function' || !el) return;
    let d;
    try { d = describe(el); } catch (e) { d = { error: String(e) }; }
    const payload = Object.assign({ type, doc_id: DOC_ID, url: D.location.href, t: Date.now(), describe: d }, extra || {});
    try { fn(payload); } catch (e) { /* ignore */ }
  }

  // Only trusted (real input) events count; page scripts cannot fabricate human actions
  // by dispatching events. (A hostile page could still call the binding directly, which
  // is why human-recorded steps are always review-only.)
  D.addEventListener('click', (ev) => {
    if (!ev.isTrusted) return;
    const el = interactiveAncestor(ev.target);
    if (el) report('click', el);
  }, true);
  D.addEventListener('change', (ev) => {
    if (!ev.isTrusted) return;
    const el = ev.target;
    if (!el || !el.tagName) return;
    const secret = isSecretField(el);
    report('change', el, { value: secret ? null : valueOf(el), secret });
  }, true);
  D.addEventListener('keydown', (ev) => {
    if (ev.isTrusted && ev.key === 'Enter' && ev.target && ev.target.tagName) report('enter', ev.target);
  }, true);

  // ------------------------------------------------------------------ public API
  const api = {
    version: 1,
    docId: () => DOC_ID,
    snapshot,
    byRef: (r) => refs.get(r) || null,
    resolve,
    count: (s) => resolve(s).length,
    describe,
    describeRef: (r) => { const el = refs.get(r); return el ? describe(el) : null; },
    pageText,
    state: pageState,
    redTexts,
    markSensitive,
    valueRects,
    readText: (el) => valueOf(el),
    readTable: (table, columns) => {
      const hdr = headerCells(table).map(normKey);
      const idx = {};
      for (const k of ObjKeys(columns)) idx[k] = hdr.indexOf(normKey(columns[k]));
      const rows = [];
      for (let i = 1; i < table.rows.length; i++) {
        const row = {};
        for (const k of ObjKeys(idx)) row[k] = idx[k] >= 0 && table.rows[i].cells[idx[k]] ? norm(textOf(table.rows[i].cells[idx[k]])) : null;
        rows.push(row);
      }
      return rows;
    },
    rect: rectOf,
    nearMisses: (s) => {
      const want = normKey(s.name || s.label || (s.attrs && (s.attrs.alt || s.attrs.name || s.attrs.href)) || '');
      const sim = (a, b) => {
        a = normKey(a); b = normKey(b);
        if (!a || !b) return 0;
        const ta = new SetCtor(a.split(' '));
        const tb = new SetCtor(b.split(' '));
        let inter = 0;
        for (const x of ta) if (tb.has(x)) inter++;
        let p = 0;
        while (p < a.length && p < b.length && a[p] === b[p]) p++;
        return Math.max(inter / (ta.size + tb.size - inter), p / Math.max(a.length, b.length));
      };
      const pool = s.role ? byRole(s.role) : deepAll().filter((el) => INTERACTIVE.has(roleOf(el)) && isVisible(el));
      return pool
        .map((el) => ({ role: roleOf(el), name: accName(el), label: proximityLabel(el), score: sim(want, accName(el) || proximityLabel(el)) }))
        .filter((c) => c.name || c.label)
        .sort((a, b) => b.score - a.score)
        .slice(0, 5);
    },
  };
  Object.defineProperty(W, '__cu', { value: api, enumerable: false, configurable: false, writable: false });
})();
