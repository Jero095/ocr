"""Run the payout failsafe over every statement and summarise.

    python scripts/failsafe_report.py
"""
from __future__ import annotations

import pathlib, sys, warnings
from collections import Counter

warnings.filterwarnings("ignore")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.extract import parse_pdf  # noqa: E402

rows = []
for p in sorted(pathlib.Path("statements").glob("*")):
    if p.suffix.lower() != ".pdf":
        continue
    try:
        s = parse_pdf(str(p), p.name)
        c = s.payout
        rows.append((p.name, c.status, c.exported_total, c.commission_column,
                     c.filename_total, c.statement_total, c.statement_label,
                     c.references_disagree))
    except Exception as exc:
        rows.append((p.name, f"CRASH:{type(exc).__name__}", None, "", None, None, "", False))

W = "{:<30} {:<13} {:>11} {:>11} {:>11}  {}"
print(W.format("file", "status", "exported", "filename", "statement", "commission column"))
print("-" * 104)
fmt = lambda v: f"{v:,.2f}" if isinstance(v, float) else "-"
for name, status, exp, col, fn, st, label, dis in rows:
    flag = "  <-- REFS DISAGREE" if dis else ""
    print(W.format(name[:30], status, fmt(exp), fmt(fn), fmt(st), (col or "-")[:24] + flag))

print("\n" + str(dict(Counter(r[1] for r in rows))))
bad = [r[0] for r in rows if r[1] == "mismatch"]
print("MISMATCH (export blocked):", bad or "none")
