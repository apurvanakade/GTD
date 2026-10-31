// The Inbox's Process button (inbox.html): the top item and one question at a time, after Allen's processing
// flowchart. The item shows as its own task page in a frame (?embed=process), so it can be clarified on the way: its title,
// notes, due date, repeat and reminder save as on its page, and are saved before it's filed. Picking an answer files
// nothing: File does, as the drop panel or its page would, in the background; the item leaves the list and the next
// one follows, until the inbox is empty; so it does when the item's own page files or finishes it. y / n answer,
// Enter files, ‹ goes back a question, Esc stops.
// /inbox?process=1 opens it; so does a click on a row, at that item (⌘/Ctrl-click still opens its page). A row ticked
// off in place (faded, `.gone`) is done: Process skips it. Stopping (×, Esc) redraws the list if an item was edited.
(() => {
  const dialog = document.querySelector('dialog.process');
  const frame = dialog.querySelector('iframe'), left = dialog.querySelector('.left'), back = dialog.querySelector('.back');
  const sections = [...dialog.querySelectorAll('section')], select = dialog.querySelector('select[name=project_id]');
  const name = dialog.querySelector('input[name=new_project]');
  let li = null, trail = [], busy = false;
  const ROW = 'main ul.tasks:not(.tickled) > li.task[data-id]:not(.gone)';
  const rows = () => [...document.querySelectorAll(ROW)];
  const shown = () => sections.find(s => !s.hidden);
  const field = n => frame.contentDocument?.querySelector(`main [name=${n}]`);  // the item's page, once loaded
  const ask = q => {
    trail.push(q);
    sections.forEach(s => s.hidden = s.dataset.q !== q);
    back.hidden = trail.length < 2;
    choose(null);  // asked again (‹), nothing is picked yet
    if (q === 'pick') name.value = field('title')?.value || name.value;  // a new project is named after the item
    (shown().querySelector('input[name], select') || shown().querySelector('button')).focus();
  };
  const next = (at = rows()[0]) => {
    li = at;
    if (!li) { dialog.close(); return location.reload(); }  // inbox zero: the page's empty state
    const title = li.querySelector('.title').textContent;
    delete frame.dataset.changed;  // edits to an item filed away don't need the list redrawn
    frame.src = `/tasks/${li.dataset.id}?embed=process`;
    left.textContent = `${rows().length} left`;
    select.selectedIndex = 0;
    name.value = title;
    trail = [];
    ask('actionable');
  };
  // An answer picked, not yet filed: it shows pressed and File comes alive.
  const choose = b => {
    const s = shown();
    s.querySelectorAll('[data-choose]').forEach(c => c.setAttribute('aria-pressed', c === b));
    s.dataset.chosen = b?.dataset.choose ?? '';
    const file = s.querySelector('[data-do]');
    if (file && s.querySelector('[data-choose]')) file.disabled = !b;
  };
  const id = () => li.dataset.id;
  const move = project_id => post('/tasks/move', new URLSearchParams({id: id(), project_id}));
  const save = async data => {  // the task page's own save, its messages shown here
    try {
      const r = await fetch(`/tasks/${id()}`, {method: 'POST', headers: {'X-Requested-With': 'fetch'}, body: new URLSearchParams(data)});
      if (r.ok) { toast((await r.json()).flashes); return true; }
    } catch {}
    location.reload();
    return false;
  };
  const actions = {
    trash: () => post(`/tasks/${id()}/delete`),  // abandoned: on Done, revivable
    someday: () => move('someday'),
    reference: () => move('reference'),
    done: () => post(`/tasks/${id()}/complete`),
    chosen: () => actions[shown().dataset.chosen](),
    project: async () => {  // a later step, or its next action if so chosen
      const v = select.value;
      return await save(v.startsWith('__new__') ? {project_id: v, new_project: name.value.trim()} : {project_id: v})
        && (shown().dataset.chosen !== 'make-next' || post(`/tasks/${id()}/make-next`));
    },
    h0: () => save({project_id: 'h0', step: 'next'}),  // its date: the frame's Due box
    waiting: () => save({project_id: 'h0', step: 'waiting'}),
  };
  fitFrame(frame);  // app.js
  // Filed, finished or abandoned on its own page (the frame's project select, ✓, ✕): dealt with, so on to the next.
  frame.addEventListener('load', () => {
    const form = frame.contentDocument?.querySelector('main form.edit');
    if (!li || !dialog.open || !form || form.hasAttribute('data-inbox')) return;
    li.remove();
    refreshNav();
    next();
  });
  // Stopping saves the item's edits first (a conflict keeps it open), and redraws the list if it changed.
  const stop = async () => {
    if (busy) return;
    busy = true;
    const saved = await frameSaved(frame);
    busy = false;
    if (!saved) return;
    dialog.close();
    if (frame.dataset.changed) location.reload();
  };
  dialog.addEventListener('cancel', e => { e.preventDefault(); stop(); });
  const press = async b => {
    if (b === back) { trail.pop(); return ask(trail.pop()); }
    if (b.classList.contains('close')) return stop();
    if (b.dataset.choose) { choose(b); return shown().querySelector('[data-do]').focus(); }
    if (b.disabled) return;
    if (b.dataset.go === 'next') {  // a project picked first, a new one named
      const fresh = select.value.startsWith('__new__');
      if (!select.value || (fresh && !name.value.trim())) return (fresh ? name : select).focus();
    }
    if (b.dataset.do) {
      if (busy) return;
      busy = true;
      const ok = await frameSaved(frame) && await actions[b.dataset.do]();
      busy = false;
      if (!ok) return;
      li.remove();
      refreshNav();
      return next();
    }
    if (b.dataset.go) ask(b.dataset.go);
  };
  dialog.addEventListener('click', e => {
    const b = e.target.closest('button:not(.pick)');
    if (b) press(b);
  });
  dialog.querySelector('form').addEventListener('submit', e => {  // Enter in a box answers with its question's main button
    e.preventDefault();
    const b = shown().querySelector('button.primary, button[data-go=next]');
    if (b) press(b);
  });
  dialog.addEventListener('keydown', e => {
    if (e.target.matches('input, select') || e.metaKey || e.ctrlKey || e.altKey) return;
    const b = shown().querySelector(`button[data-key="${e.key}"]`);
    if (b) { e.preventDefault(); press(b); }
  });
  const start = at => { dialog.showModal(); next(at); };
  document.querySelector('.process-start').addEventListener('click', () => start());
  // Ahead of app.js's row click (which would open the task page): the row's buttons, shift-click (move.js) and a
  // click ending a text selection keep their jobs.
  document.addEventListener('click', e => {
    const row = e.target.closest(ROW);
    if (!row || e.button || e.shiftKey || e.metaKey || e.ctrlKey || String(getSelection())
        || e.target.closest('button, input, select, textarea, label, form, a:not(.title)')) return;
    e.preventDefault();
    e.stopPropagation();
    start(row);
  }, true);
  // ?process=1 (the weekly review's link) opens it at once; the flag is dropped, so a reload doesn't reopen it.
  const url = new URL(location);
  if (url.searchParams.has('process')) {
    url.searchParams.delete('process');
    history.replaceState(null, '', url);
    start();
  }
})();
