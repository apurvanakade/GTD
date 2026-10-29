// The drop panel on every task list (move_panel() in _task.html): it slides in while an open row is dragged, or
// stays while pinned by the header's Projects button; dropping the row on a project (or H0, Reference, "＋ New…")
// moves the task there (/tasks/move). Rows are dragged from anywhere, except on a project page, where the ⠿ grip
// drags them, so the same drag reorders the steps or moves one out.
(() => {
  const panel = document.querySelector('.drop-panel'), body = document.body, pin = document.querySelector('.pin-panel');
  let dragged = null, naming = null;  // naming: {li, target} while a new project's name is typed
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
  document.querySelectorAll('main ul.tasks:not([data-reorder]) > li.task[data-id]').forEach(li => { if (movable(li)) li.draggable = true; });
  const close = () => {
    body.classList.remove('dragging-tasks', 'naming');
    if (naming) { naming.target.classList.remove('naming'); naming.target.querySelector('input').value = ''; }
    naming = null;
  };
  // A moved row leaves the list, its band's count drops and the nav's dots follow. A project's next action, or any
  // row where the page asks (Waiting: a waiting step stays waiting), redraws the page instead.
  const move = async (li, data) => {
    if (!await post(panel.dataset.move, new URLSearchParams({id: li.dataset.id, ...data}))) return;
    if ('reload' in panel.dataset || 'pinned' in li.dataset) return location.reload();
    const n = li.closest('details.band')?.querySelector('summary b');
    if (n) n.textContent = Math.max(0, n.textContent - 1);
    li.remove();
    refreshNav();
  };
  document.addEventListener('dragstart', e => {
    const li = e.target.closest?.('li.task');
    if (!movable(li)) return;
    dragged = li;
    dragged.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    body.classList.add('dragging-tasks');
  });
  document.addEventListener('dragend', () => {
    panel.querySelectorAll('.over').forEach(t => t.classList.remove('over'));
    if (dragged) dragged.classList.remove('dragging');
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
    // The panel stays open for the name; Enter moves it into a new project in the target's area, Esc or clicking away cancels.
    naming = {li: dragged, target: t};
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
        move(naming.li, {new_project: name.value.trim(), area_id: naming.target.dataset.area});
        close();
      }
    });
    name.addEventListener('blur', close);
  });
})();
