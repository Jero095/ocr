const $ = (id) => document.getElementById(id);

const state = { statements: [], activeId: null };

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
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      toast(`${file.name} is not a PDF`);
      continue;
    }

    const placeholder = { id: `tmp-${Date.now()}`, filename: file.name, pending: true };
    state.statements.push(placeholder);
    renderList();

    const body = new FormData();
    body.append("file", file);

    try {
      const res = await fetch("/api/statements", { method: "POST", body });
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
    else {
      const ok = s.checks?.length && s.checks.every((c) => c.ok) && !s.warnings?.length;
      dot.className = "dot " + (ok ? "ok" : "bad");
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
        await fetch(`/api/statements/${s.id}`, { method: "DELETE" });
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

  $("badges").innerHTML = "";
  for (const c of s.checks) {
    const b = document.createElement("span");
    b.className = "badge " + (c.ok ? "ok" : "bad");
    b.textContent = c.ok
      ? `${c.label} ${c.stated_total.toFixed(2)} ✓`
      : `${c.label} ${c.rows_total.toFixed(2)} ≠ ${c.stated_total.toFixed(2)}`;
    $("badges").append(b);
  }

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

function renderTable(s) {
  const table = $("table");
  table.innerHTML = "";

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

$("export-all").onclick = () => (location.href = "/api/export.tsv");

$("toggle-pdf").onclick = () => {
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

document.documentElement.dataset.theme = localStorage.getItem("theme") || "dark";

// Restore anything already parsed server-side (survives a page refresh).
fetch("/api/statements")
  .then((r) => r.json())
  .then(async (items) => {
    for (const it of items) {
      const full = await fetch(`/api/statements/${it.id}`).then((r) => r.json());
      state.statements.push(full);
    }
    if (state.statements.length) state.activeId = state.statements[0].id;
    renderList();
    renderViewer();
  })
  .catch(() => {});
