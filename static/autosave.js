// A record page's title and notes save themselves: 2 s after the last keystroke, on leaving the field, and on
// leaving or hiding the page. Each save sends the text the page last saw (base_*) next to the change; if someone
// else changed that field since, the server answers 409 with its text and you pick: theirs, mine, or a merge
// (notes get git-style conflict markers, the title stays yours). Coming back to the tab picks up edits made elsewhere.
// The server leaves title and notes to this: any other submit of the form (a select, ✓, ▶) saves them here first,
// and Enter in the title just saves.
document.querySelectorAll("form[data-autosave]").forEach(form => {
  const url = form.dataset.autosave, status = form.querySelector(".autosave");
  const fields = ["title", "notes"].map(n => form.elements[n]).filter(Boolean);
  const norm = s => (s ?? "").replace(/\r\n?/g, "\n").trim();
  const base = Object.fromEntries(fields.map(f => [f.name, f.value]));
  let timer = null, inflight = null, again = false, conflict = false;

  const dirty = () => fields.filter(f => norm(f.value) !== norm(base[f.name]) && (f.name !== "title" || f.value.trim()));
  const set = (f, v) => (f.setText ? f.setText(v) : (f.value = v));

  // Resolves true once everything typed is saved, false on a conflict or a failure.
  function save(keepalive = false) {
    clearTimeout(timer);
    if (conflict) return Promise.resolve(false);
    if (inflight && !keepalive) { again = true; return inflight.then(() => save()); }
    const send = dirty();
    if (!send.length) return Promise.resolve(true);
    const sent = Object.fromEntries(send.map(f => [f.name, f.value]));
    const body = new URLSearchParams(sent);
    send.forEach(f => body.append("base_" + f.name, base[f.name]));
    status.textContent = "Saving…";
    inflight = fetch(url, { method: "POST", body, keepalive })
      .then(async r => {
        if (r.status === 409) { showConflict(await r.json()); return false; }
        if (!r.ok) throw new Error(r.status);
        Object.assign(base, sent);
        status.textContent = "Saved";
        return true;
      })
      .catch(() => { status.textContent = "Not saved · retrying"; timer = setTimeout(save, 3000); return false; })
      .finally(() => { inflight = null; if (again) { again = false; save(); } });
    return inflight;
  }

  form.addEventListener("submit", async e => {
    if (form.dataset.saved) return;  // saved just now: let it go
    e.preventDefault();
    const by = e.submitter;
    if (!await save() || by?.hidden) return;  // a conflict stops it to be settled first; Enter only meant save
    form.dataset.saved = "1";
    setTimeout(() => form.requestSubmit(by));  // a task later: during this submit event a new one is ignored
  });

  function load(theirs) {
    fields.forEach(f => { base[f.name] = theirs[f.name]; set(f, theirs[f.name]); });
  }

  function showConflict(theirs) {
    conflict = true;
    clearTimeout(timer);
    status.innerHTML = 'Changed elsewhere: <button type="button" data-pick="theirs">load theirs</button>'
      + ' · <button type="button" data-pick="mine">keep mine</button>'
      + ' · <button type="button" data-pick="merge">merge</button>';
    status.onclick = e => {
      const pick = e.target.dataset?.pick;
      if (!pick) return;
      status.onclick = null;
      conflict = false;
      if (pick === "theirs") { load(theirs); status.textContent = "Loaded theirs"; return; }
      Object.assign(base, theirs);  // mine now overwrites theirs knowingly
      const notes = form.elements.notes;
      if (pick === "merge" && notes && norm(notes.value) !== norm(theirs.notes)) {
        set(notes, merge(notes.value, theirs.notes));  // saves like any edit
        status.textContent = "Merged · fix the marked lines";
        return;
      }
      save();
    };
  }

  // Git-style conflict markers around the lines that differ (shared leading/trailing lines kept once).
  function merge(mine, theirs) {
    const a = mine.split("\n"), b = theirs.split("\n");
    let p = 0, s = 0;
    while (p < a.length && p < b.length && a[p] === b[p]) p++;
    while (s < a.length - p && s < b.length - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s++;
    return [
      ...a.slice(0, p),
      "<<<<<<< mine", ...a.slice(p, a.length - s),
      "=======", ...b.slice(p, b.length - s),
      ">>>>>>> theirs", ...a.slice(a.length - s),
    ].join("\n");
  }

  // Edits made elsewhere: a field you haven't touched just updates; one you have asks.
  function refresh() {
    if (conflict || inflight || document.hidden) return;
    fetch(url).then(r => r.json()).then(theirs => {
      const mine = dirty();
      const changed = fields.filter(f => norm(theirs[f.name]) !== norm(base[f.name]));
      if (changed.some(f => mine.includes(f))) return showConflict(theirs);
      changed.forEach(f => { base[f.name] = theirs[f.name]; set(f, theirs[f.name]); });
      if (changed.length) status.textContent = "Updated from elsewhere";
    }).catch(() => {});
  }

  form.addEventListener("input", e => {
    if (!fields.includes(e.target) || conflict) return;
    status.textContent = "";
    clearTimeout(timer);
    timer = setTimeout(save, 2000);
  });
  form.addEventListener("focusout", () => save());
  addEventListener("pagehide", () => save(true));
  addEventListener("pageshow", e => { if (e.persisted) refresh(); });
  addEventListener("focus", refresh);
  document.addEventListener("visibilitychange", () => { if (document.hidden) save(true); else refresh(); });
});
