#!/usr/bin/env python3
"""Literal pass — controlled traversal of a data tree (docs/design/literal-pass.md).

The loop belongs to the harness: the model sees ONE vertex at a time and the
only write channel into the tree is commit(), which verifies that everything
outside the per-iteration manifest is structurally identical to the original
and rolls back otherwise. Generic scripts physically cannot damage the tree.

v0: xlsx verifier (openpyxl + zip entry-set guard) and text verifier (line
ranges). Everything else is fail-closed: no verifier, no write.

    pass_ = LiteralPass("/data/invoices")
    while (v := pass_.next_vertex()):
        print(pass_._prompt(v))
        edits, manifest = analyze(v)          # per-item brain (agent/delegate)
        pass_.commit(v, edits, manifest)
    print(pass_.reconcile())
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import zipfile
from pathlib import Path

BENIGN_XLSX_DROPS = {"xl/calcChain.xml"}  # formula-cache: openpyxl drops it, semantically dead


def sha(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def inventory(root: str, mask: str | None = None) -> list[str]:
    out = []
    for dirpath, _dirs, files in os.walk(root):
        if os.path.basename(dirpath) == ".pass":
            continue
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            if mask is None or Path(f).match(mask):
                out.append(p)
    return sorted(out)


class LiteralPass:
    def __init__(self, root: str, mask: str | None = None):
        self.root = root
        self.items = inventory(root, mask)
        self.journal = Path(root) / ".pass"
        self.journal.mkdir(exist_ok=True)
        self.jl = self.journal / "journal.jsonl"
        self.k = self._done_count()

    def _done_count(self) -> int:
        if not self.jl.exists():
            return 0
        items = set(self.items)
        return sum(1 for line in open(self.jl, encoding='utf-8-sig') if line.strip()
                   and json.loads(line)["status"] == "committed"
                   and json.loads(line)["file"] in items)

    def next_vertex(self) -> str | None:
        """One vertex per iteration — the model never sees the whole list."""
        return self.items[self.k] if self.k < len(self.items) else None

    def _prompt(self, v: str) -> str:
        return f"документ {self.k + 1} из {len(self.items)}: {v}"

    # ---- write channel -------------------------------------------------
    def commit(self, v: str, edits, manifest: dict) -> None:
        """Sole write path. edits: xlsx -> [(sheet, cell, value)]; text -> [(lo, hi, [lines])].

        manifest must declare what the edit legitimately touches; anything
        else that differs from the original => PENALTY: rollback, named method.
        """
        tmp = self.journal / ("staging" + Path(v).suffix)  # openpyxl checks extension
        self._apply(v, edits, tmp)
        problems = self._verify(v, tmp, manifest)
        if problems:
            tmp.unlink(missing_ok=True)
            self._log(v, manifest, "rejected", problems)
            raise PermissionError(
                "МЕТОД ОШТРАФОВАН: пакетная запись отвергнута как метод per-item задачи.\n"
                + "\n".join("  - " + p for p in problems)
                + "\nРаботай по одной вершине: манифест должен покрывать все изменения.")
        bak = self.journal / "backup" / (Path(v).name + f".{int(time.time())}.bak")
        bak.parent.mkdir(exist_ok=True)
        os.replace(v, bak)
        os.replace(tmp, v)
        self.k += 1
        self._log(v, manifest, "committed", [])

    def _apply(self, v: str, edits, tmp: Path) -> None:
        if v.endswith(".xlsx"):
            import openpyxl
            wb = openpyxl.load_workbook(v)  # formulas preserved
            for sheet, cell, value in edits:
                wb[sheet][cell] = value
            wb.save(tmp)
        elif v.endswith((".txt", ".csv")):
            lines = open(v, encoding='utf-8-sig').read().splitlines(keepends=True)
            for lo, hi, new in sorted(edits, key=lambda e: -e[0]):
                lines[lo - 1:hi] = [l if l.endswith("\n") else l + "\n" for l in new]
            tmp.write_text("".join(lines), encoding='utf-8')
        else:
            raise PermissionError(f"FAIL-CLOSED: нет верификатора для {v} — запись запрещена")

    def _verify(self, v: str, tmp: Path, manifest: dict) -> list[str]:
        problems = []
        # 1) structural: xlsx is a zip; dropped parts = silent data loss
        if v.endswith(".xlsx"):
            a, b = zipfile.ZipFile(v).namelist(), zipfile.ZipFile(tmp).namelist()
            lost = set(a) - set(b) - BENIGN_XLSX_DROPS
            if lost:
                problems.append(f"структура: потеряны части пакета {sorted(lost)} (графики/именованные?)")
            import openpyxl
            wa, wb_ = openpyxl.load_workbook(v), openpyxl.load_workbook(tmp)
            if wa.sheetnames != wb_.sheetnames:
                problems.append(f"структура: листы {wa.sheetnames} != {wb_.sheetnames}")
            touched = set(manifest.get("cells", []))
            for s in wa.sheetnames:
                sa, sb = wa[s], wb_[s]
                max_r = max(sa.max_row or 1, sb.max_row or 1)
                max_c = max(sa.max_column or 1, sb.max_column or 1)
                for r in range(1, max_r + 1):
                    for c in range(1, max_c + 1):
                        ca, cb = sa.cell(row=r, column=c), sb.cell(row=r, column=c)
                        ref = f"{s}!{ca.coordinate}"
                        if (ca.value, ca.data_type) != (cb.value, cb.data_type) and ref not in touched:
                            problems.append(f"чужая ячейка изменена: {ref}")
            # symmetric direction (cells appearing only in staging) is implied:
            # any new value lives at some coordinate compared per-row below.
        else:
            a = open(v, encoding='utf-8-sig').read().splitlines()
            b = open(tmp, encoding='utf-8-sig').read().splitlines()
            allowed = set()
            for lo, hi in manifest.get("lines", []):
                allowed |= set(range(lo, hi + 1))
            import difflib
            changed = {i + 1 for i in range(max(len(a), len(b)))
                       if (a[i] if i < len(a) else None) != (b[i] if i < len(b) else None)}
            # line-based diff can shift; fall back to SequenceMatcher opcodes
            changed = set()
            sm = difflib.SequenceMatcher(None, a, b)
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag != "equal":
                    changed |= set(range(i1 + 1, i2 + 2))  # +-1 slack for shifted blocks
            stray = changed - allowed
            if stray:
                problems.append(f"изменены строки вне манифеста: {sorted(stray)[:10]}")
        return problems

    def _log(self, v: str, manifest: dict, status: str, problems: list[str]) -> None:
        with open(self.jl, 'a', encoding='utf-8') as f:
            f.write(json.dumps({"ts": time.time(), "file": v, "manifest": manifest,
                                "status": status, "sha_after": sha(v) if status == "committed" else None,
                                "problems": problems}, ensure_ascii=False) + "\n")

    def reconcile(self) -> str:
        done = [json.loads(l) for l in open(self.jl, encoding='utf-8-sig') if l.strip()] if self.jl.exists() else []
        committed = {d["file"] for d in done if d["status"] == "committed"}
        missing = [i for i in self.items if i not in committed]
        verdict = "ACCEPTED" if not missing and self.k == len(self.items) else "NOT DONE"
        return json.dumps({"verdict": verdict, "done": len(committed), "total": len(self.items),
                           "missing": missing[:5]}, ensure_ascii=False)


def demo() -> int:
    """Self-proof on synthetic workbooks with different total-column layouts."""
    import openpyxl
    root = os.path.expanduser("~/.hermes/cache/scratch/lp-demo")
    os.makedirs(root, exist_ok=True)
    for name, sheet, col, row in (("a.xlsx", "Лист1", "F", 15), ("b.xlsx", "Отчёт", "K", 12)):
        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.title = sheet
        ws[f"{col}{row - 1}"] = "Итого к оплате"
        ws[f"{col}{row}"] = 12345
        ws["A1"] = "поставщик"
        wb.save(f"{root}/{name}")
    # a chartless commit passes
    p = LiteralPass(root, "*.xlsx")
    while (v := p.next_vertex()):
        is_a = v.endswith("a.xlsx")
        sheet, col, row = ("Лист1", "F", 15) if is_a else ("Отчёт", "K", 12)
        edits = [(sheet, f"B{row}", "проверено")]
        manifest = {"cells": [f"{sheet}!B{row}", f"{sheet}!{col}{row}"], "итог": f"{col}{row}"}
        print(p._prompt(v), "-> committing")
        p.commit(v, edits, manifest)
        print("   committed")
    # an overreaching commit (touches a foreign cell) must be penalized
    import shutil
    victim = f"{root}/c.xlsx"
    shutil.copy(f"{root}/a.xlsx", victim)   # fresh vertex, uncommitted
    before = sha(victim)
    p2 = LiteralPass(root, "c.xlsx")
    v = p2.next_vertex()
    assert v is not None
    try:
        p2.commit(v, [("Лист1", "A1", "ИЗБИТО")], {"cells": ["Лист1!B15"]})
        print("FAIL: надругательство прошло")
        return 1
    except PermissionError as e:
        assert sha(victim) == before, "оригинал не откатился!"
        print("penalty ok:", str(e).splitlines()[0])
    print("reconcile:", p.reconcile())
    return 0


if __name__ == "__main__":
    sys.exit(demo() if "--demo" in sys.argv else print("usage: --demo"))
