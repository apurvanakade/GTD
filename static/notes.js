// Notes are plain Markdown text, re-rendered with styling as you type (adapted from the Journal's editor).
// Each textarea[name=notes] is hidden and replaced by a contenteditable .md-editor that writes back into it,
// so the form still submits the textarea. The rendered HTML's textContent must equal the raw text exactly
// (markup only wraps, never adds or removes characters), because the caret is tracked as plain-text offsets.

// --- Markdown highlighting ---
const esc = s => s.replace(/[&<>]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);
// Markers; `hide` ones are only shown while the caret touches their .fmt element (see reveal()).
const mk = (s, hide) => `<span class="md${hide ? " hide" : ""}">${esc(s)}</span>`;
// Any scheme://, so obsidian:// links work too; never javascript: and the like.
const link = (href, text) =>
  /^([a-z][\w+.-]*:\/\/|mailto:|\/)/i.test(href)
    ? `<a href="${esc(href).replace(/"/g, "&quot;")}">${esc(text)}</a>`
    : esc(text);

const INLINE = new RegExp([
  /(`+)(.+?)\1/,                                    // 1,2 code
  /\[([^\]\n]+)\]\(([^()\s]+)\)/,                   // 3,4 [text](url)
  /([a-z][\w+.-]*:\/\/[^\s<>]*[^\s<>.,;:!?'")\]])/, // 5 bare url
  /(?<!\w)(\*\*|__)(?=\S)(.+?)(?<=\S)\6(?!\w)/,      // 6,7 bold
  /(?<!\w)(\*|_)(?=\S)(.+?)(?<=\S)\8(?!\w)/,         // 8,9 italic
  /~~(?=\S)(.+?)(?<=\S)~~/,                         // 10 strikethrough
].map(r => r.source).join("|"), "gi");

function inline(s) {
  let out = "", i = 0;
  for (const m of s.matchAll(INLINE)) {
    out += esc(s.slice(i, m.index));
    i = m.index + m[0].length;
    if (m[1]) out += `<code class="fmt">${mk(m[1], 1)}${esc(m[2])}${mk(m[1], 1)}</code>`;
    else if (m[3]) out += `<span class="fmt">${mk("[", 1)}${link(m[4], m[3])}${mk(`](${m[4]})`, 1)}</span>`;
    else if (m[5]) out += link(m[5], m[5]);
    else if (m[6]) out += `<b class="fmt">${mk(m[6], 1)}${inline(m[7])}${mk(m[6], 1)}</b>`;
    else if (m[8]) out += `<i class="fmt">${mk(m[8], 1)}${inline(m[9])}${mk(m[8], 1)}</i>`;
    else out += `<s class="fmt">${mk("~~", 1)}${inline(m[10])}${mk("~~", 1)}</s>`;
  }
  return out + esc(s.slice(i));
}

function markdown(text) {
  let fence = false;
  return text.split("\n").map(line => {
    let m;
    if (/^\s*(```|~~~)/.test(line)) { fence = !fence; return `<span class="md code-line">${esc(line)}</span>`; }
    if (fence) return `<span class="code-line">${esc(line)}</span>`;
    if ((m = line.match(/^(#{1,6} )(.*)/))) return `<span class="fmt h h${m[1].length - 1}">${mk(m[1], 1)}${inline(m[2])}</span>`;
    if (/^ {0,3}([-*_])( ?\1){2,}\s*$/.test(line)) return mk(line);
    if ((m = line.match(/^(\s*(?:[-*+]|\d+[.)]) (?:\[[ xX]\] )?)(.*)/))) return mk(m[1]) + inline(m[2]);
    if ((m = line.match(/^(>+ ?)(.*)/))) return `<span class="quote">${mk(m[1])}${inline(m[2])}</span>`;
    return inline(line);
  }).join("\n");
}

// --- One editor per notes textarea ---
function editor(ta) {
  const ed = document.createElement("div");
  ed.className = "md-editor";
  ed.contentEditable = "true";
  ed.dataset.placeholder = ta.placeholder;
  ed.style.minHeight = `${(ta.rows || 2) * 1.5 + 1}em`;
  ed.setAttribute("role", "textbox");
  ed.setAttribute("aria-multiline", "true");
  ed.setAttribute("aria-label", ta.getAttribute("aria-label") || "Notes");
  ta.hidden = true;
  ta.after(ed);
  const getText = () => ed.textContent;

  function offsetOf(node, off) {
    const r = document.createRange();
    r.selectNodeContents(ed);
    r.setEnd(node, off);
    return r.toString().length;
  }
  function getSel() {
    const s = getSelection();
    if (!s.rangeCount || !ed.contains(s.anchorNode)) return null;
    const r = s.getRangeAt(0);
    return [offsetOf(r.startContainer, r.startOffset), offsetOf(r.endContainer, r.endOffset)];
  }
  function pointAt(n) {
    const w = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT);
    for (let node; (node = w.nextNode()); n -= node.length) if (n <= node.length) return [node, n];
    return [ed, 0];
  }
  function setSel([a, b]) {
    const r = document.createRange();
    r.setStart(...pointAt(a));
    r.setEnd(...pointAt(b));
    getSelection().removeAllRanges();
    getSelection().addRange(r);
  }

  // Trailing <br> keeps a final empty line visible; it isn't part of textContent.
  function paint(text, sel) {
    const html = markdown(text) + "<br>";
    ed.classList.toggle("empty", !text);
    // Plain typing usually leaves the DOM already correct (ignoring reveal()'s class).
    if (ed.innerHTML.replace(/ md-on"/g, '"') === html) return;
    ed.innerHTML = html;
    if (sel) setSel(sel);
    reveal();
  }

  // Show hidden markers of formatting the caret touches, like Obsidian's live preview.
  function reveal() {
    const sel = getSel();
    const w = document.createTreeWalker(ed, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    let pos = 0;
    for (let n; (n = w.nextNode()); ) {
      if (n.nodeType === Node.TEXT_NODE) pos += n.length;
      else if (n.classList.contains("fmt"))
        n.classList.toggle("md-on", !!sel && pos <= sel[1] && pos + n.textContent.length >= sel[0]);
    }
  }
  document.addEventListener("selectionchange", reveal);

  // Undo history (re-rendering clears the browser's own).
  const start = ta.value.replace(/\r\n?/g, "\n");
  const hist = [{ text: start, sel: [start.length, start.length] }];
  let hi = 0, lastEdit = 0, groupStart = 0;
  function record(text, sel) {
    if (text === hist[hi].text) return;
    hist.length = hi + 1;
    const now = Date.now();
    // Merge keystrokes into one undo step until a pause or ~2s of continuous typing.
    if (hi > 0 && now - lastEdit < 1000 && now - groupStart < 2000) hist[hi] = { text, sel };
    else { hist.push({ text, sel }); hi++; groupStart = now; }
    if (hist.length > 500) { hist.shift(); hi--; }
    lastEdit = now;
  }
  // Only an edit writes the textarea, so saving untouched notes sends them back unchanged.
  // The input event tells autosave.js.
  function changed(text) { ta.value = text; ta.dispatchEvent(new Event("input", { bubbles: true })); }
  function step(d) {
    if (!hist[hi + d]) return;
    hi += d;
    lastEdit = 0;
    paint(hist[hi].text, hist[hi].sel);
    changed(hist[hi].text);
  }
  function edited() {
    const text = getText(), sel = getSel();
    paint(text, sel);
    record(text, sel);
    changed(text);
  }
  function insert(s) {
    const t = getText(), [a, b] = getSel() ?? [t.length, t.length];
    const text = t.slice(0, a) + s + t.slice(b);
    paint(text, [a + s.length, a + s.length]);
    record(text, [a + s.length, a + s.length]);
    changed(text);
  }

  let composing = false;
  ed.addEventListener("compositionstart", () => { composing = true; });
  ed.addEventListener("compositionend", () => { composing = false; edited(); });
  ed.addEventListener("input", () => { if (!composing) edited(); });
  ed.addEventListener("beforeinput", e => {
    const t = e.inputType;
    if (t === "historyUndo" || t === "historyRedo") { e.preventDefault(); step(t === "historyUndo" ? -1 : 1); }
    else if (t === "insertParagraph" || t === "insertLineBreak") { e.preventDefault(); insert("\n"); }
    else if (t === "insertFromPaste" || t === "insertFromDrop") {
      // Paste plain text only; pasted HTML would bring markup the renderer didn't make.
      e.preventDefault();
      insert((e.dataTransfer?.getData("text/plain") ?? "").replace(/\r\n?/g, "\n"));
    }
  });
  ed.addEventListener("keydown", e => {
    const k = e.key.toLowerCase();
    if ((e.metaKey || e.ctrlKey) && k === "z") { e.preventDefault(); step(e.shiftKey ? 1 : -1); }
    else if (e.ctrlKey && k === "y") { e.preventDefault(); step(1); }
    else if (e.key === "Escape") ed.blur();
  });

  // Links: ⌘/Ctrl-click to open (a plain tap on touch screens); a plain click just places the caret.
  ed.addEventListener("click", e => {
    const a = e.target.closest("a");
    if (!a || !(e.metaKey || e.ctrlKey || matchMedia("(pointer: coarse)").matches)) return;
    e.preventDefault();
    if (a.getAttribute("href").startsWith("/")) location.href = a.href;
    else window.open(a.href, "_blank", "noopener");
  });

  // Text loaded from elsewhere (autosave.js: someone else's edit, or a merge); undoable like an edit.
  ta.setText = text => {
    const sel = document.activeElement === ed ? [text.length, text.length] : null;
    paint(text, sel);
    record(text, sel ?? [text.length, text.length]);
    changed(text);
  };

  paint(start);
}

document.querySelectorAll("textarea[name=notes]").forEach(editor);
const mod = e => document.body.classList.toggle("mod", e.metaKey || e.ctrlKey);
addEventListener("keydown", mod);
addEventListener("keyup", mod);
addEventListener("blur", () => document.body.classList.remove("mod"));
