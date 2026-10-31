// Every page's JS (base.html loads it after the page); GO, the go-keys' targets, is set inline before it.
// A background save (tick-off, drag, filing). If it fails, reload so the page shows what was really saved.
async function post(url, body) {
  try {
    const r = await fetch(url, {method: 'POST', headers: {'X-Requested-With': 'fetch'}, body});
    if (r.ok) return true;
  } catch {}
  location.reload();
  return false;
}
// The nav slides away while you scroll down and comes back when you scroll up, reach the top, or point at the top
// edge. Touch scrolling (iPad) drives it too; the clamp ignores Safari's rubber band.
(() => {
  let last = scrollY;
  const show = on => document.body.classList.toggle('nav-hidden', !on);
  addEventListener('scroll', () => {
    const y = Math.max(0, Math.min(scrollY, document.documentElement.scrollHeight - innerHeight));
    if (Math.abs(y - last) < 4) return;
    show(y < last || y <= document.querySelector('nav').offsetHeight);
    last = y;
  }, {passive: true});
  addEventListener('mousemove', e => { if (e.clientY < 40) show(true); });
  document.addEventListener('focusin', e => { if (e.target.closest('nav')) show(true); });
})();
// A record page (project, task, horizon, reference) opens scrolled past the nav, its body (title to the end of the
// page) centered in the window, or from the top when taller. Room above the title (main's padding) makes that possible
// however short the page; a reload or Back keeps the browser's own scroll.
(() => {
  const big = document.querySelector('main .big'), main = document.querySelector('main');
  if (!big) return;
  main.style.paddingTop = '1px';  // measured with some padding, which stops the header's margins collapsing through main
  const top = el => el.getBoundingClientRect().top + scrollY, start = top(big);
  const end = Math.max(...[...main.children].filter(el => el.tagName !== 'SCRIPT' && getComputedStyle(el).position !== 'fixed')
    .map(el => el.getBoundingClientRect().bottom + scrollY));
  const gap = Math.max(24, (innerHeight - (end - start)) / 2);
  const pad = Math.max(1, top(main) + 2 + gap - start);  // the scroll must pass the nav and search bar; the nav slides away
  main.style.paddingTop = pad + 'px';
  main.style.minHeight = `calc(100vh + ${gap}px)`;  // room to scroll that far
  const how = performance.getEntriesByType('navigation')[0]?.type;
  if (how !== 'reload' && how !== 'back_forward') scrollTo({top: start + pad - gap, behavior: 'instant'});
})();
document.addEventListener('keydown', e => {
  if (/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) || document.activeElement.isContentEditable || e.metaKey || e.ctrlKey || e.altKey) return;
  if (document.querySelector('dialog[open]')) return;  // Esc closes it natively
  if (e.key === '/') { e.preventDefault(); capture(); }
  if (e.key === 'f') { e.preventDefault(); document.querySelector('.search input').select(); }
  if (e.key === '?') { e.preventDefault(); document.querySelector('.help-dialog').showModal(); }
  // ← → follow the page's own previous/next links (Review's steps).
  const rel = {ArrowLeft: 'prev', ArrowRight: 'next'}[e.key];
  const step = rel && document.querySelector(`header.page a[rel=${rel}]`);
  if (step) location = step.href;
  if (GO[e.key]) location = GO[e.key];
});
// Next and H0's due bands: remember which you opened or folded, per page. Only a click is saved: a band
// that starts open fires 'toggle' too, which would freeze today's default.
document.querySelectorAll('details.band').forEach(d => {
  const key = `fold:${location.pathname}:${d.dataset.band}`;
  try { const v = localStorage.getItem(key); if (v !== null) d.open = v === '1'; } catch {}
  d.querySelector('summary').addEventListener('click', () => { try { localStorage.setItem(key, d.open ? '0' : '1'); } catch {} });
});
// Capture goes to the Inbox page's own box, or elsewhere to the dialog.
function capture() {
  const inline = document.querySelector('main .capture input');
  inline ? inline.select() : document.querySelector('.capture-dialog').showModal();
}
document.querySelector('.add-inbox').addEventListener('click', capture);
// A field marked data-submit (the project page's status, area and tickler date)
// saves its form as soon as it's changed; a text box waits while its 📅 picker is open.
// A task page (form[data-save-on-change]) saves each field once it's changed: its selects reload the page, since what
// it shows depends on them; its text boxes (due, repeats, remind; on Enter or on leaving) save in the background.
// Title and notes are autosave.js's; "＋ New project…" waits for its name; a box whose 📅 picker is open waits for it.
document.addEventListener('change', async e => {
  const el = e.target, f = el.form;
  if (!f) return;
  if (el.matches('[data-submit]') && !el.closest('.date-field.picking')) return f.requestSubmit();
  if (!('saveOnChange' in f.dataset) || !el.name || ['title', 'notes'].includes(el.name)) return;
  if (el.value.startsWith('__new__') || el.closest('.date-field.picking')) return;
  if (el.tagName === 'SELECT' || el.name === 'new_project') return f.requestSubmit();
  const body = new URLSearchParams();
  for (const name of ['due_date', 'recur', 'remind_at']) body.set(name, f.elements[name].value);
  try {
    const r = await fetch(f.action, {method: 'POST', headers: {'X-Requested-With': 'fetch'}, body});
    if (r.ok) return toast((await r.json()).flashes);
  } catch {}
  location.reload();
});
document.addEventListener('keydown', e => {  // Enter in such a box saves it the same way, without a reload
  const el = e.target;
  if (e.key === 'Enter' && el.form && 'saveOnChange' in el.form.dataset && el.matches('input:not(.big)')) { e.preventDefault(); el.blur(); }
});
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
// Light / dark toggle; the pick overrides the system setting and is remembered.
document.querySelector('.theme-toggle').addEventListener('click', () => {
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
// Drag ⠿ to reorder a project's steps, or the projects on Projects and Someday; the new order is saved in the background.
// There a project dragged into another area's group (same page, `data-area`) moves to that area.
(() => {
  let dragged = null, from = null;
  // Only a list's own rows count, not the steps a project row folds out.
  const rows = list => [...list.children].filter(li => li.dataset.id);
  document.querySelectorAll('ul[data-reorder]').forEach(list => {
    // Only the grip starts a drag, so titles stay links and text stays selectable.
    list.addEventListener('pointerdown', e => { const g = e.target.closest('.grip'); if (g) g.closest('li').draggable = true; });
    list.addEventListener('pointerup', () => list.querySelectorAll('li[draggable]').forEach(li => li.removeAttribute('draggable')));
    list.addEventListener('dragstart', e => {
      dragged = e.target.closest('li[draggable]');
      if (!dragged) return;
      from = list;
      dragged.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
    });
    list.addEventListener('dragover', e => {
      if (!dragged || 'pinned' in dragged.dataset) return;  // the next action only moves out, to another project
      if (list !== from && !(list.dataset.reorder === from.dataset.reorder && 'area' in list.dataset)) return;
      const over = rows(list).find(li => li.contains(e.target));
      if (!over) return;
      e.preventDefault();
      if (over === dragged) return;
      const r = over.getBoundingClientRect();
      // A project's next action is pinned to the top: nothing goes above it.
      if (e.clientY < r.top + r.height / 2 && !('pinned' in over.dataset)) over.before(dragged); else over.after(dragged);
    });
    list.addEventListener('drop', e => e.preventDefault());
  });
  document.addEventListener('dragend', () => {
    if (!dragged || !from) return;
    const list = dragged.closest('ul[data-reorder]');
    dragged.classList.remove('dragging');
    dragged.removeAttribute('draggable');
    if (!list) { dragged = from = null; return; }  // moved out to another project (move.js)
    const body = new URLSearchParams({ids: rows(list).map(li => li.dataset.id).join(',')});
    if (list !== from) {
      body.set('moved', dragged.dataset.id);
      body.set('area_id', list.dataset.area);
      // The area it left is empty now (no projects or waiting tasks): drop its group.
      if (!rows(from).length && !from.closest('.area-group')?.querySelector('ul:not([data-reorder]) > li')) (from.closest('.area-group') || from).remove();
    }
    dragged = from = null;
    post(list.dataset.reorder, body);
  });
})();
// Search suggests fuzzy title matches as you type; Enter or a click opens the highlighted one.
// With no suggestions, Enter falls back to the full-text results page.
(() => {
  const form = document.querySelector('.search'), input = form.querySelector('input'), box = form.querySelector('.suggest');
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
    if (e.key === 'Enter') { e.preventDefault(); location = items[active].url; }
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
// The struck-out row turns into a done row: a ✓ instead of its buttons, so a click opens it and can't finish it twice.
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
  const n = li.closest('details.band')?.querySelector('summary b');
  if (n) n.textContent = Math.max(0, n.textContent - 1);
  refreshNav();
});
// The nav's dots after a change made in place (tick-off, a row dragged to another project).
// /nav renders it alone, without knowing the page, so the current tab keeps its own highlight.
async function refreshNav() {
  try {
    const old = document.querySelector('nav');
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
  if (!li || e.button || e.shiftKey || e.target.closest('a, button, input, select, textarea, label, form, .grip, .steps')
      || String(getSelection())) return;
  const a = li.querySelector(':scope > .title, :scope > .name > .title');
  if (!a) return;
  if (e.metaKey || e.ctrlKey) open(a.href); else location = a.href;
});
