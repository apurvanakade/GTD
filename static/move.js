// The drop panel on every task list (move_panel() in _task.html): it slides in while an open row is dragged, or
// stays while pinned by the header's Projects button; dropping the row on a project (or H0, its "Someday / maybe",
// Reference, "＋ New…") moves the task there (/tasks/move). A row's ⠿ grip drags it (app.js), so the same drag
// reorders its list or moves it out. Shift-click selects rows (Esc clears); dragging
// a selected row moves them all.
(() => {
  const panel = document.querySelector('.drop-panel'), body = document.body, pin = document.querySelector('.pin-panel');
  let dragged = null, naming = null;  // dragged: the rows on the move; naming: {rows, target} while a new project's name is typed
  const pinned = on => {  // remembered per browser and kind of page, like a folded band
    body.classList.toggle('pinned-panel', on);
    pin?.setAttribute('aria-pressed', on);
    try { localStorage.setItem(`${panel.dataset.key}-panel`, on ? '1' : '0'); } catch {}
  };
  try { pinned(localStorage.getItem(`${panel.dataset.key}-panel`) === '1'); } catch {}
  pin?.addEventListener('click', () => pinned(!body.classList.contains('pinned-panel')));
  panel.querySelector('.unpin').addEventListener('click', () => pinned(false));  // the panel may cover the header's button
  // Open rows only: they have a tick-off button. Done and abandoned rows stay put.
  const movable = li => li?.matches('main ul.tasks > li.task[data-id]') && li.querySelector(':scope > form.done');
  const selected = () => [...document.querySelectorAll('main li.task.selected')].filter(movable);
  const unselect = () => document.querySelectorAll('main li.task.selected').forEach(li => li.classList.remove('selected'));
  // Shift-click toggles a row, its title too (instead of opening it); no text gets selected on the way.
  document.addEventListener('mousedown', e => { if (e.shiftKey && movable(e.target.closest('li.task'))) e.preventDefault(); });
  document.addEventListener('click', e => {
    const li = e.target.closest('li.task');
    if (!e.shiftKey || !movable(li) || e.target.closest('button, input, select, textarea')) return;
    e.preventDefault();
    li.classList.toggle('selected');
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !naming) unselect(); });
  const close = () => {
    body.classList.remove('dragging-tasks', 'naming');
    if (naming) { naming.target.classList.remove('naming'); naming.target.querySelector('input').value = ''; }
    naming = null;
  };
  // Moved rows leave the list, their bands' counts drop and the nav's dots follow. A project's next action, any row
  // where the page asks (Waiting: a waiting step stays waiting), or a drop on a target whose tasks the page also
  // lists (`data-stays`: H0 on Next), redraws the page instead.
  const move = async (rows, data) => {
    const form = new URLSearchParams(data);
    rows.forEach(li => form.append('id', li.dataset.id));
    if (!await post(panel.dataset.move, form)) return;
    if ('reload' in panel.dataset || data.project_id === panel.dataset.stays || rows.some(li => 'pinned' in li.dataset)) return location.reload();
    for (const li of rows) {
      const n = li.closest('details.band')?.querySelector('summary b');
      if (n) n.textContent = Math.max(0, n.textContent - 1);
      li.remove();
    }
    refreshNav();
  };
  document.addEventListener('dragstart', e => {
    const li = e.target.closest?.('li.task');
    if (!movable(li)) return;
    dragged = li.classList.contains('selected') ? selected() : [li];
    dragged.forEach(r => r.classList.add('dragging'));
    e.dataTransfer.effectAllowed = 'move';
    body.classList.add('dragging-tasks');
  });
  document.addEventListener('dragend', () => {
    panel.querySelectorAll('.over').forEach(t => t.classList.remove('over'));
    dragged?.forEach(r => r.classList.remove('dragging'));
    dragged = null;
    if (!naming) close();
  });
  panel.addEventListener('dragover', e => {
    const t = e.target.closest('.target');
    panel.querySelectorAll('.over').forEach(o => o !== t && o.classList.remove('over'));
    if (t && dragged) { e.preventDefault(); t.classList.add('over'); }
  });
  panel.addEventListener('drop', e => {
    const t = e.target.closest('.target');
    if (!t || !dragged) return;
    e.preventDefault();
    if (t.dataset.project !== '__new__') return move(dragged, {project_id: t.dataset.project});
    // The panel stays open for the name; Enter moves it into a new project, Esc or clicking away cancels.
    naming = {rows: dragged, target: t};
    body.classList.add('naming');
    t.classList.add('naming');
    t.querySelector('input').focus();
  });
  panel.querySelectorAll('.new input').forEach(name => {
    name.addEventListener('keydown', e => {
      if (!naming) return;
      if (e.key === 'Escape') { e.preventDefault(); close(); }
      if (e.key === 'Enter' && name.value.trim()) {
        e.preventDefault();
        move(naming.rows, {new_project: name.value.trim()});
        close();
      }
    });
    name.addEventListener('blur', close);
  });
})();
