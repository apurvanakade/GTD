// The Inbox's Process button (inbox.html): the top item and one question at a time, after Allen's processing
// flowchart. An answer files it as the drop panel or its page would, in the background; it leaves the list and the next
// one follows, until the inbox is empty. y / n answer, ‹ goes back a question, Esc stops.
(() => {
  const dialog = document.querySelector('dialog.process');
  const item = dialog.querySelector('.item'), left = dialog.querySelector('.left'), back = dialog.querySelector('.back');
  const sections = [...dialog.querySelectorAll('section')], select = dialog.querySelector('select[name=project_id]');
  const name = dialog.querySelector('input[name=new_project]');
  let li = null, trail = [], busy = false;
  const rows = () => [...document.querySelectorAll('main ul.tasks:not(.tickled) > li.task[data-id]')];
  const shown = () => sections.find(s => !s.hidden);
  const ask = q => {
    trail.push(q);
    sections.forEach(s => s.hidden = s.dataset.q !== q);
    back.hidden = trail.length < 2;
    (shown().querySelector('input[name], select') || shown().querySelector('button')).focus();
  };
  const next = () => {
    li = rows()[0];
    if (!li) { dialog.close(); return location.reload(); }  // inbox zero: the page's empty state
    const a = li.querySelector('.title');
    item.textContent = a.textContent;
    item.href = a.href;
    left.textContent = `${rows().length} left`;
    select.selectedIndex = 0;
    name.value = a.textContent;  // a new project is named after the item, until changed
    dialog.querySelectorAll('input[name=due_date]').forEach(box => {
      box.value = li.querySelector('.date')?.textContent.trim() || '';  // a date captured with it (@fri) stays
      box.dispatchEvent(new Event('input'));
    });
    trail = [];
    ask('actionable');
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
  const date = () => shown().querySelector('input[name=due_date]').value;
  const actions = {
    trash: () => post(`/tasks/${id()}/delete`),  // abandoned: on Done, revivable
    someday: () => move('someday'),
    reference: () => move('reference'),
    done: () => post(`/tasks/${id()}/complete`),
    file: () => {  // a later step; the next question asks whether it's the next action
      const v = select.value, fresh = v.startsWith('__new__');
      if (!v || (fresh && !name.value.trim())) { (fresh ? name : select).focus(); return false; }
      return save(fresh ? {project_id: v, new_project: name.value.trim()} : {project_id: v});
    },
    'make-next': () => post(`/tasks/${id()}/make-next`),
    later: () => true,
    h0: () => save({project_id: 'h0', step: 'next', due_date: date()}),
    waiting: () => save({project_id: 'h0', step: 'waiting', due_date: date()}),
  };
  const press = async b => {
    if (b === back) { trail.pop(); return ask(trail.pop()); }
    if (b.classList.contains('close')) return dialog.close();
    if (b.dataset.do) {
      if (busy) return;
      busy = true;
      const ok = await actions[b.dataset.do]();
      busy = false;
      if (!ok) return;
      if (b.dataset.go) return ask(b.dataset.go);
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
    const b = shown().querySelector('button.primary');
    if (b) press(b);
  });
  dialog.addEventListener('keydown', e => {
    if (e.target.matches('input, select') || e.metaKey || e.ctrlKey || e.altKey) return;
    const b = shown().querySelector(`button[data-key="${e.key}"]`);
    if (b) { e.preventDefault(); press(b); }
  });
  document.querySelector('.process-start').addEventListener('click', () => { dialog.showModal(); next(); });
})();
