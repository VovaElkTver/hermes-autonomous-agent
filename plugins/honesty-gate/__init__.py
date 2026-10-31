"""honesty-gate — честный контур исполнения этапа.

Механизм (поdirective Архитектора, 31.10):
  1. ДО исполнения этапа — ``stage_open``: задание фиксируется в графе
     «ЗАДАЧА» файла REPORTS.md (домашняя папка Hermes, ~/.hermes/REPORTS.md).
  2. ПОСЛЕ исполнения — ``stage_close``: исходное задание + заявленный
     результат уходят критику с фиксированным вопросом
     «Что ещё можно было сделать для полноты исполнения?».
  3. Полный запрос и ответ критика дописываются в КОНЕЦ REPORTS.md;
     ответ возвращается исполнителю в tool result — не для отчёта,
     а чтобы пропущенные действия стали видимы до вердикта.

Файл append-only: открытые/закрытые этапы парсятся из него же,
состояния в памяти нет (агент не может быть памятью о себе).

Критик — OpenAI-совместимый /chat/completions (та же форма, что
supervisor.judge). На недоступность критика — fail-visible: в блок
пишется «CRITIC_UNAVAILABLE: …», тишина не симулируется.

Settings (plugins.entries.honesty-gate.settings):
  reports_path — default ~/.hermes/REPORTS.md
  judge        — {base_url, model, timeout_seconds}
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
import uuid
from typing import Any

log = logging.getLogger(__name__)

TOOLSET = "honesty_gate"
FIXED_QUESTION = "Что ещё можно было сделать для полноты исполнения?"
DEFAULT_JUDGE = {
    "base_url": "http://192.168.9.100:8001/v1",
    "model": "default",
    "timeout_seconds": 60,
}

_CTX: Any = None
_write_lock = threading.Lock()


# ── settings / storage ──────────────────────────────────────────────────────

def _cfg(key: str, default: Any) -> Any:
    return _CTX.get_config(key, default) if _CTX is not None else default


def _reports_path() -> str:
    return os.path.expanduser(_cfg("reports_path", "~/.hermes/REPORTS.md"))


def _judge_cfg() -> dict:
    cfg = dict(DEFAULT_JUDGE)
    cfg.update(_cfg("judge", {}) or {})
    return cfg


def _append(text: str) -> None:
    path = _reports_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _write_lock:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)


def _read_reports() -> str:
    path = _reports_path()
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return ""


def _ts() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


_HEADER = re.compile(r"^## STAGE (\S+) — (OPEN|CLOSE) — (\S+)", re.MULTILINE)


def _find_section(reports: str, stage_id: str, kind: str) -> str | None:
    """Return the full block (header..next header) for stage_id/kind, or None."""
    for m in _HEADER.finditer(reports):
        if m.group(1) == stage_id and m.group(2) == kind:
            end = reports.find("\n## STAGE ", m.end())
            return reports[m.start(): end if end != -1 else len(reports)]
    return None


def _task_of(reports: str, stage_id: str) -> str | None:
    block = _find_section(reports, stage_id, "OPEN")
    if block is None:
        return None
    m = re.search(r"\nЗАДАЧА:\n(.*?)$", block, re.DOTALL)
    return m.group(1).strip() if m else ""


# ── critic call (fail-visible) ──────────────────────────────────────────────

def _build_query(task: str, result: str) -> str:
    return (
        "ЗАДАЧА ЭТАПА (зафиксирована ДО исполнения, не переписывалась):\n"
        f"{task}\n\n"
        "РЕЗУЛЬТАТ (заявлен исполнителем ПОСЛЕ исполнения):\n"
        f"{result}\n\n"
        f"Вопрос: {FIXED_QUESTION}\n"
        "Формат ответа: короткий список конкретных действий, которых не хватает "
        "для полноты исполнения (или «ДОСТАТОЧНО» + одно предложение почему). "
        "Будь строг к исполнителю, вежливость не учитывай."
    )


def _ask_critic(query: str) -> str:
    cfg = _judge_cfg()
    url = str(cfg.get("base_url", DEFAULT_JUDGE["base_url"])).rstrip("/") + "/chat/completions"
    body = {
        "model": cfg.get("model", DEFAULT_JUDGE["model"]),
        "messages": [{"role": "user", "content": query}],
        "temperature": 0.2,
    }
    try:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=float(cfg.get("timeout_seconds", 60))) as resp:
            data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"].strip()
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError,
            TimeoutError, OSError, ValueError) as exc:
        return f"CRITIC_UNAVAILABLE: {exc}"


# ── tool handlers ───────────────────────────────────────────────────────────

def handle_stage_open(args: dict, **_kw) -> str:
    task = str(args.get("task") or "").strip()
    if not task:
        return "ОШИБКА: stage_open требует непустой task — задание до исполнения."
    title = str(args.get("title") or "").strip()
    stage_id = uuid.uuid4().hex[:8]
    header = f"## STAGE {stage_id} — OPEN — {_ts()}"
    if title:
        header += f" — {title}"
    _append(f"\n{header}\nЗАДАЧА:\n{task}\n")
    return (f"ЭТАП {stage_id} ОТКРЫТ, задание записано в {_reports_path()}. "
            f"По завершении вызови stage_close(stage_id=\"{stage_id}\", result=...) — "
            "задание+результат уйдут критику с фиксированным вопросом, "
            "ответ вернётся тебе и в файл. Вердикт «готово» до stage_close не выносится.")


def handle_stage_close(args: dict, **_kw) -> str:
    stage_id = str(args.get("stage_id") or "").strip()
    result = str(args.get("result") or "").strip()
    if not stage_id or not result:
        return "ОШИБКА: stage_close требует stage_id и result."
    reports = _read_reports()
    n_closed = sum(1 for m in _HEADER.finditer(reports)
                   if m.group(1) == stage_id and m.group(2) == "CLOSE")
    task = _task_of(reports, stage_id)
    if task is None:
        return (f"ОШИБКА: этап {stage_id} не открыт в {_reports_path()}. "
                "Сначала stage_open — задание фиксируется ДО исполнения.")
    query = _build_query(task, result)
    answer = _ask_critic(query)
    block = (
        f"\n## STAGE {stage_id} — CLOSE — {_ts()}\n"
        f"РЕЗУЛЬТАТ (заявлен исполнителем):\n{result}\n\n"
        f"ЗАПРОС КРИТИКУ:\n{query}\n\n"
        f"ОТВЕТ КРИТИКА:\n{answer}\n"
    )
    _append(block)
    cycle = f" (цикл {n_closed + 1})" if n_closed else ""
    return (f"ОТВЕТ КРИТИКА по этапу {stage_id}{cycle} (сохранён в {_reports_path()}):\n{answer}\n\n"
            "Если критик указал пропуски — дополни исполнение и закрой этап "
            "новым stage_close. Приёмка не здесь: закрытие этого этапа не является "
            "вердиктом успеха задачи.")


def handle_stage_list(_args: dict, **_kw) -> str:
    reports = _read_reports()
    if not reports:
        return "REPORTS.md пуст или не создан — открытых этапов нет."
    opens = [(m.group(1), m.group(3)) for m in _HEADER.finditer(reports) if m.group(2) == "OPEN"]
    open_ids = [oid for oid, _ in opens if _find_section(reports, oid, "CLOSE") is None]
    if not open_ids:
        return "Открытых незакрытых этапов нет."
    lines = [f"ОТКРЫТО ЭТАПОВ: {len(open_ids)}"]
    for oid in open_ids:
        m = next(h for h in _HEADER.finditer(reports) if h.group(1) == oid)
        task = _task_of(reports, oid) or ""
        lines.append(f"- {oid} (с {m.group(3)}): {task[:120]!r}")
    return "\n".join(lines)


TOOLS = [
    ("stage_open", {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Задание этапа своими словами — ЧТО должно быть сделано и как проверить полноту. Записывается ДО исполнения."},
            "title": {"type": "string", "description": "Короткое название этапа (опционально)."},
        },
        "required": ["task"],
    }, handle_stage_open, "📋"),
    ("stage_close", {
        "type": "object",
        "properties": {
            "stage_id": {"type": "string", "description": "Идентификатор из stage_open."},
            "result": {"type": "string", "description": "Заявленный результат исполнения (что реально сделано, с путями/выводом)."},
        },
        "required": ["stage_id", "result"],
    }, handle_stage_close, "🔍"),
    ("stage_list", {
        "type": "object",
        "properties": {},
    }, handle_stage_list, "📑"),
]


def register(ctx) -> None:
    global _CTX
    _CTX = ctx
    for name, schema, handler, emoji in TOOLS:
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema,
                          handler=handler, emoji=emoji)
    log.info("honesty-gate: registered %d tools, reports=%s", len(TOOLS), _reports_path())
