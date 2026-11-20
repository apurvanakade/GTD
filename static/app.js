// Every page's JS (base.html loads it after the page); GO, the go-keys' targets, is set inline before it.
// A background save (tick-off, drag, filing): its response once saved, else false. If it fails, reload so the page
// shows what was really saved.
// Marked changed before it's sent, and tracked (postsSettled()), so an overlay closed at once still waits for it and
// reloads what's under it.
const posting = new Set();
function postsSettled() { return Promise.allSettled([...posting]); }
function post(url, body) {
  changed();
  const p = (async () => {
    try {
      const r = await fetch(url, {method: 'POST', headers: {'X-Requested-With': 'fetch'}, body});
      if (r.ok) { projectsSaved(r); return r; }
    } catch {}
    location.reload();
    return false;
  })();
  posting.add(p);
  p.finally(() => posting.delete(p));
  return p;
}
// A background save that made a project (X-Projects-Changed, set by app.py) leaves every project list on the page
// stale: the dropdowns (project_select()) and the drop panel (project_targets()), each marked data-projects with
// its kind, and the File to menu (file_menu(), below). Redraw the marked ones from a fresh copy of the page, in place
// (scripts keep their references), each select keeping its pick; drop the menu's cached copy, so its next opening
// fetches it again.
function projectsSaved(r) { if (r.headers.has('X-Projects-Changed')) refreshProjects(); }
let fileMenuHtml = null;  // the File to menu's HTML, fetched once (below); refreshProjects() drops it
async function refreshProjects() {
  fileMenuHtml = null;
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
// Files task rows (a drop on the drop panel, move.js; a pick in a row's File to menu, below) at /tasks/move.
// Moved rows leave the list, the nav's dots follow and the move's messages (a dated next action went to the
// calendar) show as toasts. Any row where the page asks (Waiting: a waiting step stays waiting), a target whose tasks
// the page also lists (`data-stays`: H0 on Next), or a page with no drop panel (the Agenda, a project page, Search:
// a filed row may stay, changed) redraws the page instead: it posts `reload`, so the messages wait for the redrawn page.
// A band's count (Someday's, `details.band > summary > b`) after rows left it or were ticked off in place: its open
// rows. A band left with no row hides. Only the given rows' bands, so Done's (closed rows) never recount.
const bandsOf = rows => new Set([...rows].map(li => li.closest('details.band')).filter(Boolean));
function recount(bands) {
  bands.forEach(d => {
    const n = d.querySelector(':scope > summary > b');
    if (n) n.textContent = d.querySelectorAll('li.task:not(.gone)').length;
    d.hidden = !d.querySelector('li.task');
  });
}
async function fileTasks(url, rows, data) {
  const panel = document.querySelector('.drop-panel');
  const reload = !panel || 'reload' in panel.dataset || data.project_id === panel.dataset.stays;
  const form = new URLSearchParams(data);
  rows.forEach(li => form.append('id', li.dataset.id));
  if (reload) form.set('reload', '1');
  const r = await post(url, form);
  if (!r) return;
  if (reload) return keepPeeks(), location.reload();  // a project tab's fold-outs stay open
  const bands = bandsOf(rows);
  rows.forEach(li => li.remove());
  recount(bands);
  refreshNav();
  if (r.status !== 204) try { toast((await r.json()).flashes); } catch {}  // saved: only a message is lost
}
// A task row's → (file_to_button() in _task.html) opens the File to menu under it: the drop panel's places
// (file_menu(), fetched once per page from /tasks/file-to, again after refreshProjects()), its own place marked and
// not offered. Typing filters them; ↑ ↓ and Enter pick, as a click does; "＋ New project…" asks for the name first.
// A pick files the row, or every shift-selected row with it (as a drag of a selected row does). Esc or a click
// elsewhere closes it (a popover, so in the top layer, over an overlay too).
(() => {
  let menu = null, rows = [], button = null;
  const load = async () => {
    const html = fileMenuHtml ??= fetch('/tasks/file-to').then(r => r.ok ? r.text() : Promise.reject());
    try { return await html; }
    catch { if (fileMenuHtml === html) fileMenuHtml = null; return null; }
  };
  document.addEventListener('pointerover', e => { if (e.target.closest?.('button.file-to')) load(); });  // ready by the click
  const targets = () => [...menu.querySelectorAll('.file-target:not([hidden], :disabled)')];
  const light = t => {  // the place Enter picks, kept in view in the list (not scrollIntoView, which may scroll the page)
    menu.querySelectorAll('.file-target.on').forEach(o => o.classList.remove('on'));
    if (!t) return;
    t.classList.add('on');
    const list = menu.querySelector('.file-list'), at = t.getBoundingClientRect(), box = list.getBoundingClientRect();
    if (at.top < box.top) list.scrollTop -= box.top - at.top + 24;  // and its group's heading
    else if (at.bottom > box.bottom) list.scrollTop += at.bottom - box.bottom;
  };
  const filter = () => {
    const q = menu.querySelector('.file-filter').value.trim().toLowerCase();
    menu.querySelectorAll('.file-target').forEach(t => t.hidden = q ? t.matches('.new') || !t.textContent.toLowerCase().includes(q) : false);
    menu.querySelectorAll('section').forEach(s => s.hidden = !s.querySelector('.file-target:not([hidden])'));
    light(q ? targets()[0] : null);
  };
  const place = () => {  // under the button, its right edge on the button's; above it when there's more room there
    const b = button.getBoundingClientRect(), gap = 4, edge = 8;
    const below = innerHeight - b.bottom - gap - edge, above = b.top - gap - edge, up = below < 260 && above > below;
    menu.style.maxHeight = (up ? above : below) + 'px';
    menu.style.left = Math.max(edge, Math.min(b.right - menu.offsetWidth, innerWidth - menu.offsetWidth - edge)) + 'px';
    menu.style.top = up ? '' : b.bottom + gap + 'px';
    menu.style.bottom = up ? innerHeight - b.top + gap + 'px' : '';
  };
  const pick = t => {
    if (!t || t.disabled) return;
    if (t.matches('.new')) {  // the name first, in the box under it
      menu.querySelectorAll('section.naming').forEach(s => s.classList.remove('naming'));
      t.closest('section').classList.add('naming');
      return t.nextElementSibling.focus({preventScroll: true});
    }
    const data = {project_id: t.dataset.project};
    if (t.matches('.new-name')) Object.assign(data, {project_id: t.previousElementSibling.dataset.project, new_project: t.value.trim()});
    const url = menu.querySelector('.file-menu-body').dataset.move, moving = rows;
    menu.hidePopover();
    fileTasks(url, moving, data);
  };
  const open = async b => {
    const li = b.closest('li[data-id]');
    const fresh = await load();
    if (!fresh) return;
    if (!menu) {
      menu = Object.assign(document.createElement('div'), {className: 'file-menu', popover: 'auto'});
      menu.setAttribute('role', 'menu');
      document.body.append(menu);
      menu.addEventListener('toggle', e => { if (e.newState === 'closed') button?.setAttribute('aria-expanded', 'false'); });
      menu.addEventListener('click', e => pick(e.target.closest('.file-target')));
      menu.addEventListener('input', e => { if (e.target.matches('.file-filter')) filter(); });
      menu.addEventListener('keydown', e => {
        if (e.key === 'Escape') return button?.focus({preventScroll: true});  // the popover closes itself
        if (e.target.matches('.new-name')) {
          if (e.key === 'Enter' && e.target.value.trim()) { e.preventDefault(); pick(e.target); }
          return;
        }
        const all = targets(), at = all.indexOf(menu.querySelector('.file-target.on'));
        const by = {ArrowDown: 1, ArrowUp: -1}[e.key];
        if (by) { e.preventDefault(); light(all[(at + by + all.length) % all.length] ?? null); }
        if (e.key === 'Enter') { e.preventDefault(); pick(all[at]); }
      });
    }
    menu.innerHTML = fresh;
    const here = menu.querySelector(`.file-target[data-project="${CSS.escape(b.dataset.at)}"]`);
    if (b.dataset.at && here) { here.disabled = true; here.classList.add('current'); here.title = 'It’s here'; }
    rows = li.classList.contains('selected') ? [...document.querySelectorAll('main li.selected[data-id]')].filter(r => r.querySelector('button.file-to')) : [li];
    button = b;
    b.setAttribute('aria-expanded', 'true');
    menu.showPopover();
    place();
    menu.querySelector('.file-list').scrollTop = 0;
    menu.querySelector('.file-filter').focus({preventScroll: true});
  };
  // A second click on its own → closes it: light dismiss closes it before the click, so the press notes it was open.
  let wasOpen = false;
  document.addEventListener('pointerdown', e => {
    wasOpen = !!menu?.matches(':popover-open') && e.target.closest?.('button.file-to') === button;
  }, true);
  document.addEventListener('click', e => {
    const b = e.target.closest('button.file-to');
    if (!b) return;
    if (wasOpen) { wasOpen = false; if (menu.matches(':popover-open')) menu.hidePopover(); return; }
    open(b);
  });
  ['resize', 'scroll'].forEach(k => addEventListener(k, () => { if (menu?.matches(':popover-open')) place(); }, {passive: true}));
})();
// The nav slides away while you scroll down and comes back when you scroll up, reach the top, or point at the top
// edge. Touch scrolling (iPad) drives it too; the clamp ignores Safari's rubber band.
(() => {
  if (!document.querySelector('nav')) return;  // in a frame: no nav
  let last = scrollY;
  // Shown throughout a drag, since its tabs are drop targets (below).
  // --nav-h: the shown nav's height, so a sticky panel below it (Review's step list) can stick under it.
  const navH = () => document.querySelector('nav')?.offsetHeight ?? 0;  // looked up each time: refreshNav() replaces it
  const show = on => {
    document.documentElement.style.setProperty('--nav-h', navH() + 'px');
    document.body.classList.toggle('nav-hidden', !on && !document.body.classList.contains('dragging-nav'));
  };
  show(true);
  addEventListener('scroll', () => {
    const y = Math.max(0, Math.min(scrollY, document.documentElement.scrollHeight - innerHeight));
    if (Math.abs(y - last) < 4) return;
    show(y < last || y <= navH());
    last = y;
  }, {passive: true});
  addEventListener('mousemove', e => { if (e.clientY < 40) show(true); });
  document.addEventListener('focusin', e => { if (e.target.closest('nav')) show(true); });
})();
const typing = () => /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) || document.activeElement.isContentEditable;
// ⌘/ (Ctrl+/) captures from anywhere, typing in a field or in an overlay too; Ctrl+↑ goes up a level (overlays, below);
// ← → walk an overlay's task list, unless typing. All work in a frame, for the page around it.
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key === '/') { e.preventDefault(); top.capture(); }
  if (e.ctrlKey && e.key === 'ArrowUp') { e.preventDefault(); top.overlays?.up(); }
  const by = {ArrowLeft: -1, ArrowRight: 1}[e.key];
  if (by && !typing() && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey && !document.querySelector('dialog[open]:not(.overlay)')) top.overlays?.walk(by);
});
document.addEventListener('keydown', e => {
  if (typing() || e.metaKey || e.ctrlKey || e.altKey) return;
  if (document.querySelector('dialog[open]') || document.body.matches('.embed')) return;  // Esc closes a dialog natively; a task's frame goes nowhere
  if (e.key === '/') { e.preventDefault(); capture(); }
  if (e.key === 'f') { e.preventDefault(); document.querySelector('.search input').select(); }
  if (e.key === '?') { e.preventDefault(); document.querySelector('.help-dialog').showModal(); }
  // ← → follow the page's own previous/next links (the review step's footer).
  const rel = {ArrowLeft: 'prev', ArrowRight: 'next'}[e.key];
  const step = rel && document.querySelector(`main a[rel=${rel}]`);
  if (step) location = step.href;
  if (GO[e.key]) location = GO[e.key];
});
// Done's and Someday's bands: remember which you opened or folded, per page. Only a click is saved: a band that starts open fires
// 'toggle' too, which would freeze today's default.
const foldKey = d => `fold:${location.pathname}:${d.dataset.band}`;
const restoreBands = () => document.querySelectorAll('details.band').forEach(d => {
  try { const v = localStorage.getItem(foldKey(d)); if (v !== null) d.open = v === '1'; } catch {}
});
restoreBands();
document.addEventListener('click', e => {
  const summary = e.target.closest('details.band > summary');
  if (!summary || e.target.closest('a')) return;
  const d = summary.parentElement;
  try { localStorage.setItem(foldKey(d), d.open ? '0' : '1'); } catch {}
});
// Capture goes to the Inbox page's own box, or elsewhere (or over an overlay) to the dialog. The dialog adds in the
// background, so whatever is open under it stays as it was.
function capture() {
  const inline = !document.querySelector('dialog[open]') && document.querySelector('main .capture input[name=category]');
  if (inline) return inline.select();
  const dialog = document.querySelector('.capture-dialog');
  if (!dialog.open) dialog.showModal();
  dialog.querySelector('input[name=category]').select();  // the category box first: Tab goes on to the title
}
document.querySelector('.add-inbox')?.addEventListener('click', capture);
// A click on an idle task add line (its category box folded away, style.css) starts in the category box, as capture
// does; Tab goes on to the title. Once the line is in use, clicks land where they're aimed.
document.addEventListener('mousedown', e => {
  const form = e.target.closest?.('form.add');
  const cat = form?.elements.category;
  if (!cat || cat.offsetWidth) return;  // folded only while idle
  e.preventDefault();
  cat.focus();
});
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
// more, or if something typed couldn't be read. "Bookmark link" makes a title optional (the page's is used). Esc closes it.
(f => {
  if (!f) return;
  const title = f.elements.title, link = f.elements.link;
  const linked = () => {
    title.required = !link?.checked;
    title.placeholder = link?.checked ? f.elements.page_title.value || link.value : "What's on your mind?";
  };
  link?.addEventListener('change', linked);
  linked();
  document.addEventListener('keydown', e => { if (e.key === 'Escape') window.close(); });
  f.addEventListener('submit', async e => {
    e.preventDefault();
    try {
      const r = await fetch(f.action, {method: 'POST', headers: {'X-Requested-With': 'fetch'}, body: new URLSearchParams(new FormData(f))});
      if (!r.ok) throw r.status;
      const {flashes, warned} = await r.json();
      toast(flashes);
      if (!warned) window.close();  // else it stays, or the warning would go with it
    } catch { return location.reload(); }
    f.reset();
    linked();
    title.focus();
  });
})(document.querySelector('form.capture.popup'));
// Any dialog closes on a click beside it, on the backdrop, as with Esc: its 'cancel' event, so a dialog that saves
// first (an overlay) closes its own way. Pressed and let go there, so a text selection ending outside doesn't.
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
// saves its form as soon as it's changed; a text box waits while its 📅 picker is open. A form[data-background]
// (the Vision card's area lines) saves in the background instead, as it's left (a textarea: Enter is a new line).
document.addEventListener('submit', e => {
  if (!('background' in e.target.dataset)) return;
  e.preventDefault();
  document.activeElement?.blur();
});
// A task page (form[data-save-on-change]) saves each field once it's changed: its selects reload the page, since what
// it shows depends on them; its text boxes (category, due, repeats, remind; on Enter or on leaving) save in the background.
// Title and notes are autosave.js's; "＋ New project…" waits for its name; a box whose 📅 picker is open waits for it.
let fieldsSaved = Promise.resolve();  // the latest such background save; autosaved() (autosave.js) waits for it
document.addEventListener('change', e => {
  const el = e.target, f = el.form;
  if (!f) return;
  if (el.matches('[data-submit]') && !el.closest('.date-field.picking')) return f.requestSubmit();
  if ('background' in f.dataset) return post(f.action, new FormData(f));  // form[data-background]: saved in place
  if (!('saveOnChange' in f.dataset) || !el.name || ['title', 'notes'].includes(el.name)) return;
  if (el.value.startsWith('__new__') || el.closest('.date-field.picking')) return;
  if (el.tagName === 'SELECT' || el.name === 'new_project') return f.requestSubmit();
  const body = new URLSearchParams();
  for (const name of ['category', 'due_date', 'recur', 'remind_at']) {
    const box = f.elements[name];
    if (box && !box.disabled) body.set(name, box.value);  // a disabled box (Repeats while someday or waiting) keeps its rule
  }
  fieldsSaved = (async () => {
    try {
      const r = await fetch(f.action, {method: 'POST', headers: {'X-Requested-With': 'fetch'}, body});
      if (r.ok) {
        changed();
        const {flashes, saved} = await r.json();
        for (const [name, value] of Object.entries(saved)) {  // how it read: "3pm" posted again later would read differently
          const box = f.elements[name];
          if (box.value === body.get(name) && box.value !== value) { box.value = value; box.dispatchEvent(new Event('input')); }
        }
        return toast(flashes);
      }
    } catch {}
    location.reload();
  })();
});
// ⌘Enter (Ctrl+Enter) submits the form it's typed in, as its first submit button would (a task page's hidden Save, an
// add line's ⏎, a dialog's Add). Not from the notes, where it cycles a line's list state (notes.js). A task page's
// box saves first.
document.addEventListener('keydown', e => {
  const f = e.target.closest?.('form');
  if (e.key !== 'Enter' || !(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey || !f || e.target.closest('.md-editor')) return;
  e.preventDefault();
  if ('saveOnChange' in f.dataset) e.target.blur?.();  // its change saves the box in the background
  f.requestSubmit(f.querySelector('button:not([type=button]):not([type=reset]), input[type=submit]'));
});
document.addEventListener('keydown', e => {  // Enter in such a box saves it the same way, without a reload
  const el = e.target;
  if (e.key === 'Enter' && el.form && 'saveOnChange' in el.form.dataset && el.matches('input:not(.big)')) { e.preventDefault(); el.blur(); }
});
// An area's balance slider (its page, H2's rows): its word follows the drag, and letting go saves it in the background.
// An unset one sits at 0, so letting go there fires no `change`: that's a choice too, so it's the release that counts.
document.querySelectorAll('.balance').forEach(b => {
  const input = b.querySelector('input'), words = JSON.parse(b.dataset.words);
  let saved = b.classList.contains('unset') ? null : input.value;
  const show = () => { b.classList.remove('unset'); b.querySelector('output').textContent = words[+input.value + 3]; };
  const save = () => {
    if (input.value === saved) return;
    saved = input.value;
    show();
    post(b.dataset.url, new URLSearchParams({balance: saved}));
  };
  input.addEventListener('input', show);
  input.addEventListener('change', save);
  input.addEventListener('pointerdown', () => document.addEventListener('pointerup', save, {once: true}));
});
// Contexts: one word, starting with a letter, as app.py's CONTEXT; spaces typed in a box become hyphens.
const contextName = raw => { const n = raw.trim().replace(/^@/, '').replace(/\s+/g, '-'); return /^\p{L}[\p{L}\p{N}_-]*$/u.test(n) ? n : null; };
const sameName = (a, b) => a.toLowerCase() === b.toLowerCase();
// A task's contexts (context_picker() in _task.html): a click saves in the background. A name typed in the box turns
// on the context of that name, or a new one; so does a @mark ending the title, peeled off once the title is left (or
// on Enter), not on autosave's pause, so a half-typed name makes no context.
const turnOnContext = (f, name) => {
  let pick = [...f.querySelectorAll('[name=context]')].find(c => sameName(c.value, name));
  if (!pick) {
    const label = document.createElement('label');
    pick = Object.assign(document.createElement('input'), {type: 'checkbox', name: 'context', value: name});
    label.append(pick, '@' + name);
    f.elements.new_context.before(label);
  }
  pick.checked = true;
};
document.addEventListener('change', e => {
  const el = e.target, f = document.querySelector('form.contexts');
  if (!f || el.name !== 'title' || !el.form || !('saveOnChange' in el.form.dataset)) return;
  let m, marks = 0;
  while ((m = el.value.match(/\s+@(\p{L}[\p{L}\p{N}_-]*)\s*$/u))) {
    turnOnContext(f, m[1]);
    el.value = el.value.slice(0, m.index);
    marks++;
  }
  if (marks) post(f.action, new FormData(f));
});
document.addEventListener('submit', e => {
  if (!e.target.matches('form.contexts')) return;
  e.preventDefault();
  const box = e.target.elements.new_context;
  box.blur();  // its change saves it
  box.focus();
});
document.addEventListener('change', e => {
  const f = e.target.closest('form.contexts');
  if (!f) return;
  const box = f.elements.new_context;
  if (e.target === box) {
    const name = contextName(box.value);
    if (!name) { if (box.value.trim()) toast(['A context is one word, starting with a letter.']); return; }
    turnOnContext(f, name);
    box.value = '';
  }
  post(f.action, new FormData(f));
});
// Site info (the ? overlay): its contexts. A tick filters the working lists (the `contexts` cookie, read by app.py) at
// once: what the filter decides on a filtered page (each [data-filtered]) is redrawn in place from a fresh copy of it,
// under the overlay, so nothing else repaints. A filtered list's header says what's showing, and its × clears it. A
// name saves when left, a name another context has asks first, as a merge; ✕ asks first.
(() => {
  const dialog = document.querySelector('.help-dialog');
  if (!dialog) return;  // in a frame
  const list = dialog.querySelector('.context-list'), showAll = dialog.querySelector('h3 .ctx-clear');
  // Only the latest of several quick ticks is drawn; elsewhere than a filtered page the cookie is enough. Row events are
  // delegated, so the new rows need no wiring. A failed fetch reloads, so the screen never shows the wrong filter.
  let redraws = 0;
  const applyFilter = async () => {
    if (!document.querySelector('[data-filtered]')) return;
    const mine = ++redraws;
    try {
      const r = await fetch(location.href);
      if (!r.ok) throw r.status;
      const fresh = new DOMParser().parseFromString(await r.text(), 'text/html').querySelectorAll('[data-filtered]');
      if (mine !== redraws) return;
      document.querySelectorAll('[data-filtered]').forEach((el, i) => { el.innerHTML = fresh[i].innerHTML; });
      restoreBands();
    } catch { location.reload(); }
  };
  let stale = false;  // a rename or delete: the rows under the overlay no longer hold
  const setFilter = ids => { document.cookie = `contexts=${ids.join('-')}; path=/; max-age=31536000; SameSite=Lax`; };
  const saveFilter = () => {
    const ids = [...(list?.querySelectorAll('li:has(input[type=checkbox]:checked)') ?? [])].map(li => li.dataset.id);
    setFilter(ids);
    showAll.hidden = !ids.length;
  };
  // Closing with a name box still being typed in saves it first: Esc or a click beside it leaves the box (its change
  // starts the rename) before the dialog closes, since Chromium fires the box's own change only after 'close'.
  // Then the redraw waits for that save.
  let renaming = Promise.resolve();
  dialog.addEventListener('cancel', () => { if (dialog.contains(document.activeElement)) document.activeElement.blur(); });
  dialog.addEventListener('close', async () => { await renaming; if (stale) location.reload(); });
  const clearFilter = () => {
    list?.querySelectorAll('input[type=checkbox]').forEach(c => { c.checked = false; });
    saveFilter();
    applyFilter();
  };
  showAll.addEventListener('click', clearFilter);
  document.addEventListener('click', e => {
    if (e.target.closest('.ctx-open')) dialog.showModal();
    if (e.target.closest('.ctx-filter .ctx-clear')) clearFilter();
  });
  if (!list) return;
  const nameBox = li => li.querySelector('input[name=context_name]');
  list.addEventListener('change', async e => {
    const li = e.target.closest('li'), box = e.target;
    if (box.type === 'checkbox') { saveFilter(); applyFilter(); return; }
    const name = contextName(box.value);
    const other = name && [...list.children].find(o => o !== li && sameName(nameBox(o).value, name));
    if (other && !confirm(`@${nameBox(other).value} already exists. Merge @${box.defaultValue} into it? Its tasks move over.`)) {
      box.value = box.defaultValue;
      return;
    }
    let row;
    stale = true;  // set now: the dialog's close handler may already be waiting on this save
    try {
      const saving = fetch(`/contexts/${li.dataset.id}/rename`, {method: 'POST', headers: {'X-Requested-With': 'fetch'}, body: new URLSearchParams({name: box.value})});
      renaming = saving.catch(() => {});
      const r = await saving;
      if (!r.ok) throw r.status;
      row = await r.json();
    } catch { return location.reload(); }
    toast(row.flashes);
    box.value = box.defaultValue = row.name;
    if (String(row.id) === li.dataset.id) return;
    const into = list.querySelector(`[data-id="${row.id}"]`), tick = c => c.querySelector('input[type=checkbox]');
    tick(into).checked ||= tick(li).checked;  // merged: a filter on it follows
    into.querySelector('.count').textContent = row.open || '';
    li.remove();
    saveFilter();
  });
  list.addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.name === 'context_name') e.target.blur(); });
  list.addEventListener('click', async e => {
    const li = e.target.closest('.delete')?.closest('li');
    if (!li || !confirm(`Delete @${nameBox(li).value}? Its tasks stay, just without it.`)) return;
    if (!await post(`/contexts/${li.dataset.id}/delete`)) return;
    li.remove();
    saveFilter();
    stale = true;
  });
})();
// Two boxes offer what's been typed before, best first (names starting so, then a word in them starting so, then
// containing it): ↑ ↓ move, Tab, Enter or a click takes one, Esc leaves it as typed. A name typed in full offers nothing,
// so Enter adds the task.
// - @ at the end of a task's title (a line showing the marks' hint: split_title() in app.py; a task's own page): the
//   contexts so far. Taking one leaves the caret after it.
// - A task's category box (category_input() in _task.html): every category used, on any task. Taking one moves on to
//   the title. A category added here is offered from then on, without a reload.
(() => {
  const contexts = document.body.dataset.contexts.split(' ').filter(Boolean);
  const categories = JSON.parse(document.body.dataset.categories || '[]');
  const box = Object.assign(document.createElement('div'), {className: 'suggest ctx-suggest', hidden: true});
  let input = null, offer = null, items = [], active = 0;
  const hide = () => { box.hidden = true; items = []; };
  const lower = s => s.toLowerCase();
  const best = (names, q) => {
    const starts = n => lower(n).startsWith(q), word = n => lower(n).split(/\s+/).some(w => w.startsWith(q));
    return [...names.filter(starts), ...names.filter(n => !starts(n) && word(n)),
            ...names.filter(n => !starts(n) && !word(n) && lower(n).includes(q))].slice(0, 8);
  };
  // What an input offers: {q, names, show (a name as listed), take (puts a name in)}, or null.
  const offers = el => {
    if (el.matches?.('input[name=category]')) {
      const q = lower(el.value.trim());
      return q && {q, names: categories, show: n => n, take: n => {
        el.value = n;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        (el.form?.elements.title ?? el).focus();  // leaving the box: a task page saves it
      }};
    }
    if (!el.matches?.('input[name=title]') || !(el.form?.querySelector('.marks') || el.form && 'saveOnChange' in el.form.dataset)) return null;
    const m = el.value.match(/\s@([\p{L}\p{N}_-]*)$/u);
    return m && {q: lower(m[1]), names: contexts, show: n => '@' + n, take: n => {
      el.value = el.value.replace(/@[\p{L}\p{N}_-]*$/u, `@${n} `);
      el.focus();
    }};
  };
  const show = () => {
    box.replaceChildren(...items.map((n, i) => Object.assign(document.createElement('a'), {textContent: offer.show(n), className: i === active ? 'on' : ''})));
    (input.closest('dialog') ?? document.body).append(box);  // over a modal's top layer too
    const r = input.getBoundingClientRect();
    Object.assign(box.style, {left: r.left + 'px', top: r.bottom + 4 + 'px', width: Math.max(Math.min(r.width, 240), 160) + 'px'});
    box.hidden = false;
  };
  const take = i => {
    const n = items[i];
    hide();
    offer.take(n);
  };
  document.addEventListener('input', e => {
    const o = offers(e.target);
    if (!o || o.names.some(n => lower(n) === o.q)) return hide();
    input = e.target;
    offer = o;
    active = 0;
    items = best(o.names, o.q);
    items.length ? show() : hide();
  });
  document.addEventListener('keydown', e => {  // before anything else that wants these keys: Enter submits, Esc closes
    if (box.hidden) return;
    const move = {ArrowDown: 1, ArrowUp: items.length - 1}[e.key];
    if (move) active = (active + move) % items.length;
    else if (e.key === 'Tab' || e.key === 'Enter') take(active);
    else if (e.key === 'Escape') hide();
    else return;
    e.preventDefault();
    e.stopImmediatePropagation();
    if (move) show();
  }, true);
  box.addEventListener('pointerdown', e => {
    const a = e.target.closest('a');
    if (!a) return;
    e.preventDefault();  // the box keeps its focus
    take([...box.children].indexOf(a));
  });
  document.addEventListener('focusout', e => { if (e.target === input) hide(); });
  const learn = el => {  // a category just used is offered next time, tidied as db.category_name() stores it
    const n = el?.value.trim().replace(/\s+/g, ' ').replace(/:+$/, '').trim().replace(/(^| )(.)/g, (_, s, c) => s + c.toUpperCase());
    if (n && !categories.some(c => lower(c) === lower(n))) categories.unshift(n);
  };
  document.addEventListener('submit', e => { if (e.submitter?.formMethod !== 'dialog') learn(e.target.elements?.category); }, true);  // not on Cancel
  document.addEventListener('change', e => { if (e.target.matches?.('input[name=category]') && e.target.form && 'saveOnChange' in e.target.form.dataset) learn(e.target); });
})();
// A message goes as soon as it's pointed at, so it's never in the way.
document.addEventListener('mouseover', e => e.target.closest('.flashes')?.remove());
// Messages from a background save, shown like the server's.
function toast(messages) {
  document.querySelectorAll('body > .flashes').forEach(f => f.remove());
  if (!messages?.length) return;
  const box = Object.assign(document.createElement('div'), {className: 'flashes'});
  box.setAttribute('role', 'status');
  for (const m of messages) box.append(Object.assign(document.createElement('p'), {className: 'flash', textContent: m}));
  // A manual popover joins the top layer above any open dialog (Site info, an overlay), so the message is never under one.
  // It's removed once faded, so it leaves the top layer (and `:popover-open`) with its fade.
  box.popover = 'manual';
  box.addEventListener('animationend', () => box.remove());
  document.body.append(box);
  box.showPopover();
}
// 📅 opens the native date picker; its pick lands in the text box beside it. On a touch screen the native input
// itself lies over 📅 (style.css), since iPad Safari doesn't always open showPicker(): the tap opens it.
// A mouse press on 📅 keeps the focus in the box, so what's typed there saves when the box is left, picked or not.
// On a touch screen the tap lands on the picker input and leaves the box: its change waits for the pick (`picking`),
// and a picker closed without a pick saves the box itself (the picker's blur).
// The picker starts on the box's date (and time, for the Remind box) when it holds one, else today.
const startPick = field => {
  field.classList.add('picking');
  const picker = field.querySelector('.picker'), v = field.querySelector('input[name]').value.replace(' ', 'T');
  picker.value = (picker.type === 'date' ? /^\d{4}-\d\d-\d\d$/ : /^\d{4}-\d\d-\d\dT\d\d:\d\d$/).test(v) ? v : '';
  return picker;
};
document.addEventListener('mousedown', e => { if (e.target.closest('.date-field .pick')) e.preventDefault(); });
document.addEventListener('pointerdown', e => {
  const field = e.target.closest('.date-field .picker')?.parentElement;
  if (field) startPick(field);
});
document.addEventListener('focusout', e => {  // the touch picker closed: a box still waiting saves now
  const field = e.target.matches?.('.date-field .picker') && e.target.closest('.date-field.picking');
  if (!field) return;
  setTimeout(() => {
    if (!field.classList.contains('picking')) return;  // a pick arrived and saved it
    field.classList.remove('picking');
    field.querySelector('input[name]').dispatchEvent(new Event('change', {bubbles: true}));
  });
});
document.addEventListener('click', e => {
  const b = e.target.closest('.date-field .pick');
  if (!b) return;
  const picker = startPick(b.parentElement);
  b.parentElement.classList.remove('picking');  // the box kept its focus: nothing waits
  picker.showPicker();
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
  box.form.addEventListener('reset', () => setTimeout(show));  // an add form, emptied after adding
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
// What's folded out survives the reload a closing overlay makes when its record changed (overlays' close()), so
// closing still goes back to the view it was opened over. Kept for that one reload only.
const PEEKS = 'peeks:' + location.pathname;
function keepPeeks() {
  const open = [...document.querySelectorAll('li.open > button.peek')].map(b => b.closest('li').dataset.id);
  try { sessionStorage.setItem(PEEKS, JSON.stringify(open)); } catch {}
}
(() => {
  let open = null;
  try { open = JSON.parse(sessionStorage.getItem(PEEKS)); sessionStorage.removeItem(PEEKS); } catch {}
  if (!open?.length) return;
  document.documentElement.classList.add('peeks-restored');
  requestAnimationFrame(() => requestAnimationFrame(() => document.documentElement.classList.remove('peeks-restored')));
  open.forEach(id => { const b = document.querySelector(`li[data-id="${id}"] > button.peek`); if (b) peek(b, true); });
  const all = document.querySelector('button.peek-all');
  if (all && !document.querySelector('button.peek[aria-expanded="false"]')) all.textContent = 'Collapse all';
})();
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
// block, if they're all in this list. Delegated, since a context filter redraws the lists (applyFilter above).
// Rows dropped on the drop panel (move.js) or a nav tab (below) leave instead.
(() => {
  let dragged = null, from = null, before = null, out = false;  // dragged: the rows on the move, in page order
  // Dropped on the drop panel or the nav, the rows leave: the list's order isn't saved, and stays as it was. A drop
  // fires only where a dragover was taken, so any drop there was taken.
  document.querySelector('.drop-panel')?.addEventListener('drop', () => { out = true; });
  // Only a list's own rows count, not the steps a project row folds out.
  const rows = list => [...list.children].filter(li => li.dataset.id);
  const listUnder = e => e.target.closest?.('ul[data-reorder]');
  // Only the grip starts a drag, so titles stay links; a row ticked off in place stays put. It's the row's own child,
  // or in its `.head` (Horizons' goals and areas, `ul.tree`).
  document.addEventListener('pointerdown', e => { if (listUnder(e)) { const g = e.target.closest('li:not(.gone) > .grip, li:not(.gone) > .head > .grip'); if (g) g.closest('li').draggable = true; } });
  document.addEventListener('pointerup', e => listUnder(e)?.querySelectorAll('li[draggable]').forEach(li => li.removeAttribute('draggable')));
  document.addEventListener('dragstart', e => {
    const list = listUnder(e), li = list && e.target.closest('li[draggable]');
    if (li?.parentElement !== list) return;  // a project card's folded-out step only moves out (move.js)
    const picked = li.classList.contains('selected') ? [...document.querySelectorAll('main li.task.selected')] : [li];
    if (picked.some(r => r.parentElement !== list)) return;  // a selection across lists only moves out (move.js)
    dragged = picked;
    from = list;
    before = rows(list);
    out = false;
    dragged.forEach(r => r.classList.add('dragging'));
    e.dataTransfer.effectAllowed = 'move';
  });
  document.addEventListener('dragover', e => {
    const list = listUnder(e);
    if (!dragged || !list || list !== from) return;
    // A project's next actions are pinned on top: they reorder among themselves, its other steps below them; a
    // block of both doesn't move.
    const pinned = li => 'pinned' in li.dataset, pin = pinned(dragged[0]);
    if (dragged.some(r => pinned(r) !== pin)) return;
    const over = rows(list).find(li => li.contains(e.target));
    if (!over || pinned(over) !== pin) return;
    e.preventDefault();
    if (dragged.includes(over)) return;
    const r = over.getBoundingClientRect();
    if (e.clientY < r.top + r.height / 2) over.before(...dragged); else over.after(...dragged);
  });
  document.addEventListener('drop', e => {
    if (e.target.closest?.('nav')) out = true;
    else if (listUnder(e)) e.preventDefault();
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
// What can be dragged out of its list (move.js's drop panel, the nav): an open task row (it has a tick-off button)
// or a project card's folded-out step; a selected one takes every selected one with it (shift-click, move.js).
function movableTask(li) {
  return li?.matches('main ul.tasks > li.task[data-id]') && !!li.querySelector(':scope > form.done')
    || !!li?.matches('main ul.tasks > li.task > ul.steps > li[data-id]');
}
function draggedTasks(li) {
  return li.classList.contains('selected') ? [...document.querySelectorAll('main li.selected')].filter(movableTask) : [li];
}
// Drop on a nav tab, and the dragged rows go where the tab means. Tasks (any task list's rows, a project tab's
// folded-out steps, a calendar task) go to its `data-drop` (/tasks/to/<tab>, task_to_tab() in app.py): Timer times
// it, Inbox sends it back, Next and Waiting make it that step (in its project, if any), Someday an H0 someday task,
// Done completes it, Reference files it there. A project row (the project tabs) takes the tab's project status
// (/projects/<id>/move): a project tab's `data-status`, or Done's `data-project-status` (Done lists both kinds). The
// page's own tab takes nothing. Throughout the drag the nav shows (body.dragging-nav) and the tab under the pointer
// lights.
(() => {
  if (!document.querySelector('nav')) return;  // in a frame
  const body = document.body;
  let drag = null;  // {rows, status}: status, a project row's, the one tab it can't be dropped on; none for tasks
  const unmark = () => document.querySelectorAll('nav a.over').forEach(a => a.classList.remove('over'));
  const target = e => {
    const a = drag && e.target.closest?.('nav a');
    if (!a || a.classList.contains('on')) return null;
    if (drag.status) {
      const status = a.dataset.status || a.dataset.projectStatus;
      return status && status !== drag.status ? {a, status} : null;
    }
    const tab = a.dataset.drop;
    return tab && !(tab === 'timer' && drag.rows.length > 1) ? {a, tab} : null;
  };
  document.addEventListener('dragstart', e => {
    const el = e.target.closest?.('li[draggable], a.ev[data-id]');
    if (!el) return;
    if (el.matches('main ul[data-status] > li')) drag = {rows: [el], status: el.parentElement.dataset.status};
    else if (el.matches('.cal a.ev')) drag = {rows: [el]};
    else if (movableTask(el)) drag = {rows: draggedTasks(el)};
    else return;
    body.classList.add('dragging-nav');
    body.classList.remove('nav-hidden');
  });
  document.addEventListener('dragover', e => {
    if (!drag) return;
    const t = target(e);
    unmark();
    if (t) { e.preventDefault(); t.a.classList.add('over'); }
  });
  document.addEventListener('drop', e => {
    const t = target(e);
    if (!t) return;
    e.preventDefault();
    (t.status ? projectsTo : tasksTo)(drag.rows, t.status || t.tab);
  });
  document.addEventListener('dragend', () => { unmark(); body.classList.remove('dragging-nav'); drag = null; });
  const projectsTo = async (rows, status) => {
    for (const li of rows) if (!await post(`/projects/${li.dataset.id}/move`, new URLSearchParams({status}))) return;
    rows.forEach(li => li.remove());
    refreshNav();
  };
  // A task list's rows leave it in place, its flashes toasted. Anything else redraws the page, the flashes waiting
  // for it: a project tab's steps (their project's ⚠ may change; the fold-outs stay open), the calendar's tasks
  // (they may stay, recolored), the timer (its pill).
  const tasksTo = async (rows, tab) => {
    const inPlace = tab !== 'timer' && rows.every(li => li.matches('main ul.tasks > li.task'));
    const form = new URLSearchParams();
    rows.forEach(li => form.append('id', li.dataset.id));
    if (!inPlace) form.set('reload', '1');
    const r = await post(`/tasks/to/${tab}`, form);
    if (!r) return;
    if (!inPlace) return keepPeeks(), location.reload();
    const bands = bandsOf(rows);
    rows.forEach(li => li.remove());
    recount(bands);
    refreshNav();
    if (r.status !== 204) try { toast((await r.json()).flashes); } catch {}
  };
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
// Tick off a task in place instead of reloading the page; its Agenda heading's or Someday band's count drops (recount())
// and the nav's dots are refetched.
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
  const n = li.closest('ul.agenda')?.previousElementSibling?.querySelector(':scope.agenda-head > b');  // its Agenda heading's count
  if (n) n.textContent = Math.max(0, n.textContent - 1);
  recount(bandsOf([li]));
  // Its timer, if running, was stopped with it (complete_task()); a done task gets no stopped pill either.
  document.querySelectorAll(`.running-timer a[href="/tasks/${li.dataset.id}"]`).forEach(a => a.closest('.running-timer').remove());
  refreshNav();
});
// The nav's dots after a change made in place (tick-off, a row dragged to another project).
// /nav renders it alone, without knowing the page, so the current tab keeps its own highlight.
let navAsked = 0;
async function refreshNav() {
  try {
    if (!document.querySelector('nav')) return;
    const mine = ++navAsked;
    const fresh = new DOMParser().parseFromString(await (await fetch('/nav')).text(), 'text/html').querySelector('nav');
    const old = document.querySelector('nav');  // now, not before the fetch: an earlier refresh may have replaced it
    if (!fresh || !old || mine !== navAsked) return;  // a later refresh is on its way, with fresher counts
    fresh.querySelectorAll('a').forEach(a => a.classList.toggle('on', !!old.querySelector(`a.on[href="${a.getAttribute('href')}"]`)));
    old.replaceWith(fresh);
  } catch {}  // only the dots go stale
}
// A click anywhere on a row opens it, as its title link would. Its buttons, links, fields, grip and folded-out
// steps keep their own jobs, a click that ends a text selection selects, and a shift-click selects the row (move.js).
document.addEventListener('click', e => {
  const step = e.target.closest('.task .steps > li');  // a folded-out step: a click beside its link opens it too
  if (step && !e.button && !e.shiftKey && !e.target.closest('a, button, .grip') && !String(getSelection())) {
    const a = step.querySelector('a');
    if (a) { if (e.metaKey || e.ctrlKey) open(a.href); else a.click(); }
    return;
  }
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
// if the record changed (changed(), from the frame), so it shows the change. A task opened from a task list has ‹ ›
// (← →), which walk that list as it stood when opened, wherever the task gets filed meanwhile.
function changed() { if (frameElement) frameElement.dataset.changed = '1'; }
// A frame holding a record's page (an overlay) takes the height of the page in it, growing as notes do.
function fitFrame(frame) {
  let watch = null;
  const fit = () => {
    watch?.disconnect();
    const doc = frame.contentDocument, html = doc?.documentElement;
    if (!html || doc.URL === 'about:blank') return;  // its first, empty document: the CSS height holds until the page loads
    watch = new ResizeObserver(() => frame.style.height = html.offsetHeight + 'px');
    watch.observe(html);
  };
  frame.addEventListener('load', fit);
  fit();  // loaded before this ran (?task=<id>)
}
// Everything typed in a frame's page saved: the box being typed in leaves it (saving it), title and notes through
// autosave. False on a conflict, to be settled in the frame first; true with nothing loaded.
async function frameSaved(frame) {
  const w = frame.contentWindow;
  w?.document.activeElement?.blur?.();
  await w?.postsSettled?.();  // a tick or a filing still on its way
  return w?.autosaved?.() ?? true;
}
// The record a link opens ("/projects/4"), or nothing for any other link.
const RECORD = /^\/(tasks|projects|horizons|reference)\/\d+$/;
const recordOf = href => {
  const u = new URL(href, location);
  return u.origin === location.origin && !u.search && RECORD.test(u.pathname) && u.pathname;
};
// The task list a link's row is in, as paths in page order: a task row's is every task row on its page (Next's bands
// run on, folded ones too), skipping those ticked off in place; another list's (a project row's folded steps, a
// review list) is its own items.
const listOf = a => {
  const li = a.closest('li');
  if (!li) return [];
  const links = li.matches('.task') ? a.ownerDocument.querySelectorAll('li.task:not(.gone) > .name > a.title')
    : li.parentElement.querySelectorAll(':scope > li > a');
  return [...new Set([...links].map(x => recordOf(x.href)))].filter(p => /^\/tasks\//.test(p));
};
if (frameElement) {
  document.addEventListener('submit', changed, true);
  // A button with data-close (a task's ✕ delete for good) posts in the background, then closes its overlay,
  // back to what's under it (reloaded, as changed). Bubbling, so autosave's hold (it prevents, saves, resubmits) runs first.
  document.addEventListener('submit', async e => {
    const by = e.submitter;
    if (e.defaultPrevented || !by?.matches('[data-close]')) return;
    e.preventDefault();
    if (await post(by.formAction, new FormData(e.target, by))) frameElement.closest('dialog')?.querySelector('.close')?.click();
  });
  // Esc in the frame closes its overlay, unless a dialog or popover in it (Outcome reached) takes it; a toast doesn't.
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !document.querySelector('dialog[open], :popover-open:not(.flashes)')) frameElement.closest('dialog')?.querySelector('.close')?.click();
  });
}
// A record's link opens its overlay, in a frame too (on the page around it).
document.addEventListener('click', e => {
  const a = e.target.closest('a[href]'), path = a && recordOf(a.href);
  if (!path || !top.overlays || e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  top.overlays.open(path, listOf(a));
});
window.overlays = document.body.matches('.embed') ? null : (() => {
  const stack = [];  // {dialog, frame, path}, bottom first
  let closing = false;
  // Draws o's ‹ ›: hidden unless its task is in its list and has company, disabled at the list's ends.
  const draw = o => {
    const at = o.list.indexOf(o.path), walk = o.dialog.querySelector('.walk'), [back, on] = walk.children;
    walk.hidden = at < 0 || o.list.length < 2;
    back.disabled = at === 0;
    on.disabled = at === o.list.length - 1;
  };
  const setList = (o, list) => {  // a record open below is gone back to, not opened twice, so it isn't walked to
    o.list = list.filter(p => p === o.path || !stack.some(x => x.path === p));
    draw(o);
  };
  const adopt = (dialog, list) => {
    const frame = dialog.querySelector('iframe'), o = {dialog, frame, path: new URL(frame.src).pathname, list: []};
    dialog.insertAdjacentHTML('beforeend', '<span class="walk" hidden><button type="button" class="icon" title="Previous (left arrow)" aria-label="Previous">‹</button><button type="button" class="icon" title="Next (right arrow)" aria-label="Next">›</button></span>');
    const [back, on] = dialog.querySelector('.walk').children;
    back.addEventListener('click', () => walk(-1));
    on.addEventListener('click', () => walk(1));
    fitFrame(frame);
    dialog.addEventListener('cancel', e => { e.preventDefault(); close(); });
    dialog.querySelector('.close').addEventListener('click', () => close());
    stack.push(o);
    dialog.showModal();
    dialog.focus();  // itself, not ×: a page just loaded would show ×'s focus ring (style.css .overlay:focus)
    if (list) return setList(o, list);
    // Opened with the page (?task=<id>): the list of the row naming it, on the page, or in the overlay below once in.
    const below = stack.at(-2)?.frame;
    const find = () => {
      const a = (below ? below.contentDocument : document)?.querySelector(`li a[href="${o.path}"]`);
      if (a) setList(o, listOf(a));
    };
    below?.addEventListener('load', find, {once: true});
    find();
  };
  // ?task=<id>, ?project=<id>… no longer names what's open.
  const forget = path => {
    const [, kind, id] = path.match(/^\/(\w+?)s?\/(\d+)$/), url = new URL(location);
    if (url.searchParams.get(kind) === id) { url.searchParams.delete(kind); history.replaceState(null, '', url); }
  };
  // The top overlay on to the next or previous task of its list, once what's typed in it is saved.
  const walk = async by => {
    const o = stack.at(-1);
    if (!o || closing || !o.list.includes(o.path)) return;
    const at = o.list.indexOf(o.path), to = o.list[at + by];
    if (!to) return;
    closing = true;
    const saved = await frameSaved(o.frame);
    closing = false;
    if (!saved) return;
    if (o.frame.contentWindow.location.pathname !== o.path) o.list.splice(at, 1);  // filed to Reference: the task is gone
    forget(o.path);
    o.path = to;
    o.frame.contentWindow.location.replace(to + '?embed=overlay');  // not a step in the browser's history
    draw(o);
  };
  const open = (path, list) => {
    const at = stack.findIndex(o => o.path === path);
    if (at >= 0) return closeTo(at + 1);  // already open below: back to it
    const dialog = Object.assign(document.createElement('dialog'), {className: 'overlay', tabIndex: -1});
    dialog.innerHTML = '<button type="button" class="close icon" title="Close (Esc)" aria-label="Close">×</button><iframe title="Overlay"></iframe>';
    dialog.querySelector('iframe').src = path + '?embed=overlay';
    document.body.append(dialog);
    adopt(dialog, list);
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
    forget(o.path);
    if (o.frame.dataset.changed) {
      const below = stack.at(-1);
      if (below) { below.frame.dataset.changed = '1'; below.frame.contentWindow.location.reload(); }
      else { keepPeeks(); location.reload(); }
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
  document.querySelectorAll('dialog.overlay').forEach(d => adopt(d));  // ?project=<id>, ?task=<id>…, bottom first
  return {open, up, walk};
})();
