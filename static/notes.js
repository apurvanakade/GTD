// Notes are plain Markdown text, re-rendered with styling as you type (adapted from the Journal's editor).
// Each textarea[name=notes] (and any textarea[data-md]: the Vision card's area lines) is hidden and replaced by a
// contenteditable .md-editor that writes back into it, so the form still submits the textarea. The rendered HTML's textContent must equal the raw text exactly
// (markup only wraps, never adds or removes characters), because the caret is tracked as plain-text offsets.
// A toolbar (shown while the editor has focus) adds formatting and checklists; a checklist's boxes tick with a click.

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

const RULE = /^ {0,3}([-*_])( ?\1){2,}\s*$/;  // a horizontal rule: ---, - - -, ***

const ITEM = /^\s*(?:[-*+]|\d+[.)]) /;  // a list item, bullet, numbered or checklist

// Each non-empty line outside code is a paragraph (.p) with a gap below it, parskip-style, as in the Journal's
// editor; a list's items stay tight (.li) but for the last of a run. The "\n"s stay outside the wrappers.
function markdown(text) {
  let fence = false;
  const lines = text.split("\n");
  return lines.map((line, i) => {
    if (/^\s*(```|~~~)/.test(line)) { fence = !fence; return `<span class="md code-line">${esc(line)}</span>`; }
    if (fence) return `<span class="code-line">${esc(line)}</span>`;
    if (!line) return "";
    const tight = ITEM.test(line) && i + 1 < lines.length && ITEM.test(lines[i + 1]);
    // A line's leading spaces drawn wider (.ind), so an indented (nested) line reads as one.
    const ws = line.match(/^ */)[0], rest = line.slice(ws.length);
    const body = ws && rest ? `<span class="ind">${ws}</span>${lineHtml(rest)}` : lineHtml(line);
    return `<span class="${tight ? "li" : "p"}">${body}</span>`;
  }).join("\n");
}

function lineHtml(line) {
  let m;
  if ((m = line.match(/^(#{1,6} )(.*)/))) return `<span class="fmt h h${m[1].length - 1}">${mk(m[1], 1)}${inline(m[2])}</span>`;
  if (RULE.test(line)) return mk(line);
  // A checklist line: its [ ] or [x] drawn as a box (the characters stay, hidden), a ticked one's text struck.
  // The box may end the line ("- [ ]" just typed).
  if ((m = line.match(/^(\s*(?:[-*+]|\d+[.)]) )\[([ xX])\]( |$)(.*)/))) {
    const on = m[2] !== " ";
    return mk(m[1]) + `<span class="box${on ? " on" : ""}">[${m[2]}]</span>${m[3]}` + (on ? `<span class="ticked">${inline(m[4])}</span>` : inline(m[4]));
  }
  if ((m = line.match(/^(\s*(?:[-*+]|\d+[.)]) )(.*)/))) return mk(m[1]) + inline(m[2]);
  if ((m = line.match(/^(>+ ?)(.*)/))) return `<span class="quote">${mk(m[1])}${inline(m[2])}</span>`;
  return inline(line);
}

// --- Pasted rich text (a web page, Google Docs, Word) as Markdown ---
// Only HTML with something Markdown can carry is converted; anything else (a code editor's colored spans)
// pastes as its plain text, whitespace and all.
const RICH = "b,strong,i,em,s,del,strike,a[href],h1,h2,h3,h4,h5,h6,ul,ol,blockquote,pre,code,hr,table," +
             "[style*=font-weight],[style*=font-style],[style*=line-through]";
const BLOCK = new Set(["P", "DIV", "SECTION", "ARTICLE", "HEADER", "FOOTER", "MAIN", "ASIDE", "NAV", "FIGURE",
                       "FIGCAPTION", "DL", "DT", "DD", "TABLE", "THEAD", "TBODY", "TR", "ADDRESS", "DETAILS", "SUMMARY"]);
const SKIP = new Set(["SCRIPT", "STYLE", "HEAD", "TITLE", "META", "LINK", "TEMPLATE", "NOSCRIPT", "IMG", "SVG", "BUTTON"]);
const SAFE = /^([a-z][\w+.-]*:\/\/|mailto:)/i;  // what the renderer links (a pasted page's relative links mean nothing here)

// Marks hug their text (`**a** `, not `**a **`), or the renderer wouldn't read them.
const wrap = (s, a, b = a) => { const m = s.match(/^(\s*)([\s\S]*?)(\s*)$/); return m[2] ? m[1] + a + m[2] + b + m[3] : s; };
const KEYS = ["href", "bold", "italic", "strike", "code"];
// A line's text runs (each with its style) as Markdown: neighbours sharing a style share one mark.
function runs(segs, k = 0) {
  if (k === KEYS.length) return segs.map(s => s.text).join("");
  let out = "";
  for (let i = 0; i < segs.length; ) {
    let j = i;
    while (j < segs.length && segs[j][KEYS[k]] === segs[i][KEYS[k]]) j++;
    const group = segs.slice(i, j), v = group[0][KEYS[k]];
    if (!v) out += runs(group, k + 1);
    else if (KEYS[k] === "href") {
      const text = group.map(s => s.text).join("").replace(/[[\]]/g, "").trim();
      const url = v.replace(/[\s()]/g, c => encodeURIComponent(c));
      out += !text || text === v ? (/^[a-z][\w+.-]*:\/\//i.test(v) ? url : `[${v}](${url})`) : wrap(text, "[", `](${url})`);
    }
    else if (KEYS[k] === "code") {
      const text = group.map(s => s.text).join(""), ticks = text.includes("`") ? "``" : "`";
      out += wrap(text, ticks);
    }
    else out += wrap(runs(group, k + 1), {bold: "**", italic: "_", strike: "~~"}[KEYS[k]]);
    i = j;
  }
  return out;
}

function htmlToMarkdown(html) {
  const body = new DOMParser().parseFromString(html, "text/html").body;  // inert: nothing runs or loads
  if (!body.querySelector(RICH)) return null;
  const lines = [];
  let ctx = {quote: 0, lists: []}, lead = "", segs = [];
  const indent = () => "> ".repeat(ctx.quote) + "  ".repeat(ctx.lists.length);
  // A block's line is done: kept if it has any text; the next one starts at the current depth.
  function flush() {
    const text = runs(segs).trimEnd();
    if (text.trim()) lines.push(lead + (segs[0]?.pre ? text : text.trimStart()));
    if (text.trim() || !lead.trim()) { lead = indent(); segs = []; }  // a list item's marker waits for its text
  }
  function text(s, st) {
    if (!st.pre) {
      s = s.replace(/\s+/g, " ");
      const before = segs.length ? segs[segs.length - 1].text : " ";
      if (before.endsWith(" ")) s = s.replace(/^ /, "");
    }
    if (s) segs.push({...st, text: s});
  }
  function walk(node, st) {
    if (node.nodeType === Node.TEXT_NODE) return text(node.data, st);
    if (node.nodeType !== Node.ELEMENT_NODE || SKIP.has(node.tagName)) return;
    const tag = node.tagName, css = node.style;
    st = {...st};
    const w = css.fontWeight;
    if (/^(B|STRONG)$/.test(tag) || w === "bold" || +w >= 600) st.bold = true;
    if (w === "normal" || (+w && +w < 600)) st.bold = false;
    if (/^(I|EM)$/.test(tag) || css.fontStyle === "italic") st.italic = true;
    if (css.fontStyle === "normal") st.italic = false;
    if (/^(S|DEL|STRIKE)$/.test(tag) || css.textDecoration.includes("line-through")) st.strike = true;
    if (/^(CODE|KBD|SAMP|TT)$/.test(tag)) st.code = true;
    if (/^pre/.test(css.whiteSpace)) st.pre = true;
    if (tag === "A") { const h = node.getAttribute("href") ?? ""; st.href = SAFE.test(h) ? h : null; }
    const kids = () => node.childNodes.forEach(n => walk(n, st));
    const nest = (change, f) => { const old = ctx; ctx = {...ctx, ...change}; flush(); f(); flush(); ctx = old; lead = indent(); };
    if (tag === "BR") flush();
    else if (tag === "HR") { flush(); lines.push(indent() + "---"); }
    else if (/^H[1-6]$/.test(tag)) { flush(); lead = indent() + "#".repeat(+tag[1]) + " "; kids(); flush(); }
    else if (tag === "PRE") {
      flush();
      lines.push(indent() + "```", ...node.textContent.replace(/\n$/, "").split("\n").map(l => indent() + l), indent() + "```");
    }
    else if (tag === "BLOCKQUOTE") nest({quote: ctx.quote + 1}, kids);
    else if (tag === "UL" || tag === "OL")
      nest({lists: [...ctx.lists, {ordered: tag === "OL", n: +(node.getAttribute("start") ?? 1)}]}, kids);
    else if (tag === "LI") {
      flush();
      const list = ctx.lists[ctx.lists.length - 1] ?? {ordered: false, n: 1};
      const box = node.querySelector(":scope > input[type=checkbox], :scope > p > input[type=checkbox]");
      lead = "> ".repeat(ctx.quote) + "  ".repeat(Math.max(ctx.lists.length - 1, 0)) +
             (list.ordered ? `${list.n++}. ` : "- ") + (box ? `[${box.checked ? "x" : " "}] ` : "");
      kids();
      flush();
    }
    else if (tag === "TD" || tag === "TH") { if (node.previousElementSibling) text(" | ", {}); kids(); }
    else if (BLOCK.has(tag)) { flush(); kids(); flush(); }
    else kids();
  }
  walk(body, {});
  flush();
  return lines.join("\n");
}

// --- The toolbar ---
// The indent icons: a paragraph's lines, the middle ones set in, beside an arrow pointing in (or out).
const indentIcon = arrow => `<svg class="tool-icon" viewBox="3 4 18 16" aria-hidden="true"><path d="M3 5h18M11 10h10M11 14h10M3 19h18" stroke="currentColor" stroke-width="2"/><path d="${arrow}" fill="currentColor"/></svg>`;
const TOOLS = [["bold", "<b>B</b>", "Bold (⌘B)"], ["italic", "<i>I</i>", "Italic (⌘I)"], ["heading", "H", "Heading"],
               ["bullet", "•", "Bullet list"], ["check", "☐", "Checklist"],
               ["outdent", indentIcon("M8 8.5v7L3 12z"), "Decrease indent"],
               ["indent", indentIcon("M3 8.5v7L8 12z"), "Increase indent"], ["link", "🔗", "Link"],
               ["untick", "↺", "Untick all"]];
// A checklist box is "[ ]" or "[x]" followed by a space or the line's end.
const PREFIX = {heading: /^(\s*)#{1,6} /, bullet: /^(\s*)[-*+] (?!\[[ xX]\](?: |$))/, check: /^(\s*)[-*+] \[[ xX]\](?: |$)/};
const ADD = {heading: "# ", bullet: "- ", check: "- [ ] "};
const INDENT = "  ";  // one indent step: a nested list's, as pasted lists nest
// A line's indent (1), list marker (2) and checklist box (3), each optional.
const LIST_HEAD = /^(\s*)(?:([-*+]|\d+[.)]) (\[[ xX]\](?: |$))?)?/;
const ANY_PREFIX = /^(\s*)(?:#{1,6} |(?:[-*+]|\d+[.)]) (?:\[[ xX]\](?: |$))?)/;
const TICKED = /^\s*(?:[-*+]|\d+[.)]) \[[xX]\](?: |$)/m;
const TICKED_ALL = /^(\s*(?:[-*+]|\d+[.)]) \[)[xX](\](?: |$))/gm;

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
  const wrap = document.createElement("div"), bar = document.createElement("div");
  wrap.className = "md-wrap";
  bar.className = "md-tools";
  bar.innerHTML = TOOLS.map(([k, label, title]) => `<button type="button" data-tool="${k}" title="${title}" aria-label="${title}">${label}</button>`).join("");
  ta.after(wrap);
  wrap.append(ed, bar);
  const untick = bar.querySelector("[data-tool=untick]");
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
    untick.hidden = !TICKED.test(text);
    // Plain typing usually leaves the DOM already correct (ignoring reveal()'s class).
    if (ed.innerHTML.replace(/ md-on"/g, '"') === html) return;
    ed.innerHTML = html;
    // Markers first, then the caret: a caret placed in a still-hidden marker (just after a typed closing _)
    // would be moved by the browser to the nearest visible spot, before the marker.
    reveal(sel ?? undefined);
    if (sel) setSel(sel);
  }

  // Show hidden markers of formatting the caret touches, like Obsidian's live preview.
  function reveal(sel = getSel()) {
    const w = document.createTreeWalker(ed, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    let pos = 0;
    for (let n; (n = w.nextNode()); ) {
      if (n.nodeType === Node.TEXT_NODE) pos += n.length;
      else if (n.classList.contains("fmt"))
        n.classList.toggle("md-on", !!sel && pos <= sel[1] && pos + n.textContent.length >= sel[0]);
    }
  }
  document.addEventListener("selectionchange", () => reveal());

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

  // A toolbar edit or a box ticked: its own undo step, written back like typing.
  function apply(text, sel) {
    lastEdit = 0;
    paint(text, sel);
    record(text, sel);
    changed(text);
  }
  const selection = () => getSel() ?? [getText().length, getText().length];
  // **bold** or _italic_ around the selection, or off again when it's already wrapped; empty: the pair, caret inside.
  // Wrapped means the same run of the mark's character on both sides, of its own length or bold-italic's 3, so
  // italic inside __bold__ wraps it again (___x___) rather than eating one of bold's underscores.
  function wrapWith(mark) {
    const t = getText(), [a, b] = selection(), n = mark.length, ch = mark[0];
    let left = 0, right = 0;
    while (t[a - left - 1] === ch) left++;
    while (t[b + right] === ch) right++;
    if (left === right && (left === n || left === 3))
      return apply(t.slice(0, a - n) + t.slice(a, b) + t.slice(b + n), [a - n, b - n]);
    apply(t.slice(0, a) + mark + t.slice(a, b) + mark + t.slice(b), [a + n, b + n]);
  }
  // A heading, bullet or checklist prefix on every selected line; off again when they all have it already.
  function prefix(kind) {
    const t = getText(), [a, b] = selection();
    const start = t.lastIndexOf("\n", a - 1) + 1, end = t.indexOf("\n", b) < 0 ? t.length : t.indexOf("\n", b);
    const lines = t.slice(start, end).split("\n");
    const has = l => PREFIX[kind].test(l), bare = l => l.replace(ANY_PREFIX, "");
    const off = lines.every(has);
    const out = lines.map(l => off ? l.replace(PREFIX[kind], "$1") : l.match(/^\s*/)[0] + ADD[kind] + bare(l).trimStart());
    const block = out.join("\n"), text = t.slice(0, start) + block + t.slice(end);
    // One line: the caret keeps its place in the text; several: they stay selected.
    const sel = lines.length === 1 ? [Math.max(start, a + block.length - (end - start)), Math.max(start, b + block.length - (end - start))]
                                   : [start, start + block.length];
    apply(text, sel);
  }
  // Swaps the head of every selected line: f(line) gives [old head's length, new head], or null to leave the line
  // be (blank lines in a several-line selection always are). The selection keeps its place in the text: an offset
  // after a line's head moves with it, one inside it stays put but within the new head.
  function heads(f) {
    const t = getText(), [a, b] = selection();
    const start = t.lastIndexOf("\n", a - 1) + 1, end = t.indexOf("\n", b) < 0 ? t.length : t.indexOf("\n", b);
    const lines = t.slice(start, end).split("\n"), swaps = [];
    let pos = start, same = true;
    const out = lines.map(l => {
      const h = lines.length > 1 && !l.trim() ? null : f(l), at = pos;
      pos += l.length + 1;
      if (!h || l.slice(0, h[0]) === h[1]) return l;
      same = false;
      swaps.push([at, h[0], h[1].length]);
      return h[1] + l.slice(h[0]);
    });
    if (same) return;
    const map = x => swaps.reduce((y, [s, was, now]) => x < s ? y : y + (x - s >= was ? now - was : Math.min(x - s, now) - (x - s)), x);
    apply(t.slice(0, start) + out.join("\n") + t.slice(end), [map(a), map(b)]);
  }
  // Every selected line one INDENT step in (d = 1) or out (d = -1, as much of a step as it has).
  const indent = d => heads(l => {
    const n = Math.min(INDENT.length, l.match(/^ */)[0].length);
    return d > 0 ? [0, INDENT] : [n, ""];
  });
  // ⌘/Ctrl-Enter, as in Obsidian: plain → bullet → checklist item → ticked → plain. Every selected line takes the
  // state after the first one's, so a mixed selection ends up alike; a numbered item keeps its number.
  const STATES = ["", "", "[ ] ", "[x] "];  // plain, list, box, ticked (by index)
  const stateOf = m => !m[2] ? 0 : !m[3] ? 1 : m[3][1] === " " ? 2 : 3;
  function cycle() {
    const t = getText(), [a, b] = selection();
    const start = t.lastIndexOf("\n", a - 1) + 1, end = t.indexOf("\n", b) < 0 ? t.length : t.indexOf("\n", b);
    const first = t.slice(start, end).split("\n").find(l => l.trim()) ?? "";
    const next = (stateOf(first.match(LIST_HEAD)) + 1) % 4;
    heads(l => {
      const m = l.match(LIST_HEAD);
      return [m[0].length, next ? m[1] + (m[2] ?? "-") + " " + STATES[next] : m[1]];
    });
  }
  function link() {
    const t = getText(), [a, b] = selection(), label = t.slice(a, b);
    const caret = label ? a + label.length + 3 : a + 1;  // in the (url) for a selection, else in the [text]
    apply(t.slice(0, a) + `[${label}]()` + t.slice(b), [caret, caret]);
  }
  const TOOL = {bold: () => wrapWith("**"), italic: () => wrapWith("_"), heading: () => prefix("heading"),
                bullet: () => prefix("bullet"), check: () => prefix("check"), link,
                indent: () => indent(1), outdent: () => indent(-1),
                untick: () => apply(getText().replace(TICKED_ALL, "$1 $2"), getSel())};
  bar.addEventListener("mousedown", e => e.preventDefault());  // keeps the caret (and the focus) in the editor
  bar.addEventListener("click", e => {
    const b = e.target.closest("button");
    if (!b) return;
    if (!getSel()) { ed.focus(); setSel(selection()); }
    TOOL[b.dataset.tool]();
  });

  // A click on a checklist box ticks or unticks it, without moving the caret or taking the focus.
  ed.addEventListener("mousedown", e => { if (e.target.closest(".box")) e.preventDefault(); });
  ed.addEventListener("click", e => {
    const box = e.target.closest(".box");
    if (!box) return;
    e.preventDefault();
    const t = getText(), i = offsetOf(box, 0) + 1;
    apply(t.slice(0, i) + (t[i] === " " ? "x" : " ") + t.slice(i + 1), getSel());
  });

  // Enter in a list continues it (a new checklist item starts unticked); Enter on an empty item ends the list. A
  // rule (- - -) isn't a list. Kept in sync with Journal's editor.js (which also runs its typing assists here).
  function newline() {
    const t = getText(), [a, b] = selection();
    const start = t.lastIndexOf("\n", a - 1) + 1, line = t.slice(start, a);
    const m = a === b && !RULE.test(line) && line.match(/^(\s*)(?:([-*+])|(\d+)([.)])) (\[[ xX]\](?: |$))?/);
    if (!m) return insert("\n");
    if (line.length === m[0].length && (t[a] ?? "\n") === "\n")  // nothing after the prefix: the list ends here
      return apply(t.slice(0, start) + t.slice(a), [start, start]);
    const next = m[1] + (m[2] ?? `${+m[3] + 1}${m[4]}`) + " " + (m[5] ? "[ ] " : "");
    insert("\n" + next);
  }

  let composing = false;
  ed.addEventListener("compositionstart", () => { composing = true; });
  ed.addEventListener("compositionend", () => { composing = false; edited(); });
  ed.addEventListener("input", () => { if (!composing) edited(); });
  // Left changed: the textarea's change event, as a textarea of its own would fire (app.js saves a
  // form[data-background] on it).
  let focusText = start;
  ed.addEventListener("focus", () => { focusText = ta.value; });
  ed.addEventListener("blur", () => {
    if (ta.value !== focusText) ta.dispatchEvent(new Event("change", { bubbles: true }));
  });
  ed.addEventListener("beforeinput", e => {
    const t = e.inputType;
    if (t === "historyUndo" || t === "historyRedo") { e.preventDefault(); step(t === "historyUndo" ? -1 : 1); }
    else if (t === "insertParagraph") { e.preventDefault(); newline(); }
    else if (t === "insertLineBreak") { e.preventDefault(); insert("\n"); }
    else if (t === "insertFromPaste" || t === "insertFromDrop") {
      // Rich text comes in as Markdown (the editor holds only text); plain text as it is.
      e.preventDefault();
      const dt = e.dataTransfer, html = dt?.getData("text/html");
      insert((html ? htmlToMarkdown(html) : null) ?? (dt?.getData("text/plain") ?? "").replace(/\r\n?/g, "\n"));
    }
  });
  // Copying or cutting gives the Markdown itself, not the rendered HTML (which pasted back would double its marks).
  function copy(e) {
    const sel = getSel();
    if (!sel || sel[0] === sel[1]) return;
    e.preventDefault();
    e.clipboardData.setData("text/plain", getText().slice(...sel));
    if (e.type === "cut") { const t = getText(); apply(t.slice(0, sel[0]) + t.slice(sel[1]), [sel[0], sel[0]]); }
  }
  ed.addEventListener("copy", copy);
  ed.addEventListener("cut", copy);
  ed.addEventListener("dragstart", e => {
    const sel = getSel();
    if (sel) { e.dataTransfer.clearData(); e.dataTransfer.setData("text/plain", getText().slice(...sel)); }
  });
  ed.addEventListener("keydown", e => {
    const k = e.key.toLowerCase();
    if ((e.metaKey || e.ctrlKey) && k === "z") { e.preventDefault(); step(e.shiftKey ? 1 : -1); }
    else if (e.ctrlKey && k === "y") { e.preventDefault(); step(1); }
    else if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !e.altKey && !e.shiftKey) { e.preventDefault(); cycle(); }
    else if ((e.metaKey || e.ctrlKey) && (k === "b" || k === "i")) { e.preventDefault(); TOOL[k === "b" ? "bold" : "italic"](); }
    else if (e.key === "Escape") ed.blur();
  });

  // Links: ⌘/Ctrl-click to open (a plain tap on touch screens); a plain click just places the caret.
  ed.addEventListener("click", e => {
    const a = e.target.closest("a");
    if (!a || !(e.metaKey || e.ctrlKey || matchMedia("(pointer: coarse)").matches)) return;
    e.preventDefault();
    const path = recordOf(a.href);  // app.js: a record opens in its overlay, another page of the app in its place
    if (path && top.overlays) top.overlays.open(path);
    else if (a.getAttribute("href").startsWith("/")) top.location.href = a.href;
    else window.open(a.href, "_blank", "noopener");
  });

  // Text loaded from elsewhere (autosave.js: someone else's edit, or a merge); undoable like an edit.
  ta.setText = text => {
    const sel = document.activeElement === ed ? [text.length, text.length] : null;
    paint(text, sel);
    record(text, sel ?? [text.length, text.length]);
    changed(text);
  };

  // An add form reset after adding (the capture dialog, the popup): the editor empties with its textarea.
  ta.form?.addEventListener("reset", () => setTimeout(() => {
    paint(ta.value);
    hist.length = 0;  // a new task: ⌘Z mustn't bring back the last one's notes
    hist.push({ text: ta.value, sel: [ta.value.length, ta.value.length] });
    hi = 0;
  }));

  paint(start);
}

document.querySelectorAll("textarea[name=notes], textarea[data-md]").forEach(editor);
const mod = e => document.body.classList.toggle("mod", e.metaKey || e.ctrlKey);
addEventListener("keydown", mod);
addEventListener("keyup", mod);
addEventListener("blur", () => document.body.classList.remove("mod"));
