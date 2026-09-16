const $ = (id) => document.getElementById(id);

const state = { statements: [], activeId: null, user: null };

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
  $("carrier").textContent = s.template
    ? `${s.template} · ${s.rows.length} row${s.rows.length === 1 ? "" : "s"}`
    : "Layout not recognised";

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
  $("pdf").src = `/api/statements/${s.id}/pdf`;
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

  el.className = `failsafe ${view.cls}`;
  el.innerHTML =
    `<span class="failsafe-icon">${view.icon}</span>` +
    `<span class="failsafe-body">` +
    `<div class="failsafe-title">${view.title}</div>` +
    `<div class="failsafe-detail">${view.detail}</div>` +
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

  const thead = table.createTHead();
  const hr = thead.insertRow();
  s.columns.forEach((col, i) => {
    const th = document.createElement("th");
    th.textContent = col;
    th.title = `Copy the ${col} column`;
    th.onclick = () => {
      const vals = s.rows.map((r) => r[col] ?? "");
      copy(vals.join("\n"), col);
    };
    hr.append(th);
  });

  const tbody = table.createTBody();
  const addRow = (data, cls) => {
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
      if (val) {
        td.onclick = async () => {
          if (await copy(val, col)) {
            td.classList.add("flash");
            setTimeout(() => td.classList.remove("flash"), 450);
          }
        };
      }
    }
  };

  s.rows.forEach((r) => addRow(r));
  if (s.totals && Object.keys(s.totals).length) addRow(s.totals, "totals");
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

$("signout").onclick = signOut;
boot();
