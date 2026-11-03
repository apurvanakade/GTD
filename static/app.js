// Every page's JS (base.html loads it after the page); GO, the go-keys' targets, is set inline before it.
// A background save (tick-off, drag, filing). If it fails, reload so the page shows what was really saved.
async function post(url, body) {
  try {
    const r = await fetch(url, {method: 'POST', headers: {'X-Requested-With': 'fetch'}, body});
    if (r.ok) { changed(); projectsSaved(r); return true; }
  } catch {}
  location.reload();
  return false;
}
// A background save that made a project (X-Projects-Changed, set by app.py) leaves every project list on the page
// stale: the dropdowns (project_select()) and the drop panel (project_targets()), each marked data-projects with
// its kind. Redraw them all from a fresh copy of the page, in place (scripts keep their references), each select
// keeping its pick.
function projectsSaved(r) { if (r.headers.has('X-Projects-Changed')) refreshProjects(); }
async function refreshProjects() {
  try {
    const fresh = new DOMParser().parseFromString(await (await fetch(location.href)).text(), 'text/html');
    const kinds = {};  // kind → its fresh lists, in page order, matched to the page's in that order
    fresh.querySelectorAll('[data-projects]').forEach(el => (kinds[el.dataset.projects] ??= []).push(el));
    for (const el of document.querySelectorAll('[data-projects]')) {
      const from = kinds[el.dataset.projects]?.shift();
      if (!from) continue;
      const pick = el.value;
      el.innerHTML = from.innerHTML;
      if (el.tagName === 'SELECT') el.value = [...el.options].some(o => o.value === pick) ? pick : '';
    }
  } catch {}  // only the lists go stale
}
// The nav slides away while you scroll down and comes back when you scroll up, reach the top, or point at the top
// edge. Touch scrolling (iPad) drives it too; the clamp ignores Safari's rubber band.
(() => {
  const nav = document.querySelector('nav');
  if (!nav) return;  // in a frame: no nav
  let last = scrollY;
  const show = on => document.body.classList.toggle('nav-hidden', !on);
  addEventListener('scroll', () => {
    const y = Math.max(0, Math.min(scrollY, document.documentElement.scrollHeight - innerHeight));
    if (Math.abs(y - last) < 4) return;
    show(y < last || y <= nav.offsetHeight);
    last = y;
  }, {passive: true});
  addEventListener('mousemove', e => { if (e.clientY < 40) show(true); });
  document.addEventListener('focusin', e => { if (e.target.closest('nav')) show(true); });
})();
// ⌘/ (Ctrl+/) captures from anywhere, typing in a field or in an overlay too; Ctrl+↑ goes up a level (overlays, below).
// Both work in a frame, for the page around it.
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key === '/') { e.preventDefault(); top.capture(); }
  if (e.ctrlKey && e.key === 'ArrowUp') { e.preventDefault(); top.overlays?.up(); }
});
document.addEventListener('keydown', e => {
  if (/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) || document.activeElement.isContentEditable || e.metaKey || e.ctrlKey || e.altKey) return;
  if (document.querySelector('dialog[open]') || document.body.matches('.embed')) return;  // Esc closes a dialog natively; a task's frame goes nowhere
  if (e.key === '/') { e.preventDefault(); capture(); }
  if (e.key === 'f') { e.preventDefault(); document.querySelector('.search input').select(); }
  if (e.key === '?') { e.preventDefault(); document.querySelector('.help-dialog').showModal(); }
  // ← → follow the page's own previous/next links (Review's steps).
  const rel = {ArrowLeft: 'prev', ArrowRight: 'next'}[e.key];
  const step = rel && document.querySelector(`header.page a[rel=${rel}]`);
  if (step) location = step.href;
  if (GO[e.key]) location = GO[e.key];
});
// Next's due bands: remember which you opened or folded, per page. Only a click is saved: a band
// that starts open fires 'toggle' too, which would freeze today's default.
document.querySelectorAll('details.band').forEach(d => {
  const key = `fold:${location.pathname}:${d.dataset.band}`;
  try { const v = localStorage.getItem(key); if (v !== null) d.open = v === '1'; } catch {}
  d.querySelector('summary').addEventListener('click', () => { try { localStorage.setItem(key, d.open ? '0' : '1'); } catch {} });
});
// Capture goes to the Inbox page's own box, or elsewhere (or over an overlay) to the dialog. The dialog adds in the
// background, so whatever is open under it stays as it was.
function capture() {
  const inline = !document.querySelector('dialog[open]') && document.querySelector('main .capture input');
  if (inline) return inline.select();
  const dialog = document.querySelector('.capture-dialog');
  if (!dialog.open) dialog.showModal();
  dialog.querySelector('input').select();
}
document.querySelector('.add-inbox')?.addEventListener('click', capture);
document.querySelector('.capture-dialog form')?.addEventListener('submit', async e => {
  if (e.submitter?.formMethod === 'dialog') return;  // Cancel
  e.preventDefault();
  const f = e.target;
  try {
    const r = await fetch(f.action, {method: 'POST', headers: {'X-Requested-With': 'fetch'}, body: new URLSearchParams(new FormData(f))});
    if (!r.ok) throw r.status;
    f.reset();
    f.closest('dialog').close();
    toast((await r.json()).flashes);
  } catch { return location.reload(); }
  refreshNav();
});
// The bookmarklet's popup (capture.html): adds in the background and closes; opened some other way, it stays to add
// more. "Bookmark link" makes a title optional (the page's is used). Esc closes it.
(f => {
  if (!f) return;
  const title = f.elements.title, link = f.elements.link;
  link?.addEventListener('change', () => {
    title.required = !link.checked;
    title.placeholder = link.checked ? f.elements.page_title.value || link.value : "What's on your mind?";
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') window.close(); });
  f.addEventListener('submit', async e => {
    e.preventDefault();
    try {
      const r = await fetch(f.action, {method: 'POST', headers: {'X-Requested-With': 'fetch'}, body: new URLSearchParams(new FormData(f))});
      if (!r.ok) throw r.status;
      toast((await r.json()).flashes);
    } catch { return location.reload(); }
    window.close();
    f.reset();
    title.required = true;
    title.focus();
  });
})(document.querySelector('form.capture.popup'));
// Any dialog closes on a click beside it, on the backdrop, as with Esc: its 'cancel' event, so a dialog that saves
// first (an overlay, Process) closes its own way. Pressed and let go there, so a text selection ending outside doesn't.
(() => {
  const beside = e => e.target.matches('dialog') && (r => e.clientX < r.left || e.clientX > r.right
    || e.clientY < r.top || e.clientY > r.bottom)(e.target.getBoundingClientRect());
  let down = null;
  document.addEventListener('pointerdown', e => { down = beside(e) ? e.target : null; });
  document.addEventListener('click', e => {
    if (!down || down !== e.target || !beside(e)) return;
    if (down.dispatchEvent(new Event('cancel', {cancelable: true}))) down.close();
    down = null;
  });
})();
// A field marked data-submit (the project page's status and tickler date)
// saves its form as soon as it's changed; a text box waits while its 📅 picker is open.
// A task page (form[data-save-on-change]) saves each field once it's changed: its selects reload the page, since what
// it shows depends on them; its text boxes (due, repeats, remind; on Enter or on leaving) save in the background.
// Title and notes are autosave.js's; "＋ New project…" waits for its name; a box whose 📅 picker is open waits for it.
let fieldsSaved = Promise.resolve();  // the latest such background save; autosaved() (autosave.js) waits for it
document.addEventListener('change', e => {
  const el = e.target, f = el.form;
  if (!f) return;
  if (el.matches('[data-submit]') && !el.closest('.date-field.picking')) return f.requestSubmit();
  if (!('saveOnChange' in f.dataset) || !el.name || ['title', 'notes'].includes(el.name)) return;
  if (el.value.startsWith('__new__') || el.closest('.date-field.picking')) return;
  if (el.tagName === 'SELECT' || el.name === 'new_project') return f.requestSubmit();
  const body = new URLSearchParams();
  for (const name of ['due_date', 'recur', 'remind_at']) {
    const box = f.elements[name];
    if (box && !box.disabled) body.set(name, box.value);  // a disabled box (Repeats while someday or waiting) keeps its rule
  }
  fieldsSaved = (async () => {
    try {
      const r = await fetch(f.action, {method: 'POST', headers: {'X-Requested-With': 'fetch'}, body});
      if (r.ok) { changed(); return toast((await r.json()).flashes); }
    } catch {}
    location.reload();
  })();
});
document.addEventListener('keydown', e => {  // Enter in such a box saves it the same way, without a reload
  const el = e.target;
  if (e.key === 'Enter' && el.form && 'saveOnChange' in el.form.dataset && el.matches('input:not(.big)')) { e.preventDefault(); el.blur(); }
});
// An area's balance slider (its page, H2's rows): its word follows the drag, and letting go saves it in the background.
document.querySelectorAll('.balance').forEach(b => {
  const input = b.querySelector('input'), words = JSON.parse(b.dataset.words);
  input.addEventListener('input', () => { b.classList.remove('unset'); b.querySelector('output').textContent = words[+input.value + 3]; });
  input.addEventListener('change', () => post(b.dataset.url, new URLSearchParams({balance: input.value})));
});
// A message goes as soon as it's pointed at, so it's never in the way.
document.addEventListener('mouseover', e => e.target.closest('.flashes')?.remove());
// Messages from a background save, shown like the server's.
function toast(messages) {
  document.querySelector('body > .flashes')?.remove();
  if (!messages?.length) return;
  const box = Object.assign(document.createElement('div'), {className: 'flashes'});
  box.setAttribute('role', 'status');
  for (const m of messages) box.append(Object.assign(document.createElement('p'), {className: 'flash', textContent: m}));
  document.querySelector('main').before(box);
}
// 📅 opens the native date picker; its pick lands in the text box beside it. On a touch screen the native input
// itself lies over 📅 (style.css), since iPad Safari doesn't always open showPicker(): the tap opens it.
// Pressing 📅 leaves the box: its own change waits for the pick (`picking`), which then saves it.
// The picker starts on the box's date (and time, for the Remind box) when it holds one, else today.
const startPick = field => {
  field.classList.add('picking');
  const picker = field.querySelector('.picker'), v = field.querySelector('input[name]').value.replace(' ', 'T');
  picker.value = (picker.type === 'date' ? /^\d{4}-\d\d-\d\d$/ : /^\d{4}-\d\d-\d\dT\d\d:\d\d$/).test(v) ? v : '';
  return picker;
};
document.addEventListener('pointerdown', e => {
  const field = e.target.closest('.date-field .pick, .date-field .picker')?.parentElement;
  if (field) startPick(field);
});
document.addEventListener('click', e => {
  const b = e.target.closest('.date-field .pick');
  if (b) startPick(b.parentElement).showPicker();
});
document.addEventListener('focusin', e => { if (e.target.matches('.date-field input[name]')) e.target.parentElement.classList.remove('picking'); });
document.addEventListener('change', e => {
  if (!e.target.matches('.date-field .picker')) return;
  const field = e.target.closest('.date-field'), text = field.querySelector('input[name]');
  field.classList.remove('picking');
  text.value = e.target.value.replace('T', ' ');  // "2026-10-30 17:00", as the Remind box shows it
  text.dispatchEvent(new Event('input', {bubbles: true}));  // so the box's preview follows
  text.dispatchEvent(new Event('change', {bubbles: true}));  // and a task page saves it
});
// The task page's Due, Repeats and Remind boxes: the hint beside each shows how it reads, live (/parse/<kind>). A
// repeat's next date follows the Due box too. An untouched Remind box keeps "sent" (or nothing) instead.
for (const hint of document.querySelectorAll('.hint.when')) {
  const box = hint.closest('label').querySelector('input[name]'), kind = hint.dataset.parse, shown = box.value;
  const due = box.form.elements.due_date;
  let asked = 0, wait;
  const show = () => {
    clearTimeout(wait);
    const q = box.value.trim(), n = ++asked;
    if (!q || (kind === 'remind' && box.value === shown)) { hint.textContent = (box.value && hint.dataset.sent) || ''; return; }
    const url = `/parse/${kind}?q=${encodeURIComponent(q)}` + (kind === 'recur' ? '&due=' + encodeURIComponent(due.value) : '');
    wait = setTimeout(async () => {
      try {
        const r = await (await fetch(url)).json();
        if (n === asked) hint.textContent = !r.reads ? 'not understood' : '→ ' + r.reads + (r.past ? ' · already passed' : '');
      } catch {}  // offline: the hint just doesn't follow
    }, 150);
  };
  box.addEventListener('input', show);
  if (kind === 'recur') due.addEventListener('input', show);
  if (kind !== 'remind') show();  // what a saved date or rule means, from the start
}
// A project row's ▸ folds its steps out below it; "Expand all" does every row.
const peek = (b, open) => {
  b.setAttribute('aria-expanded', open);
  b.textContent = open ? '▾' : '▸';
  b.closest('li').classList.toggle('open', open);
};
document.addEventListener('click', e => {
  const b = e.target.closest('button.peek'), all = e.target.closest('button.peek-all');
  if (b) peek(b, b.getAttribute('aria-expanded') !== 'true');
  if (!all) return;
  const open = all.textContent === 'Expand all';
  document.querySelectorAll('button.peek').forEach(b => peek(b, open));
  all.textContent = open ? 'Collapse all' : 'Expand all';
});
// Light / dark toggle (in the nav, which refreshNav() replaces, so delegated); the pick overrides the system setting and is remembered.
document.addEventListener('click', e => {
  if (!e.target.closest('.theme-toggle')) return;
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  root.dataset.theme = dark ? 'light' : 'dark';
  try { localStorage.theme = root.dataset.theme; } catch (e) {}
});
// The running timer's clocks: the pill's (since the entry started) and a task page's (its total, from the seconds at load).
const hms = s => `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
const loaded = Date.now();
const clocks = [...document.querySelectorAll('.running-timer .clock[data-start], .task-timer .clock[data-seconds]')];
if (clocks.length) setInterval(() => clocks.forEach(c => {
  const s = c.dataset.start ? (Date.now() - c.dataset.start) / 1000 : +c.dataset.seconds + (Date.now() - loaded) / 1000;
  c.textContent = hms(Math.max(0, Math.floor(s)));
}), 1000);
// Drag ⠿ to reorder a list: a project's steps, the tasks of Inbox, Next (each band), Waiting and Someday, or the projects
// on the project tabs (active, waiting, someday); the new order is saved in the background. Shift-selected rows (move.js) go as a
// block, if they're all in this list.
(() => {
  let dragged = null, from = null, before = null, out = false;  // dragged: the rows on the move, in page order
  // Dropped on the drop panel (move.js), the rows leave: the list's order isn't saved, and stays as it was.
  document.querySelector('.drop-panel')?.addEventListener('drop', () => { out = true; });
  // Only a list's own rows count, not the steps a project row folds out.
  const rows = list => [...list.children].filter(li => li.dataset.id);
  document.querySelectorAll('ul[data-reorder]').forEach(list => {
    // Only the grip starts a drag, so titles stay links and text stays selectable.
    list.addEventListener('pointerdown', e => { const g = e.target.closest('.grip'); if (g) g.closest('li').draggable = true; });
    list.addEventListener('pointerup', () => list.querySelectorAll('li[draggable]').forEach(li => li.removeAttribute('draggable')));
    list.addEventListener('dragstart', e => {
      const li = e.target.closest('li[draggable]');
      if (!li) return;
      const picked = li.classList.contains('selected') ? [...document.querySelectorAll('main li.task.selected')] : [li];
      if (picked.some(r => r.parentElement !== list)) return;  // a selection across lists only moves out (move.js)
      dragged = picked;
      from = list;
      before = rows(list);
      out = false;
      dragged.forEach(r => r.classList.add('dragging'));
      e.dataTransfer.effectAllowed = 'move';
    });
    list.addEventListener('dragover', e => {
      if (!dragged || list !== from) return;
      // A project's next actions are pinned on top: they reorder among themselves, its other steps below them; a
      // block of both only moves out, to another project.
      const pinned = li => 'pinned' in li.dataset, pin = pinned(dragged[0]);
      if (dragged.some(r => pinned(r) !== pin)) return;
      const over = rows(list).find(li => li.contains(e.target));
      if (!over || pinned(over) !== pin) return;
      e.preventDefault();
      if (dragged.includes(over)) return;
      const r = over.getBoundingClientRect();
      if (e.clientY < r.top + r.height / 2) over.before(...dragged); else over.after(...dragged);
    });
    list.addEventListener('drop', e => e.preventDefault());
  });
  document.addEventListener('dragend', () => {
    if (!dragged) return;
    const list = from, ids = rows(list).map(li => li.dataset.id);
    dragged.forEach(r => { r.classList.remove('dragging'); r.removeAttribute('draggable'); });
    if (out) list.append(...before);
    else if (ids.join() !== before.map(li => li.dataset.id).join()) post(list.dataset.reorder, new URLSearchParams({ids: ids.join(',')}));
    dragged = from = before = null;
  });
})();
// Search suggests fuzzy title matches as you type; Enter or a click opens the highlighted one.
// With no suggestions, Enter falls back to the full-text results page.
(() => {
  const form = document.querySelector('.search');
  if (!form) return;  // in a frame: no search bar
  const input = form.querySelector('input'), box = form.querySelector('.suggest');
  let items = [], active = 0, seq = 0, timer;
  const esc = s => s.replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'})[c]);
  const show = () => {
    box.innerHTML = items.map((s, i) => `<a href="${esc(s.url)}" class="${i === active ? 'on' : ''}"><span>${esc(s.title)}</span>`
      + `<span class="meta">${esc([s.meta, s.kind].filter(Boolean).join(' · '))}</span></a>`).join('');
    box.hidden = !items.length;
  };
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const mine = ++seq, q = input.value.trim();
      let found = [];
      try { found = q ? await (await fetch(`${form.action}/suggest?q=${encodeURIComponent(q)}`)).json() : []; } catch {}
      if (mine !== seq) return;  // a later keystroke already asked again
      items = found; active = 0; show();
    }, 80);
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'Escape') { items = []; show(); input.blur(); }
    if (!items.length) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      active = (active + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length;
      show();
    }
    if (e.key === 'Enter') { e.preventDefault(); box.querySelector('a.on').click(); }  // a record opens in its overlay
  });
  input.addEventListener('focus', () => { if (items.length) box.hidden = false; });
  document.addEventListener('pointerdown', e => { if (!form.contains(e.target)) box.hidden = true; });
})();
// A time entry saves once focus leaves its row with a time changed, never mid-typing, so start and end
// can be fixed together. An end before the start isn't sent; the row is marked until it's fixed.
document.addEventListener('focusout', e => {
  const f = e.target.closest('form.entry');
  if (!f || f.contains(e.relatedTarget)) return;
  const [start, end] = [f.elements.start, f.elements.end];
  if (start.value === start.defaultValue && end.value === end.defaultValue) return;
  const bad = !start.value || (end.value && end.value < start.value);
  f.classList.toggle('bad', bad);
  if (!bad) f.requestSubmit();
});
// Tick off a task in place instead of reloading the page; its band's count drops and the nav's dots are refetched.
// The struck-out row turns into a done row: a ✓ and Revive instead of its buttons, so a click opens it and can't
// finish it twice.
document.addEventListener('submit', async e => {
  const f = e.target;
  if (!f.classList.contains('done')) return;
  e.preventDefault();
  if (!await post(f.action)) return;
  const li = f.closest('li');
  li.classList.add('gone');
  li.draggable = false;
  f.replaceWith(Object.assign(document.createElement('span'), {className: 'check', title: 'Done', textContent: '✓'}));
  li.querySelectorAll('form').forEach(x => x.remove());
  const revive = Object.assign(document.createElement('form'), {method: 'post', className: 'row-btn',
    action: f.action.replace(/complete$/, 'revive')});
  revive.append(Object.assign(document.createElement('button'), {title: 'Bring it back', textContent: 'Revive'}));
  li.append(revive);
  const n = li.closest('details.band')?.querySelector('summary b');
  if (n) n.textContent = Math.max(0, n.textContent - 1);
  refreshNav();
});
// The nav's dots after a change made in place (tick-off, a row dragged to another project).
// /nav renders it alone, without knowing the page, so the current tab keeps its own highlight.
async function refreshNav() {
  try {
    const old = document.querySelector('nav');
    if (!old) return;
    const fresh = new DOMParser().parseFromString(await (await fetch('/nav')).text(), 'text/html').querySelector('nav');
    if (!fresh) return;
    fresh.querySelectorAll('a').forEach(a => a.classList.toggle('on', !!old.querySelector(`a.on[href="${a.getAttribute('href')}"]`)));
    old.replaceWith(fresh);
  } catch {}  // only the dots go stale
}
// A click anywhere on a row opens it, as its title link would. Its buttons, links, fields, grip and folded-out
// steps keep their own jobs, a click that ends a text selection selects, and a shift-click selects the row (move.js).
document.addEventListener('click', e => {
  const li = e.target.closest('li.task');
  if (!li || e.button || e.shiftKey || e.target.closest('a, button, input, select, textarea, label, form, .grip, .steps, .balance')
      || String(getSelection())) return;
  const a = li.querySelector(':scope > .title, :scope > .name > .title');
  if (!a) return;
  if (e.metaKey || e.ctrlKey) open(a.href); else a.click();  // a record opens in its overlay (below)
});
// Every task, project, horizon and reference item opens in an overlay over the page it was clicked on, its page in a
// frame (?embed=overlay), so closing it is going back. Overlays stack: a step opened from its project's overlay sits on
// top of it, and a record already open below is gone back to rather than opened twice. ⌘/Ctrl-click opens a new tab,
// where /tasks/<id> (or /projects/<id>…) opens its list with it open (?task=<id>, as a reminder's tap does). Closing
// (×, Esc, a click beside it) saves what's typed first, and reloads what's under it (the overlay below, else the page)
// if the record changed (changed(), from the frame), so it shows the change.
function changed() { if (frameElement) frameElement.dataset.changed = '1'; }
// A frame holding a record's page (an overlay, Process's) takes the height of the page in it, growing as notes do.
function fitFrame(frame) {
  let watch = null;
  const fit = () => {
    watch?.disconnect();
    const html = frame.contentDocument?.documentElement;
    if (!html || frame.src === 'about:blank') return;
    watch = new ResizeObserver(() => frame.style.height = html.offsetHeight + 'px');
    watch.observe(html);
  };
  frame.addEventListener('load', fit);
  fit();  // loaded before this ran (?task=<id>)
}
// Everything typed in a frame's page saved: the box being typed in leaves it (saving it), title and notes through
// autosave. False on a conflict, to be settled in the frame first; true with nothing loaded.
function frameSaved(frame) {
  const w = frame.contentWindow;
  w?.document.activeElement?.blur?.();
  return w?.autosaved?.() ?? Promise.resolve(true);
}
// The record a link opens ("/projects/4"), or nothing for any other link.
const RECORD = /^\/(tasks|projects|horizons|reference)\/\d+$/;
const recordOf = href => {
  const u = new URL(href, location);
  return u.origin === location.origin && !u.search && RECORD.test(u.pathname) && u.pathname;
};
if (frameElement) {
  document.addEventListener('submit', changed, true);
  document.addEventListener('keydown', e => {  // Esc in the frame closes its overlay
    if (e.key === 'Escape' && !document.querySelector('dialog[open]')) frameElement.closest('dialog')?.querySelector('.close')?.click();
  });
}
// A record's link opens its overlay, in a frame too (on the page around it).
document.addEventListener('click', e => {
  const a = e.target.closest('a[href]'), path = a && recordOf(a.href);
  if (!path || !top.overlays || e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  top.overlays.open(path);
});
window.overlays = document.body.matches('.embed') ? null : (() => {
  const stack = [];  // {dialog, frame, path}, bottom first
  let closing = false;
  const adopt = dialog => {
    const frame = dialog.querySelector('iframe'), o = {dialog, frame, path: new URL(frame.src).pathname};
    fitFrame(frame);
    dialog.addEventListener('cancel', e => { e.preventDefault(); close(); });
    dialog.querySelector('.close').addEventListener('click', () => close());
    stack.push(o);
    dialog.showModal();
  };
  const open = path => {
    const at = stack.findIndex(o => o.path === path);
    if (at >= 0) return closeTo(at + 1);  // already open below: back to it
    const dialog = Object.assign(document.createElement('dialog'), {className: 'overlay'});
    dialog.innerHTML = '<button type="button" class="close icon" title="Close (Esc)" aria-label="Close">×</button><iframe title="Overlay"></iframe>';
    dialog.querySelector('iframe').src = path + '?embed=overlay';
    document.body.append(dialog);
    adopt(dialog);
  };
  // The top overlay, once what's typed in it is saved; false if it stays (a conflict).
  const close = async () => {
    const o = stack.at(-1);
    if (!o || closing) return false;
    closing = true;
    const saved = await frameSaved(o.frame);
    closing = false;
    if (!saved) return false;
    stack.pop();
    o.dialog.close();
    o.dialog.remove();
    const [, kind, id] = o.path.match(/^\/(\w+?)s?\/(\d+)$/), url = new URL(location);  // ?task=<id>, ?project=<id>…
    if (url.searchParams.get(kind) === id) { url.searchParams.delete(kind); history.replaceState(null, '', url); }
    if (o.frame.dataset.changed) {
      const below = stack.at(-1);
      if (below) { below.frame.dataset.changed = '1'; below.frame.contentWindow.location.reload(); }
      else if (!document.querySelector('dialog[open]')) location.reload();  // not under Process, which redraws itself
    }
    return true;
  };
  const closeTo = async n => { while (stack.length > n) if (!await close()) return false; return true; };
  // Ctrl+↑: from the top overlay to its record's parent (its <link rel=up>): a step's project, opened in its place
  // unless it's the overlay below; a list or Horizons card, gone to with every overlay closed.
  const up = async () => {
    const o = stack.at(-1);
    if (!o) return;
    const doc = () => o.frame.contentDocument;
    if (doc()?.readyState !== 'complete' || doc().URL === 'about:blank')  // pressed before its page is in
      await new Promise(done => o.frame.addEventListener('load', done, {once: true}));
    const href = doc()?.querySelector('link[rel=up]')?.href;
    const path = href && recordOf(href);
    if (path) { if (await close() && stack.at(-1)?.path !== path) open(path); return; }
    if (!await closeTo(0) || !href) return;
    const u = new URL(href);
    if (u.pathname + u.search !== location.pathname + location.search) location = href;
  };
  document.querySelectorAll('dialog.overlay').forEach(adopt);  // ?project=<id>, ?task=<id>…, bottom first
  return {open, up};
})();
