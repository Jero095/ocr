const $ = (id) => document.getElementById(id);

const state = { statements: [], activeId: null, user: null, editing: null, dirty: false };

// Every request goes through here so an expired session lands on the login page
// instead of failing silently. The gate in app/auth.py answers /api/* with 401
// JSON rather than an HTML redirect precisely so this check is possible.
async function api(url, options) {
  const res = await fetch(url, options);
  if (res.status === 401) {
    location.href = "/login";
    throw new Error("signed out");
  }
  return res;
}

/* ---------- helpers ---------- */

function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 1600);
}

async function copy(text, label) {
  try {
    await navigator.clipboard.writeText(text);
    toast(`${label} copied`);
    return true;
  } catch {
    // Clipboard API needs a secure context; fall back to a hidden textarea.
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    toast(ok ? `${label} copied` : "Copy failed - select manually");
    return ok;
  }
}

const isNumeric = (v) => /^\(?-?[\d,]+(\.\d+)?%?\)?$/.test(v.trim()) && /\d/.test(v);
const isNegative = (v) => /^\(.*\)$/.test(v.trim()) || v.trim().startsWith("-");

const active = () => state.statements.find((s) => s.id === state.activeId);

/* ---------- upload ---------- */

async function upload(files) {
  for (const file of files) {
    if (!/\.(pdf|csv|tsv|txt)$/i.test(file.name)) {
      toast(`${file.name} is not a PDF, CSV or TSV`);
      continue;
    }

    const placeholder = { id: `tmp-${Date.now()}`, filename: file.name, pending: true };
    state.statements.push(placeholder);
    renderList();

    const body = new FormData();
    body.append("file", file);

    try {
      const res = await api("/api/statements", { method: "POST", body });
      const data = await res.json();
      const i = state.statements.indexOf(placeholder);
      if (!res.ok) {
        state.statements.splice(i, 1);
        toast(data.detail || `Could not read ${file.name}`);
      } else {
        state.statements[i] = data;
        state.activeId = data.id;
      }
    } catch {
      state.statements.splice(state.statements.indexOf(placeholder), 1);
      toast("Upload failed - is the server running?");
    }
    renderList();
    renderViewer();
  }
}

/* ---------- sidebar ---------- */

function renderList() {
  const list = $("list");
  list.innerHTML = "";

  for (const s of state.statements) {
    const row = document.createElement("div");
    row.className = "item" + (s.id === state.activeId ? " active" : "");

    const dot = document.createElement("span");
    if (s.pending) dot.className = "dot pending";
    else if (s.payout?.status === "mismatch") {
      dot.className = "dot bad";
    } else if (s.edited) {
      // Edited but not failed. Red would read as "this is wrong"; an edit is a
      // deliberate act, it just means the table is no longer purely extracted.
      dot.className = "dot edited";
      dot.title = "Edited after extraction";
    } else {
      const ok = s.payout?.status === "match" || (s.checks?.length && s.checks.every((c) => c.ok));
      dot.className = "dot " + (ok && !s.warnings?.length ? "ok" : "bad");
    }

    const name = document.createElement("span");
    name.className = "item-name";
    name.textContent = s.filename;
    name.title = s.filename;

    const meta = document.createElement("span");
    meta.className = "item-meta";
    meta.textContent = s.pending ? "…" : `${s.rows.length}`;

    row.append(dot, name, meta);

    if (!s.pending) {
      row.onclick = () => {
        state.activeId = s.id;
        renderList();
        renderViewer();
      };

      const x = document.createElement("button");
      x.className = "item-x";
      x.textContent = "×";
      x.title = "Remove";
      x.onclick = async (e) => {
        e.stopPropagation();
        await api(`/api/statements/${s.id}`, { method: "DELETE" });
        state.statements = state.statements.filter((v) => v.id !== s.id);
        if (state.activeId === s.id) state.activeId = state.statements.at(-1)?.id ?? null;
        renderList();
        renderViewer();
      };
      row.append(x);
    }

    list.append(row);
  }

  $("export-all").hidden = state.statements.filter((s) => !s.pending).length < 2;
}

/* ---------- main view ---------- */

function renderViewer() {
  const s = active();
  $("empty").hidden = !!s;
  $("viewer").hidden = !s;
  if (!s) return;

  $("title").textContent = s.filename;
  const edited = s.edited
    ? ` · edited${s.edited_by ? ` by ${s.edited_by}` : ""}`
    : "";
  $("carrier").textContent = (s.template
    ? `${s.template} · ${s.rows.length} row${s.rows.length === 1 ? "" : "s"}`
    : "Layout not recognised") + edited;

  $("badges").innerHTML = "";
  for (const c of s.checks) {
    const b = document.createElement("span");
    b.className = "badge " + (c.ok ? "ok" : "bad");
    b.textContent = c.ok
      ? `${c.label} ${c.stated_total.toFixed(2)} ✓`
      : `${c.label} ${c.rows_total.toFixed(2)} ≠ ${c.stated_total.toFixed(2)}`;
    $("badges").append(b);
  }

  renderFailsafe(s);
  renderCleanup(s);

  const warn = $("warnings");
  warn.hidden = !s.warnings.length;
  if (s.warnings.length) {
    warn.innerHTML =
      "<strong>Needs review</strong><ul>" +
      s.warnings.map((w) => `<li>${w}</li>`).join("") +
      "</ul>";
  }

  renderTable(s);
  renderEditBar();
  openPdfViewer(s).catch(() => {
    // Falling back to the plain viewer is better than an empty pane: the user
    // loses region selection, not the ability to look at the document.
    $("pdf-canvas-wrap").hidden = true;
    $("pdf").hidden = false;
    $("pdf").src = `/api/statements/${s.id}/pdf`;
    $("pick-region").hidden = true;
  });
  $("table-hint").textContent =
    state.editing === s.id
      ? "Click a cell to edit it. Changes are saved only when you press Save."
      : "Click any cell to copy it. Click a column header to copy the whole column.";
}

// Pinned to en-US: the statements are US-dollar documents and the table below
// shows the raw values, so a locale-dependent separator would contradict both.
// Null-safe: the view object in renderFailsafe builds every branch's text before
// picking one by status, so money() is called on exported_total even for the
// statuses that have none (an image-only scan extracts no amounts). Returning a
// dash instead of throwing keeps the rest of renderViewer alive - when this threw,
// the previous statement's table and pass banner stayed on screen under the new
// statement's name, which is the most misleading thing this UI could do.
const money = (v) =>
  typeof v === "number" && Number.isFinite(v)
    ? v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : "—";

// The failsafe: do the commission amounts headed for Excel equal the total the
// statement itself declares? Rendered above the table because a mismatch means
// the export must not be trusted, and export is disabled while it fails.
function renderFailsafe(s) {
  const el = $("failsafe");
  const p = s.payout;
  if (!p) {
    el.hidden = true;
    setExportEnabled(true);
    return;
  }
  el.hidden = false;

  const refs = p.references
    .map((r) => `${r.source} <b>${money(r.amount)}</b>`)
    .join(" · ");

  const view = {
    match: {
      cls: "pass",
      icon: "✓",
      title: `Failsafe passed — exports ${money(p.exported_total)}`,
      detail: `Sum of <b>${p.commission_column}</b> across ${s.rows.length} row${
        s.rows.length === 1 ? "" : "s"
      } matches ${refs}.`,
    },
    mismatch: {
      cls: "fail",
      icon: "✕",
      title: `Failsafe FAILED — exports ${money(p.exported_total)}`,
      detail:
        `Sum of <b>${p.commission_column}</b> does not match ${refs}.` +
        (p.references_disagree
          ? " The references also disagree with each other."
          : "") +
        " Export is blocked until this is explained.",
    },
    no_reference: {
      cls: "unknown",
      icon: "?",
      title: "Failsafe could not run — no declared total",
      detail:
        "Nothing to reconcile against: no amount in the filename and no labelled" +
        " total in the statement text.",
    },
    no_amounts: {
      cls: "unknown",
      icon: "?",
      title: "Failsafe could not run — no amounts to check",
      detail: p.references.length
        ? `The statement declares ${refs}, but no commission column was extracted.`
        : "No commission column was extracted and no declared total was found.",
    },
  }[p.status];

  // The verdict is a fact about the extraction and never changes once measured.
  // But its wording says "exports N", and after an edit that figure is no longer
  // what leaving the app would carry - so say so rather than let the banner
  // imply a number it stopped governing.
  const drifted =
    typeof s.edited_total === "number" &&
    typeof p.exported_total === "number" &&
    Math.abs(s.edited_total - p.exported_total) > 0.01;

  el.className = `failsafe ${view.cls}${drifted ? " drifted" : ""}`;
  el.innerHTML =
    `<span class="failsafe-icon">${view.icon}</span>` +
    `<span class="failsafe-body">` +
    `<div class="failsafe-title">${
      drifted
        ? `${view.title.replace(/exports/, "checked")} — now edited`
        : view.title
    }</div>` +
    `<div class="failsafe-detail">${view.detail}${
      drifted
        ? ` <b>Your edits changed the table: it now totals ${money(
            s.edited_total,
          )}, which is what an export would carry.</b>`
        : ""
    }</div>` +
    `</span>`;

  setExportEnabled(p.status !== "mismatch");
}

// What the CSV cleaner changed, and what it deliberately did not.
function renderCleanup(s) {
  const el = $("cleanup");
  const notes = s.cleanup || [];
  if (!notes.length) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  const where = s.delimiter ? ` · ${s.delimiter}-delimited` : "";
  el.innerHTML =
    `<div class="cleanup-head">Cleanup applied${where}</div><ul>` +
    notes
      .map(
        (n) =>
          `<li><span class="tag ${n.action}">${n.action.replace(/-/g, " ")}</span>` +
          `<span><span class="col">${n.column}</span> <span class="why">${n.detail}</span></span></li>`
      )
      .join("") +
    "</ul>";
}

function setExportEnabled(enabled) {
  for (const id of ["copy-tsv", "download-xlsx", "download"]) {
    $(id).disabled = !enabled;
    $(id).title = enabled ? "" : "Blocked: the failsafe check failed";
  }
}

function renderTable(s) {
  const table = $("table");
  table.innerHTML = "";

  // An image-only scan or an unrecognised layout yields no columns at all. Say so
  // rather than leaving an empty <table>, which reads as "extracted nothing"
  // indistinguishably from "still loading".
  if (!s.columns.length) {
    const tr = table.insertRow();
    const td = tr.insertCell();
    td.className = "table-empty";
    td.textContent = s.warnings.length
      ? "Nothing extracted — see “Needs review” above."
      : "Nothing extracted from this file.";
    return;
  }

  const editing = state.editing === s.id;

  const thead = table.createTHead();
  const hr = thead.insertRow();
  s.columns.forEach((col, i) => {
    const th = document.createElement("th");

    if (!editing) {
      th.textContent = col;
      th.title = `Copy the ${col} column`;
      th.onclick = () => {
        const vals = s.rows.map((r) => r[col] ?? "");
        copy(vals.join("\n"), col);
      };
      hr.append(th);
      return;
    }

    // Editing: the header is renamed in place and carries its own controls.
    const name = document.createElement("span");
    name.className = "col-name";
    name.contentEditable = "true";
    name.spellcheck = false;
    name.textContent = col;
    name.onblur = () => renameColumn(i, name.textContent.trim() || col);
    name.onkeydown = (e) => {
      if (e.key === "Enter") { e.preventDefault(); name.blur(); }
      if (e.key === "Escape") { name.textContent = col; name.blur(); }
    };

    const tools = document.createElement("span");
    tools.className = "col-tools";
    const button = (label, title, fn, disabled) => {
      const b = document.createElement("button");
      b.textContent = label;
      b.title = title;
      b.disabled = !!disabled;
      b.onclick = (e) => { e.stopPropagation(); fn(); };
      return b;
    };
    tools.append(
      button("‹", "Move left", () => moveColumn(i, -1), i === 0),
      button("›", "Move right", () => moveColumn(i, 1), i === s.columns.length - 1),
      button("×", `Delete the ${col} column`, () => deleteColumn(i)),
    );

    th.append(name, tools);
    hr.append(th);
  });

  if (editing) {
    const th = document.createElement("th");
    th.className = "row-tools-head";
    hr.append(th);
  }

  const tbody = table.createTBody();
  const addRow = (data, cls, rowIndex) => {
    const tr = tbody.insertRow();
    if (cls) tr.className = cls;
    for (const col of s.columns) {
      const td = tr.insertCell();
      const val = data[col] ?? "";
      td.textContent = val;
      if (isNumeric(val)) {
        td.classList.add("num");
        if (isNegative(val)) td.classList.add("neg");
      }

      if (editing && rowIndex !== undefined) {
        td.contentEditable = "true";
        td.spellcheck = false;
        td.classList.add("editable");
        td.onblur = () => editCell(rowIndex, col, td.textContent.trim());
        td.onkeydown = (e) => {
          if (e.key === "Enter") { e.preventDefault(); td.blur(); }
          if (e.key === "Escape") { td.textContent = val; td.blur(); }
        };
      } else if (val && !editing) {
        td.onclick = async () => {
          if (await copy(val, col)) {
            td.classList.add("flash");
            setTimeout(() => td.classList.remove("flash"), 450);
          }
        };
      }
    }

    if (editing) {
      const td = tr.insertCell();
      td.className = "row-tools";
      if (rowIndex !== undefined) {
        const b = document.createElement("button");
        b.textContent = "×";
        b.title = "Delete this row";
        b.onclick = () => deleteRow(rowIndex);
        td.append(b);
      }
    }
  };

  s.rows.forEach((r, i) => addRow(r, null, i));
  // The totals row is rendered but never editable: it is the statement's own
  // figure, and the per-column checks compare the rows against it. Letting it
  // be typed over would turn the one internal reference into another thing the
  // user can accidentally change.
  if (s.totals && Object.keys(s.totals).length) addRow(s.totals, "totals");
}

/* ---------- editing ---------- */

// Edits mutate the loaded statement and are saved explicitly. The failsafe
// verdict is never recomputed here or on the server - see _apply_edit in
// app/main.py for why.

function markDirty() {
  state.dirty = true;
  renderEditBar();
}

function editCell(rowIndex, col, value) {
  const s = active();
  if (!s || (s.rows[rowIndex]?.[col] ?? "") === value) return;
  s.rows[rowIndex][col] = value;
  markDirty();
}

function renameColumn(index, name) {
  const s = active();
  const old = s.columns[index];
  if (!s || old === name || s.columns.includes(name)) return;
  s.columns[index] = name;
  for (const row of s.rows) {
    row[name] = row[old] ?? "";
    delete row[old];
  }
  if (s.totals && old in s.totals) {
    s.totals[name] = s.totals[old];
    delete s.totals[old];
  }
  markDirty();
  renderTable(s);
}

function moveColumn(index, delta) {
  const s = active();
  const to = index + delta;
  if (!s || to < 0 || to >= s.columns.length) return;
  const [col] = s.columns.splice(index, 1);
  s.columns.splice(to, 0, col);
  markDirty();
  renderTable(s);
}

function deleteColumn(index) {
  const s = active();
  if (!s) return;
  // Deleting the commission column is allowed, but it is worth a confirmation:
  // the export then carries no reconciled figure at all.
  const col = s.columns[index];
  if (col === s.payout?.commission_column &&
      !confirm(`"${col}" is the column the failsafe checked. Delete it anyway?`)) {
    return;
  }
  s.columns.splice(index, 1);
  markDirty();
  renderTable(s);
}

function deleteRow(index) {
  const s = active();
  if (!s) return;
  s.rows.splice(index, 1);
  markDirty();
  renderTable(s);
}

function renderEditBar() {
  const s = active();
  const bar = $("editbar");
  const editing = s && state.editing === s.id;
  bar.hidden = !editing;
  $("edit-table").textContent = editing ? "Done" : "Edit table";
  if (!editing) return;
  $("save-edits").disabled = !state.dirty;
  $("save-edits").textContent = state.dirty ? "Save changes" : "Saved";
  $("revert-edits").hidden = !s.edited;
}

async function saveEdits() {
  const s = active();
  if (!s) return;
  $("save-edits").disabled = true;
  $("save-edits").textContent = "Saving…";
  try {
    const res = await api(`/api/statements/${s.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ columns: s.columns, rows: s.rows }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      toast(data.detail || "Could not save");
      renderEditBar();
      return;
    }
    const saved = await res.json();
    const i = state.statements.findIndex((v) => v.id === s.id);
    state.statements[i] = saved;
    state.dirty = false;
    toast("Changes saved");
    renderList();
    renderViewer();
  } catch {
    toast("Save failed");
    renderEditBar();
  }
}

async function revertEdits() {
  const s = active();
  if (!s || !confirm("Discard all edits and restore the table as extracted?")) return;
  const res = await api(`/api/statements/${s.id}/revert`, { method: "POST" });
  if (!res.ok) { toast("Could not revert"); return; }
  const restored = await res.json();
  const i = state.statements.findIndex((v) => v.id === s.id);
  state.statements[i] = restored;
  state.dirty = false;
  toast("Restored as extracted");
  renderList();
  renderViewer();
}

function toggleEdit() {
  const s = active();
  if (!s) return;
  if (state.editing === s.id) {
    if (state.dirty && !confirm("You have unsaved changes. Leave edit mode?")) return;
    state.editing = null;
    state.dirty = false;
  } else {
    state.editing = s.id;
    state.dirty = false;
  }
  renderViewer();
}

/* ---------- wiring ---------- */

$("file").onchange = (e) => {
  upload([...e.target.files]);
  e.target.value = "";
};

const drop = $("drop");
["dragenter", "dragover"].forEach((ev) =>
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.add("over");
  })
);
["dragleave", "drop"].forEach((ev) =>
  drop.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.remove("over");
  })
);
drop.addEventListener("drop", (e) => upload([...e.dataTransfer.files]));
drop.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    $("file").click();
  }
});

$("copy-tsv").onclick = () => {
  const s = active();
  if (s) copy(s.tsv, "Table");
};

$("download").onclick = () => {
  const s = active();
  if (s) location.href = `/api/statements/${s.id}/tsv`;
};

$("download-xlsx").onclick = () => {
  const s = active();
  if (s) location.href = `/api/statements/${s.id}/xlsx`;
};

$("export-all").onclick = () => {
  const failing = state.statements.filter((s) => s.payout?.status === "mismatch");
  if (failing.length) {
    toast(
      `${failing.length} statement${failing.length === 1 ? "" : "s"} failed the failsafe - remove or fix first`
    );
    return;
  }
  location.href = `/api/export.xlsx?ids=${workingSetIds()}`;
};

$("toggle-pdf").onclick = () => {
  const s = active();
  if (s && !/\.pdf$/i.test(s.filename)) {
    toast("Side-by-side view is for PDFs only");
    return;
  }
  const pane = $("pdf-pane");
  const showing = !pane.hidden;
  pane.hidden = showing;
  $("panes").classList.toggle("split", !showing);
  $("toggle-pdf").textContent = showing ? "Show PDF" : "Hide PDF";
};

$("theme").onclick = () => {
  const root = document.documentElement;
  const next = root.dataset.theme === "dark" ? "light" : "dark";
  root.dataset.theme = next;
  localStorage.setItem("theme", next);
};

// The ids currently in the sidebar. Exports are scoped to these: statements now
// persist, so an unscoped "export all" would mean the entire history rather than
// what the user is looking at.
function workingSetIds() {
  return state.statements.filter((s) => !s.pending).map((s) => s.id).join(",");
}

async function signOut() {
  try {
    await fetch("/api/logout", { method: "POST" });
  } finally {
    location.href = "/login";
  }
}

// Boot: identify the user, then load the most recent statements. The list
// endpoint returns summaries only, so payloads are fetched for the ones shown.
async function boot() {
  document.documentElement.dataset.theme = localStorage.getItem("theme") || "dark";

  const meRes = await api("/api/me").catch(() => null);
  if (!meRes) return;
  state.user = await meRes.json();
  $("whoami").textContent = state.user.display_name || state.user.email;

  const res = await api("/api/statements?limit=25").catch(() => null);
  if (!res) return;
  const { items } = await res.json();
  for (const it of items) {
    const full = await api(`/api/statements/${it.id}`).then((r) => r.json());
    state.statements.push(full);
  }
  if (state.statements.length) state.activeId = state.statements[0].id;
  renderList();
  renderViewer();
}

$("edit-table").onclick = toggleEdit;
$("save-edits").onclick = saveEdits;
$("revert-edits").onclick = revertEdits;
$("signout").onclick = signOut;
boot();

/* ---------- PDF region selection ---------- */

// Drawing a box over the page restricts *table detection* to what is inside it,
// which is how a statement buried in letterhead and legal copy gets read. The
// box is stored as fractions of the page, so it survives zoom, window size and
// a restart. The server deliberately keeps validating against the whole page -
// see parse_pdf, the crop must never narrow what checks the figures.

pdfjsLib.GlobalWorkerOptions.workerSrc = "/vendor/pdf.worker.min.js";

const pdfView = { doc: null, page: 1, sid: null, viewport: null, picking: false, chain: null };

async function openPdfViewer(s) {
  const wrap = $("pdf-canvas-wrap");
  const frame = $("pdf");

  // Only PDFs can be drawn on; a CSV keeps the plain viewer.
  if (!/\.pdf$/i.test(s.filename)) {
    wrap.hidden = true;
    frame.hidden = false;
    frame.src = `/api/statements/${s.id}/pdf`;
    $("pick-region").hidden = true;
    $("clear-region").hidden = true;
    $("pdf-pager").textContent = "";
    return;
  }

  $("pick-region").hidden = false;
  $("clear-region").hidden = !s.region;
  frame.hidden = true;
  frame.removeAttribute("src");
  wrap.hidden = false;

  if (pdfView.sid !== s.id) {
    pdfView.sid = s.id;
    pdfView.page = (s.region?.page ?? 0) + 1;
    const res = await api(`/api/statements/${s.id}/pdf`);
    const bytes = new Uint8Array(await res.arrayBuffer());
    pdfView.doc = await pdfjsLib.getDocument({ data: bytes }).promise;
  }
  await renderPdfPage(s);
}

// Renders are serialised through one chain. Cancelling the in-flight task is
// not enough on its own: several callers can read pdfView.task before any of
// them assigns it, since the first await yields. pdf.js then refuses the second
// render with "same canvas in use". A queue removes the race rather than
// narrowing it.
function renderPdfPage(s) {
  pdfView.chain = (pdfView.chain || Promise.resolve())
    .catch(() => {})
    .then(() => drawPdfPage(s));
  return pdfView.chain;
}

async function drawPdfPage(s) {
  if (!pdfView.doc) return;
  const page = await pdfView.doc.getPage(pdfView.page);
  const canvas = $("pdf-canvas");
  const wrap = $("pdf-canvas-wrap");

  // Fit the width of the pane; the overlay is sized to match exactly so a box
  // drawn on it converts to page fractions with no offset maths.
  const scale = Math.max(0.2, (wrap.clientWidth - 2) / page.getViewport({ scale: 1 }).width);
  const viewport = page.getViewport({ scale });
  pdfView.viewport = viewport;

  canvas.width = viewport.width;
  canvas.height = viewport.height;
  canvas.style.width = viewport.width + "px";
  canvas.style.height = viewport.height + "px";
  const overlay = $("pdf-overlay");
  overlay.style.width = viewport.width + "px";
  overlay.style.height = viewport.height + "px";

  // renderViewer runs on every list/selection change, so renders overlap and
  // pdf.js refuses a second one on a canvas already in use. Cancel the one in
  // flight rather than let the newer call throw into the fallback path.
  await page.render({ canvasContext: canvas.getContext("2d"), viewport }).promise;

  $("pdf-pager").textContent =
    pdfView.doc.numPages > 1 ? `Page ${pdfView.page} of ${pdfView.doc.numPages}` : "";

  overlay.innerHTML = "";
  if (s.region && s.region.page === pdfView.page - 1) drawSavedRegion(s.region);
}

function drawSavedRegion(r) {
  const box = document.createElement("div");
  box.className = "region saved";
  box.style.left = r.x0 * 100 + "%";
  box.style.top = r.y0 * 100 + "%";
  box.style.width = (r.x1 - r.x0) * 100 + "%";
  box.style.height = (r.y1 - r.y0) * 100 + "%";
  $("pdf-overlay").append(box);
}

function startRegionPick() {
  const s = active();
  if (!s || !pdfView.doc) return;
  pdfView.picking = true;
  $("pdf-overlay").classList.add("picking");
  $("region-hint").hidden = false;
  $("pick-region").textContent = "Cancel";
}

function stopRegionPick() {
  pdfView.picking = false;
  $("pdf-overlay").classList.remove("picking");
  $("region-hint").hidden = true;
  $("pick-region").textContent = "Select region";
}

// Fractions of the overlay, exactly as the sibling PDF tool does it: the same
// box means the same thing at any zoom or screen size.
$("pdf-overlay").addEventListener("pointerdown", (e) => {
  if (!pdfView.picking) return;
  e.preventDefault();
  const overlay = $("pdf-overlay");
  const rect = overlay.getBoundingClientRect();
  const start = { x: e.clientX, y: e.clientY };
  const box = document.createElement("div");
  box.className = "region drawing";
  overlay.append(box);

  const move = (ev) => {
    const x0 = Math.min(start.x, ev.clientX) - rect.left;
    const y0 = Math.min(start.y, ev.clientY) - rect.top;
    box.style.left = x0 + "px";
    box.style.top = y0 + "px";
    box.style.width = Math.abs(ev.clientX - start.x) + "px";
    box.style.height = Math.abs(ev.clientY - start.y) + "px";
  };

  const up = async (ev) => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    box.remove();

    const region = {
      page: pdfView.page - 1,
      x0: (Math.min(start.x, ev.clientX) - rect.left) / rect.width,
      y0: (Math.min(start.y, ev.clientY) - rect.top) / rect.height,
      x1: (Math.max(start.x, ev.clientX) - rect.left) / rect.width,
      y1: (Math.max(start.y, ev.clientY) - rect.top) / rect.height,
    };
    for (const k of ["x0", "y0", "x1", "y1"]) {
      region[k] = Math.min(1, Math.max(0, region[k]));
    }
    stopRegionPick();
    if (region.x1 - region.x0 < 0.01 || region.y1 - region.y0 < 0.01) {
      toast("That box is too small");
      return;
    }
    await reparse(region);
  };

  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
});

async function reparse(region) {
  const s = active();
  if (!s) return;
  if (s.edited && !confirm("Re-reading replaces the table and discards your edits. Continue?")) {
    return;
  }
  toast(region ? "Reading the selected region…" : "Reading the whole page…");
  const res = await api(`/api/statements/${s.id}/reparse`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ region }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    toast(data.detail || "Could not re-read that file");
    return;
  }
  const updated = await res.json();
  const i = state.statements.findIndex((v) => v.id === s.id);
  state.statements[i] = updated;
  state.editing = null;
  state.dirty = false;
  toast(`${updated.rows.length} row${updated.rows.length === 1 ? "" : "s"} found`);
  renderList();
  renderViewer();
}

$("pick-region").onclick = () => (pdfView.picking ? stopRegionPick() : startRegionPick());
$("clear-region").onclick = () => reparse(null);

/* ---------- navbar ---------- */

// Hash routing keeps it one page: the statement viewer keeps its state while the
// OCR view is open, and a reload lands back on the same section.
function showView() {
  const view = location.hash === "#ocr" ? "ocr" : "statements";
  $("statements-view").hidden = view !== "statements";
  $("ocr-view").hidden = view !== "ocr";
  $("export-all").style.visibility = view === "statements" ? "" : "hidden";
  document.querySelectorAll(".nav-link").forEach((a) =>
    a.classList.toggle("active", a.dataset.view === view)
  );
  if (view === "ocr") loadOcrOptions();
}
window.addEventListener("hashchange", showView);

/* ---------- simple OCR ---------- */

// One image in, plain text out. Nothing is stored server-side.
const ocr = { file: null, options: null, previewUrl: null };

function remember(key, value) {
  try { localStorage.setItem(key, value); } catch {}
}
function recall(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

async function loadOcrOptions() {
  if (ocr.options) return;
  const res = await api("/api/ocr/options").catch(() => null);
  if (!res) return;
  ocr.options = await res.json();

  const lang = $("ocr-language");
  lang.innerHTML = "";
  for (const l of ocr.options.languages) lang.add(new Option(l.label, l.code));
  lang.value = recall("ocr-language") || "auto";

  const eng = $("ocr-engine");
  eng.innerHTML = "";
  for (const e of ocr.options.engines) {
    const opt = new Option(e.label + (e.available ? "" : " - unavailable"), e.id);
    opt.disabled = !e.available;
    eng.add(opt);
  }
  const saved = ocr.options.engines.find((e) => e.id === recall("ocr-engine") && e.available);
  const first = ocr.options.engines.find((e) => e.available);
  eng.value = (saved || first || ocr.options.engines[0]).id;
  renderEngineNote();
  updateOcrButtons();
}

function renderEngineNote() {
  const e = ocr.options?.engines.find((x) => x.id === $("ocr-engine").value);
  const unavailable = (ocr.options?.engines || [])
    .filter((x) => !x.available)
    .map((x) => `${x.label}: ${x.reason}`);
  const lines = [];
  if (e && !e.local) lines.push("The image is sent to the Anthropic API to be read.");
  if (e && e.local) lines.push("Runs on this machine. Weak on handwriting.");
  $("ocr-engine-note").textContent = [...lines, ...unavailable].join(" ");
}

function updateOcrButtons() {
  const engineOk = ocr.options?.engines.some((e) => e.id === $("ocr-engine").value && e.available);
  $("ocr-run").disabled = !(ocr.file && engineOk);
  $("ocr-copy").disabled = !$("ocr-text").value.trim();
}

function setOcrFile(file) {
  if (!file) return;
  if (!file.type.startsWith("image/")) {
    toast(`${file.name || "That file"} is not an image`);
    return;
  }
  ocr.file = file;
  if (ocr.previewUrl) URL.revokeObjectURL(ocr.previewUrl);
  ocr.previewUrl = URL.createObjectURL(file);
  $("ocr-preview").src = ocr.previewUrl;
  $("ocr-preview").hidden = false;
  $("ocr-drop-text").hidden = true;
  updateOcrButtons();
}

async function runOcr() {
  if (!ocr.file) return;
  const btn = $("ocr-run");
  btn.disabled = true;
  btn.textContent = "Reading...";
  $("ocr-note").textContent = "";
  const form = new FormData();
  // Pasted images arrive without a name; the server checks the extension.
  const name = ocr.file.name || `pasted.${(ocr.file.type.split("/")[1] || "png")}`;
  form.append("file", ocr.file, name);
  form.append("engine", $("ocr-engine").value);
  form.append("language", $("ocr-language").value);
  try {
    const res = await api("/api/ocr", { method: "POST", body: form });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      $("ocr-note").textContent = body.detail || `Failed (${res.status})`;
      toast("OCR failed");
      return;
    }
    $("ocr-text").value = body.text;
    $("ocr-note").textContent = body.note || "";
  } catch (err) {
    if (err.message !== "signed out") $("ocr-note").textContent = `Failed: ${err.message}`;
  } finally {
    btn.textContent = "Extract text";
    updateOcrButtons();
  }
}

const ocrDrop = $("ocr-drop");
["dragenter", "dragover"].forEach((ev) =>
  ocrDrop.addEventListener(ev, (e) => { e.preventDefault(); ocrDrop.classList.add("over"); })
);
["dragleave", "drop"].forEach((ev) =>
  ocrDrop.addEventListener(ev, (e) => { e.preventDefault(); ocrDrop.classList.remove("over"); })
);
ocrDrop.addEventListener("drop", (e) => setOcrFile(e.dataTransfer.files[0]));
ocrDrop.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("ocr-file").click(); }
});
$("ocr-file").onchange = (e) => { setOcrFile(e.target.files[0]); e.target.value = ""; };

// Paste a screenshot straight from the clipboard while the OCR view is open.
document.addEventListener("paste", (e) => {
  if ($("ocr-view").hidden) return;
  const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith("image/"));
  if (!item) return;
  e.preventDefault();
  setOcrFile(item.getAsFile());
});

$("ocr-language").onchange = (e) => remember("ocr-language", e.target.value);
$("ocr-engine").onchange = (e) => {
  remember("ocr-engine", e.target.value);
  renderEngineNote();
  updateOcrButtons();
};
$("ocr-run").onclick = runOcr;
$("ocr-text").oninput = updateOcrButtons;
$("ocr-copy").onclick = () => copy($("ocr-text").value, "Text");

showView();
