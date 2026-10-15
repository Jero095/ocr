/* ---------- deposit slips ---------- */

// Claude reads each slip into fields; this file decides whether the figures add
// up. The two are kept apart on purpose: the model is told never to adjust a
// figure to make the totals agree, so the check below is independent of the
// reading and a misread digit shows up as a mismatch instead of a false pass.
//
// CSV export is blocked while any slip does not add up, the same rule the
// statement exports follow. A misread figure is fixed by editing it against the
// image beside it, after which the check re-runs.

const slips = { items: [], seq: 0, reading: 0 };
const SLIP_CONCURRENCY = 3;

/* ---------- money, in integer cents ---------- */

// "" -> null (blank on the slip), anything not a plain amount -> NaN.
function cents(value) {
  const s = String(value ?? "").replace(/[$,\s]/g, "");
  if (!s) return null;
  if (!/^-?\d+(\.\d{1,2})?$/.test(s)) return NaN;
  const [whole, frac = ""] = s.replace("-", "").split(".");
  const n = Number(whole) * 100 + Number(frac.padEnd(2, "0"));
  return s.startsWith("-") ? -n : n;
}

function slipMoney(c) {
  if (c === null || c === undefined || Number.isNaN(c)) return "";
  const sign = c < 0 ? "-" : "";
  const abs = Math.abs(c);
  const whole = Math.floor(abs / 100).toLocaleString("en-US");
  return `${sign}${whole}.${String(abs % 100).padStart(2, "0")}`;
}

/* ---------- the check ---------- */

// Two totals may be written on a slip, and each is an independent reference:
//   check total = sum of the payments (no cash)
//   net total   = payments + cash
// Either one, when present, must agree with the payments as read. With neither
// there is nothing to check against, so the slip is "unverified" and only goes
// into the CSV once someone confirms it against the image by hand.
//
// Returns { state: "ok" | "bad" | "unverified", lines: [{level, text}] }.
function reconcile(slip) {
  const lines = [];
  const bad = (text) => lines.push({ level: "bad", text });
  const good = (text) => lines.push({ level: "ok", text });

  const payments = slip.payments.filter((p) => String(p.amount).trim() !== "");
  const amounts = payments.map((p) => cents(p.amount));
  const cash = slip.cash_present ? cents(slip.cash) : 0;
  const checkTotal = cents(slip.check_total);
  const netTotal = cents(slip.net_total);

  const invalid = [];
  amounts.forEach((a, i) => Number.isNaN(a) && invalid.push(`payment ${i + 1}`));
  if (Number.isNaN(cash)) invalid.push("cash");
  if (Number.isNaN(checkTotal)) invalid.push("check total");
  if (Number.isNaN(netTotal)) invalid.push("net total");
  if (invalid.length) {
    bad(`Not a valid amount: ${invalid.join(", ")}. Use digits and a decimal point.`);
    return { state: "bad", lines };
  }
  if (slip.cash_present && cash === null) {
    bad("Cash is marked as present but has no amount. Enter it, or untick cash.");
    return { state: "bad", lines };
  }
  if (!payments.length) {
    bad("No payments were read from this slip.");
    return { state: "bad", lines };
  }

  const sum = amounts.reduce((a, b) => a + b, 0);
  const n = `${payments.length} payment${payments.length === 1 ? "" : "s"}`;
  let ok = true;

  if (checkTotal !== null) {
    if (sum === checkTotal) good(`${n} = check total ${slipMoney(checkTotal)}`);
    else {
      bad(`${n} add up to ${slipMoney(sum)}, but the check total is ` +
          `${slipMoney(checkTotal)} (off by ${slipMoney(Math.abs(checkTotal - sum))}).`);
      ok = false;
    }
  }
  if (netTotal !== null) {
    const deposit = sum + (cash || 0);
    const parts = cash ? `${n} + cash ${slipMoney(cash)}` : `${n} (no cash)`;
    if (deposit === netTotal) good(`${parts} = net total ${slipMoney(netTotal)}`);
    else {
      bad(`${parts} = ${slipMoney(deposit)}, but the net total is ` +
          `${slipMoney(netTotal)} (off by ${slipMoney(Math.abs(netTotal - deposit))}).`);
      ok = false;
    }
  }
  if (!ok) {
    lines.push({ level: "info", text: "Compare each figure with the image and correct the misread one." });
    return { state: "bad", lines };
  }
  if (checkTotal === null && netTotal === null) {
    lines.push({ level: "info", text:
      `No check total or net total on this slip, so ${n} totalling ` +
      `${slipMoney(sum)} could not be cross-checked.` });
    return { state: "unverified", lines };
  }
  return { state: "ok", lines };
}

// Whether a read slip may go into the CSV.
const exportable = (entry) => {
  const { state } = reconcile(entry.slip);
  return state === "ok" || (state === "unverified" && entry.confirmed);
};

/* ---------- reading ---------- */

function addSlipFiles(files) {
  const images = [...files].filter((f) => f.type.startsWith("image/"));
  if (images.length < files.length) toast("Skipped files that are not images");
  for (const file of images) {
    slips.items.push({
      id: ++slips.seq,
      file,
      url: URL.createObjectURL(file),
      name: file.name || `pasted-${slips.seq}.png`,
      status: "queued",
    });
  }
  renderSlips();
  pumpSlips();
}

function pumpSlips() {
  while (slips.reading < SLIP_CONCURRENCY) {
    const next = slips.items.find((s) => s.status === "queued");
    if (!next) return;
    readSlip(next);
  }
}

async function readSlip(entry) {
  entry.status = "reading";
  slips.reading++;
  renderSlips();
  const form = new FormData();
  form.append("file", entry.file, entry.name);
  try {
    const res = await api("/api/ocr/deposit-slip", { method: "POST", body: form });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || `Failed (${res.status})`);
    entry.slip = body;
    entry.original = JSON.parse(JSON.stringify(body));
    entry.status = "read";
  } catch (err) {
    if (err.message === "signed out") return;
    entry.status = "error";
    entry.error = err.message;
  } finally {
    slips.reading--;
    renderSlips();
    pumpSlips();
  }
}

/* ---------- rendering ---------- */

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node[k] = v;
    else if (v !== undefined && v !== null) node.setAttribute(k, v);
  }
  for (const c of children.flat()) node.append(c instanceof Node ? c : String(c));
  return node;
}

function renderSlips() {
  const list = $("slip-list");
  list.replaceChildren(...slips.items.map(slipCard));
  renderSlipSummary();
}

// Re-renders only the verdict, so typing in a field keeps its focus.
function refreshVerdict(entry) {
  const card = document.querySelector(`[data-slip="${entry.id}"]`);
  if (card) card.querySelector(".slip-verdict").replaceWith(verdict(entry));
  renderSlipSummary();
}

// Any edit withdraws a manual confirmation: it vouched for different figures.
function edited(entry) {
  entry.confirmed = false;
  refreshVerdict(entry);
}

const VERDICT_TITLES = {
  ok: "Cross-checked",
  bad: "Does not add up - export blocked",
  unverified: "Not verified - no totals on the slip",
};

function verdict(entry) {
  const { state, lines } = reconcile(entry.slip);
  const changed = JSON.stringify(entry.slip) !== JSON.stringify(entry.original);
  const confirm = state === "unverified"
    ? el("label", { class: "slip-confirm" },
        el("input", {
          type: "checkbox",
          ...(entry.confirmed ? { checked: "" } : {}),
          onchange: (e) => { entry.confirmed = e.target.checked; refreshVerdict(entry); },
        }),
        " I checked these figures against the image - include in the CSV")
    : "";
  return el("div", { class: `slip-verdict ${state}` },
    el("strong", {}, VERDICT_TITLES[state]),
    changed ? el("span", { class: "muted" }, " (edited)") : "",
    el("ul", {}, lines.map((l) => el("li", { class: l.level }, l.text))),
    confirm,
  );
}

function field(entry, key, label, opts = {}) {
  return el("label", { class: "slip-field" },
    el("span", { class: "muted" }, label),
    el("input", {
      value: entry.slip[key] ?? "",
      class: opts.money ? "num" : "",
      inputmode: opts.money ? "decimal" : undefined,
      placeholder: opts.placeholder,
      oninput: (e) => { entry.slip[key] = e.target.value; edited(entry); },
    }),
  );
}

// Cash is either written beside CASH or absent, and "absent" has to be explicit
// so a blank field cannot be mistaken for a figure that failed to read.
function cashField(entry) {
  const s = entry.slip;
  return el("label", { class: "slip-field" },
    el("span", { class: "muted slip-cash-label" },
      el("input", {
        type: "checkbox", title: "Cash on this slip",
        ...(s.cash_present ? { checked: "" } : {}),
        onchange: (e) => {
          s.cash_present = e.target.checked;
          if (!s.cash_present) s.cash = "";
          entry.confirmed = false;
          renderSlips();
        },
      }),
      " Cash"),
    s.cash_present
      ? el("input", {
          value: s.cash, class: "num", inputmode: "decimal",
          oninput: (e) => { s.cash = e.target.value; edited(entry); },
        })
      : el("input", { value: "None", disabled: "", class: "num" }),
  );
}

function paymentsTable(entry) {
  const rows = entry.slip.payments.map((p, i) =>
    el("tr", {},
      el("td", { class: "muted" }, i + 1),
      el("td", {}, el("input", {
        value: p.check_number, placeholder: "-",
        oninput: (e) => { p.check_number = e.target.value; edited(entry); },
      })),
      el("td", {}, el("input", {
        value: p.amount, class: "num", inputmode: "decimal",
        oninput: (e) => { p.amount = e.target.value; edited(entry); },
      })),
      el("td", {}, el("button", {
        class: "btn ghost icon", title: "Remove payment", "aria-label": "Remove payment",
        onclick: () => { entry.slip.payments.splice(i, 1); entry.confirmed = false; renderSlips(); },
      }, "×")),
    ));
  return el("div", {},
    el("table", { class: "slip-items" },
      el("thead", {}, el("tr", {},
        el("th", {}, "#"), el("th", {}, "Check no."), el("th", {}, "Payment"), el("th", {}))),
      el("tbody", {}, rows)),
    el("button", {
      class: "btn ghost", onclick: () => {
        entry.slip.payments.push({ check_number: "", amount: "" });
        entry.confirmed = false;
        renderSlips();
      },
    }, "+ Add payment"),
  );
}

function slipCard(entry) {
  const remove = el("button", {
    class: "btn ghost", onclick: () => {
      URL.revokeObjectURL(entry.url);
      slips.items = slips.items.filter((s) => s !== entry);
      renderSlips();
    },
  }, "Remove");

  const image = el("a", { href: entry.url, target: "_blank", class: "slip-image",
                          title: "Open full size" },
    el("img", { src: entry.url, alt: `Deposit slip ${entry.name}` }));

  const head = el("div", { class: "slip-card-head" },
    el("strong", {}, entry.name), remove);

  let body;
  if (entry.status === "queued" || entry.status === "reading") {
    body = el("p", { class: "muted" }, entry.status === "queued" ? "Waiting..." : "Reading the slip...");
  } else if (entry.status === "error") {
    body = el("div", { class: "slip-verdict bad" },
      el("strong", {}, "Could not read this slip"), el("p", {}, entry.error),
      el("button", { class: "btn", onclick: () => { entry.status = "queued"; renderSlips(); pumpSlips(); } }, "Retry"));
  } else {
    const s = entry.slip;
    body = el("div", { class: "slip-body" },
      verdict(entry),
      s.uncertain?.length
        ? el("div", { class: "slip-uncertain" },
            el("strong", {}, "Uncertain readings"),
            el("ul", {}, s.uncertain.map((u) => el("li", {}, u))))
        : "",
      el("div", { class: "slip-fields" },
        field(entry, "date", "Date"),
        field(entry, "office", "Office"),
        cashField(entry),
      ),
      paymentsTable(entry),
      el("div", { class: "slip-fields" },
        field(entry, "check_total", "Check total", { money: true, placeholder: "Not on slip" }),
        field(entry, "net_total", "Net total", { money: true, placeholder: "Not on slip" }),
      ),
    );
  }
  return el("article", { class: "slip-card", "data-slip": entry.id },
    image, el("div", { class: "slip-detail" }, head, body));
}

function renderSlipSummary() {
  const n = slips.items.length;
  const read = slips.items.filter((s) => s.status === "read");
  const states = read.map((s) => reconcile(s.slip).state);
  const count = (st) => states.filter((x) => x === st).length;
  const busy = slips.items.some((s) => s.status === "queued" || s.status === "reading");
  const failed = slips.items.filter((s) => s.status === "error").length;
  const waiting = read.filter((s) => reconcile(s.slip).state === "unverified" && !s.confirmed).length;

  const parts = [];
  if (n) parts.push(`${count("ok")} of ${n} slip${n === 1 ? "" : "s"} cross-checked`);
  if (count("bad")) parts.push(`${count("bad")} not adding up`);
  if (waiting) parts.push(`${waiting} waiting for confirmation`);
  if (busy) parts.push("reading...");
  if (failed) parts.push(`${failed} could not be read`);
  $("slip-summary").textContent = parts.join(" · ");

  const ready = n > 0 && !busy && !failed && read.every(exportable);
  $("slip-csv").disabled = !ready;
  $("slip-csv").title = ready ? "" : "Every slip must add up, or be confirmed by hand, before export";
  $("slip-clear").hidden = n === 0;
}

/* ---------- CSV ---------- */

const CSV_COLUMNS = ["Date", "Office", "Type", "Check Number", "Amount",
                     "Check Total", "Net Total", "Verified", "Source File"];

function csvCell(v) {
  const s = String(v ?? "");
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

const fixed = (c) => (c === null ? "" : (c / 100).toFixed(2));

function buildCsv() {
  const rows = [CSV_COLUMNS];
  for (const entry of slips.items) {
    const s = entry.slip;
    // "Verified" says how the slip got into the file: cross-checked against
    // its own totals, or confirmed by a person because it had none.
    const how = reconcile(s).state === "ok" ? "Cross-checked" : "Confirmed by hand";
    const tail = [fixed(cents(s.check_total)), fixed(cents(s.net_total)), how, entry.name];
    if (s.cash_present) rows.push([s.date, s.office, "Cash", "", fixed(cents(s.cash)), ...tail]);
    for (const p of s.payments) {
      const amount = cents(p.amount);
      if (amount === null) continue;
      rows.push([s.date, s.office, "Payment", p.check_number, fixed(amount), ...tail]);
    }
  }
  return rows.map((r) => r.map(csvCell).join(",")).join("\r\n") + "\r\n";
}

function downloadCsv() {
  // Re-checked here, not only via the disabled button: figures that do not add
  // up must not leave the page.
  if (slips.items.some((s) => s.status !== "read" || !exportable(s))) {
    toast("Every slip must add up, or be confirmed by hand, before export");
    return;
  }
  // The BOM makes Excel open the file as UTF-8 instead of mangling accents.
  const blob = new Blob(["﻿" + buildCsv()], { type: "text/csv;charset=utf-8" });
  const a = el("a", {
    href: URL.createObjectURL(blob),
    download: `deposit-slips-${new Date().toISOString().slice(0, 10)}.csv`,
  });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

/* ---------- wiring ---------- */

const slipDrop = $("slip-drop");
["dragenter", "dragover"].forEach((ev) =>
  slipDrop.addEventListener(ev, (e) => { e.preventDefault(); slipDrop.classList.add("over"); })
);
["dragleave", "drop"].forEach((ev) =>
  slipDrop.addEventListener(ev, (e) => { e.preventDefault(); slipDrop.classList.remove("over"); })
);
slipDrop.addEventListener("drop", (e) => addSlipFiles(e.dataTransfer.files));
slipDrop.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("slip-file").click(); }
});
$("slip-file").onchange = (e) => { addSlipFiles(e.target.files); e.target.value = ""; };

document.addEventListener("paste", (e) => {
  if ($("slips-view").hidden) return;
  const files = [...(e.clipboardData?.items || [])]
    .filter((i) => i.type.startsWith("image/")).map((i) => i.getAsFile());
  if (!files.length) return;
  e.preventDefault();
  addSlipFiles(files);
});

$("slip-csv").onclick = downloadCsv;
$("slip-clear").onclick = () => {
  if (slips.items.some((s) => s.status === "reading")) {
    toast("Wait for the slips being read to finish");
    return;
  }
  slips.items.forEach((s) => URL.revokeObjectURL(s.url));
  slips.items = [];
  renderSlips();
};

// Deposit slips need Claude; say so up front rather than after an upload fails.
document.addEventListener("ocr-options", () => {
  const claude = ocr.options.engines.find((e) => e.id === "claude");
  $("slip-engine-note").textContent = claude?.available
    ? "Slips are read by Claude: each image is sent to the Anthropic API."
    : `Deposit slips need the Claude engine. ${claude?.reason || ""}`;
  slipDrop.classList.toggle("disabled", !claude?.available);
});
