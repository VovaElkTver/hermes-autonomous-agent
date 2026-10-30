"""write-dialog v8.1 — диалог записки как ЯДРЕННЫЙ ФОРК (stage001 п.4, Вл 30.10).

v8.1 (Вл 30.10, «такого не могло быть. просто НЕВОЗМОЖНО»): инвариант
ВИДИМОСТИ. Открытый диалог = видимый в комнате вопрос. Анонс пишется с
read-back подтверждением; не подтвердился — откат заморозки (запись не
исполнена, тихого невидимого диалога нет). Тик-watchdog перепроверяет
видимость активного вопроса и перепубликует потерянный; session-контур
промотируется в matrix, как только канал появился.

Модель (ToDo.md stage001 п.4 — карточного слоя, JSON-склада и RAM-состояния
НОЛЬ; состояние диалога = строка session_branches + её сообщения):

  * foreground-запись в память/скилл/защищённый файл перехватывается (wrap
    реестровых хендлеров + хук pre_tool_call + safety-net evaluate_gate) и
    ЗАМОРАЖИВАЕТСЯ ядреным примитивом: ``session_fork(name="wd:<key>")``
    (tools/session_fork_tool.py) — настоящая дочерняя сессия с ленивым
    overlay полной истории родителя (ветка видит весь трёд решений).
    Имя ветки канонически кладётся в meta_json строки branch (b1358a028a) —
    оно же immutably-имя консоли ``⎇ wd:<key>``.

  * ветка СООБЩАЕТ в комнату обычным текстом: точный текст записки, бюджет,
    обоснование агента, вердикт критика. Метку консоли наносит ЯДЕРНЫЙ
    ДЕКОРАТОР (gateway/branch_prefix.decorate_response на границе печати),
    плагин префикс вручную не печатает.

  * замороженный payload живёт в СООБЩЕНИИ ветки (assistant-реплика с
    блоком <wd-frozen>…</wd-frozen>) — не в JSON-файле и не в RAM.
    «правка: …» дописывает новое сообщение (последнее <wd-frozen> — истина).

  * решение человека = обычное сообщение в комнату. Предикат тика (тонкий
    watcher-поток, 5 с) читает ЛОГ MATRIX (аксиома Вл: единственный
    достоверный источник): GET /_matrix/client/v1/rooms/{room}/threads/{root}/
    events с фолбэком /messages; при E2EE — ещё и дешифрованный поток
    гейтвея (v7 паттерн _stream_thread_texts). Токены первой строки:
    «записать» / «не пиши» / «правка: <текст>».

  * исполнение замороженной записи — в РОДИТЕЛЕ с обходом гейта
    (voоружённый флаг + registry.dispatch); атомарная защёлка
    UPDATE…WHERE status='active' (db.set_branch_status) — ровно один
    исполнитель, двойного срабатывания нет.

  * окончание диалога = семантика ``session_join(strategy='note')``:
    детерминированная work-note в родителя (что решено, какой текст
    записан/отклонён и почему), без LLM-сжатия. «не пиши» — тот же
    закрытый цикл со strategy='discard' и note с причиной.

  * рестарт-безопасность: состояние — строка ветки + сообщения в state.db
    (внешней относительно процесса); install() пересообщает открытые ветки
    в тред («паркованный тёрн → переспрос всегда»).

  * деградированный контур (нет Matrix-канала — CLI/вебхук): ветка всё
    равно поднимается ядреным форком, вопрос кладётся в текущий тёрн, а
    РЕШЕНИЕ доказуется историей сессии родителя (stage000: «CLI-контур
    читать сессионный history»), не RAM-карточкой.

  * статистика: каждый переход — строка jsonl в
    ~/.hermes/events/write-dialog.jsonl (это журнал, не состояние).

Конфиг: memory/skills/files.write_dialog (вкл/выкл), files.protected_paths
(дефолт ~/.hermes/config.yaml, ~/.hermes/plugins), write_dialog.deciders
(Matrix MXID'ы решателей; дефолт [@vovak]), auxiliary.expert (критик).
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import threading
import time
from contextlib import suppress
from contextvars import ContextVar
from pathlib import Path
from typing import Any

logger = logging.getLogger("hermes.plugins.write_dialog")

WD_VERSION = "v8"
NAME_PREFIX = "wd:"                 # имя ветки = wd:<ключ> — метка консоли
FROZEN_OPEN = "<wd-frozen>"         # маркер замороженного payload'а в сообщении ветки
FROZEN_CLOSE = "</wd-frozen>"
MAX_BODY = 1900
_POLL_S = 5.0
_STATS_PATH = "events/write-dialog.jsonl"
_EXECUTE_TOKENS = ("записать", "запиши", "пиши", "execute")
_DISCARD_TOKENS = ("не пиши", "неписать", "забыть", "отклоняю", "discard")
_EDIT_PREFIXES = ("правка:", "текст:", "edit:")
_DEFAULT_DECIDERS = ("@vovak",)
_DEFAULT_PROTECTED = ("~/.hermes/config.yaml", "~/.hermes/plugins")

_ORIG: dict[str, Any] = {}
_RATIONALE: ContextVar[str] = ContextVar("wd_rationale", default="")
_EXEC_LOCK = threading.Lock()
_EXEC_ARMED: list = [0.0]


# --------------------------------------------------------------------------
# гейт-обход (исполнение замороженной записи ядром)
# --------------------------------------------------------------------------

def _arm_exec(seconds: float = 15.0) -> None:
    with _EXEC_LOCK:
        _EXEC_ARMED[0] = time.monotonic() + seconds


def _exec_armed() -> bool:
    with _EXEC_LOCK:
        return time.monotonic() < _EXEC_ARMED[0]


def _stats(event: str, **fields) -> None:
    try:
        from hermes_constants import get_hermes_home
        p = get_hermes_home() / _STATS_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": round(time.time(), 3), "event": event, "v": WD_VERSION,
               **{k: (v if isinstance(v, (int, float, bool, str)) or v is None else str(v))
                  for k, v in fields.items()}}
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------
# конфигурация
# --------------------------------------------------------------------------

def _cfg_get(subsys: str, key: str, default=None):
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        sec = cfg.get(subsys)
        if isinstance(sec, dict):
            return sec.get(key, default)
        return default
    except Exception:
        return default


def _on(subsys: str) -> bool:
    try:
        from utils import is_truthy_value
        return is_truthy_value(_cfg_get(subsys, "write_dialog", False), default=False)
    except Exception:
        return False


def _deciders() -> tuple[str, ...]:
    vals = _cfg_get("write_dialog", "deciders", None) or list(_DEFAULT_DECIDERS)
    return tuple(str(v).lower() for v in vals)


def _sender_is_decider(sender: str) -> bool:
    s = (sender or "").lower()
    if not s:                      # CLI-контур: у сессии нет MXID — говорит сам человек
        return True
    return any(s.startswith(d if d.startswith("@") else "@" + d) for d in _deciders())


def _protected_globs() -> tuple[str, ...]:
    vals = _cfg_get("files", "protected_paths", None)
    return tuple(str(v) for v in vals) if vals else _DEFAULT_PROTECTED


def _is_protected_path(path: str) -> bool:
    p = os.path.expanduser(str(path or ""))
    return any(g and g in p for g in _protected_globs())


def _subsystem_for(tool: str, args: dict) -> str | None:
    """Память/скилл/защищённый файл — или None (не наш вызов)."""
    if tool == "memory" and _on("memory"):
        return "memory"
    if tool == "skill_manage" and _on("skills"):
        return "skills"
    if tool in ("write_file", "patch") and _on("files") \
            and _is_protected_path(args.get("path") or args.get("file_path") or ""):
        return "files"
    return None


# --------------------------------------------------------------------------
# ключ, payload, бюджет, текст
# --------------------------------------------------------------------------

def _key_for(subsys: str, tool: str, args: dict) -> str:
    if subsys == "memory":
        return f"memory:{args.get('target') or 'memory'}"
    if subsys == "skills":
        name = str(args.get("name") or (args.get("operations") or [{}])[0].get("name") or "?")
        return f"skills:{name}"
    return f"file:{os.path.basename(str(args.get('path') or args.get('file_path')))}"


def _summary(subsys: str, tool: str, args: dict) -> str:
    if subsys == "memory":
        ops = args.get("operations")
        n = len(ops) if isinstance(ops, list) else 1
        return f"{args.get('action') or 'batch'} x{n} в память target={args.get('target') or 'memory'}"
    if subsys == "skills":
        return f"правка скилла '{args.get('name') or '?'}' ({args.get('action') or 'batch'})"
    p = str(args.get("path") or args.get("file_path") or "")
    return f"{tool} -> {p}"


def _new_text(args: dict) -> str:
    ops = args.get("operations")
    if isinstance(ops, list):
        return "\n---\n".join(str(op.get("content") or op.get("new_text") or op.get("old_text") or "")
                              for op in ops if isinstance(op, dict))
    return str(args.get("content") or args.get("new_string") or args.get("new_text")
               or args.get("patch") or "")


def _payload_of(subsys: str, tool: str, args: dict) -> dict:
    clean = {k: v for k, v in dict(args).items() if k != "rationale"}
    return {"tool": tool, "args": clean}


def _budget_note(subsys: str, text: str) -> str:
    if subsys == "memory":
        try:
            from hermes_constants import get_hermes_home
            p = get_hermes_home() / "memories" / "MEMORY.md"
            cur = p.stat().st_size if p.exists() else 0
            lim = int(_cfg_get("memory", "memory_char_limit", 9000) or 9000)
            return f"бюджет памяти: {cur}/{lim} → станет ~{cur + len(text)}/{lim}"
        except Exception:
            return ""
    if subsys == "skills":
        return "скилл: скилл-индекс растёт на строку описания; тело — только по загрузке"
    return ""


def _edit_slot(args: dict) -> bool:
    """Можно ли править одним текстом: одиночный content/new_string/single-op."""
    if isinstance(args.get("operations"), list):
        return len(args["operations"]) == 1 and isinstance(args["operations"][0], dict)
    return isinstance(args.get("content"), str) or isinstance(args.get("new_string"), str)


# --------------------------------------------------------------------------
# критик (вердикт обязателен в сообщении; молчание/timeout != допуск)
# --------------------------------------------------------------------------

def _critic_verdict(rec: dict) -> str:
    try:
        from hermes_cli.config import load_config
        exp = ((load_config() or {}).get("auxiliary") or {}).get("expert") or {}
    except Exception:
        exp = {}
    base, model = exp.get("base_url"), exp.get("model")
    if not base or not model:
        return "критик недоступен (auxiliary.expert не настроен) — допуск не дан"
    user = (
        "Намерение: зафиксировать новое знание. Куда: "
        f"{rec['subsystem']} ({rec['wd_key']}). Что именно записывается (точный текст):\n"
        f"{rec['text'][:MAX_BODY]}\n\n"
        f"Обоснование агента: {rec.get('rationale') or '(не предоставлено)'}\n"
        f"Бюджет: {rec.get('budget') or '—'}\n\n"
        "Верни ОДИН JSON: {\"verdict\":\"ок|правка|нет\",\"comment\":\"1-4 предложения по-русски\"}."
    )
    system = (
        "Ты — эксперт-критик формулировок знаний, которые агент собирается сохранить "
        "(память/скиллы/конфиг). ОцениВАЙ ТОЛЬКО ФОРМУЛИРОВКУ, без обоснований агента: "
        "1) точность и системность (без утрирования и пафоса); 2) понятность без "
        "контекста трёрна, в котором она родилась; 3) адресат — по месту ли это "
        "(факт в память / процедура в скилл / конфиг в файлы); 4) риск искажения "
        "поведения агента от этой записи. НЕ требуй определения «роли агента во "
        "флоте» и подобных внепроектных ролей. Верни ОДИН JSON без текста вокруг: "
        "{\"verdict\":\"ок|правка|нет\",\"comment\":\"...\"}."
    )
    try:
        import urllib.request
        body = json.dumps({
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.2,
            "max_tokens": 1500,
        }).encode()
        req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=min(float(exp.get("timeout_seconds", 60)), 22.0)) as r:
            data = json.loads(r.read())
        raw = data["choices"][0]["message"]["content"]
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            v = json.loads(m.group(0))
            return f"{v.get('verdict', '?')}: {v.get('comment', '')}".strip()
        return raw[:300]
    except Exception as exc:
        return f"критик недоступен ({type(exc).__name__}) — допуск не дан"


# --------------------------------------------------------------------------
# состояние: строка session_branches + сообщения ветки (не в meta, не в RAM)
# --------------------------------------------------------------------------

def _db():
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    return SessionDB(get_hermes_home() / "state.db")


def _fork_name(wd_key: str) -> str:
    return NAME_PREFIX + wd_key


def _branch_name(row: dict) -> str:
    """Имя из immutably-meta (тот же источник, что метка консоли ядра)."""
    try:
        return str((json.loads(row.get("meta_json") or "{}")).get("title") or "").strip()
    except Exception:
        return ""


def _wd_branches(db) -> list[dict]:
    rows = db._read_all(
        "SELECT * FROM session_branches WHERE status='active' AND meta_json LIKE ? "
        "ORDER BY created_at", (f'%"{NAME_PREFIX}%',))
    return [dict(r) for r in rows if _branch_name(dict(r)).startswith(NAME_PREFIX)]


def _find_wd_branch(db, wd_key: str) -> dict | None:
    want = _fork_name(wd_key)
    for row in _wd_branches(db):
        if _branch_name(row) == want:
            return row
    return None


def _core_fork(db, parent_sid: str, name: str) -> tuple[str, str]:
    """Ядренный session_fork (имя → meta_json строки branch, b1358a028a).

    Фолбэк зеркалит механику tools/session_fork_tool.py дословно — он нужен
    только когда родитель ещё без сообщений (пустой тёрн), где core-примитив
    отказывает по base_uid; во всём остальном идёт ядро."""
    try:
        from tools.session_fork_tool import session_fork as core_session_fork
        out = json.loads(core_session_fork(name=name, db=db, current_session_id=parent_sid))
        if out.get("ok"):
            return str(out["branch_id"]), str(out["child_session_id"])
    except Exception:
        logger.debug("write-dialog: core session_fork failed; mirroring", exc_info=True)
    branch_id = db.new_branch_id()
    child_id = f"{parent_sid}-f{db.new_branch_id()}"
    db.ensure_session(child_id, source="agent-fork", session_key=f"{parent_sid}:fork",
                      parent_session_id=parent_sid)
    db.create_branch_row(branch_id, parent_sid, child_id,
                         db.get_last_message_uid(parent_sid),
                         title=name[:80], created_by_turn=_current_msg_id())
    with suppress(Exception):
        db.set_session_title(child_id, name[:80])
    return branch_id, child_id


def _frozen_of(db, child_id: str) -> dict | None:
    """Последний замороженный payload ветки — из СООБЩЕНИЯ ветки (истина —
    последнее <wd-frozen>; «правка» дописывает новое, не переписывая старое)."""
    rows = db._read_all(
        "SELECT content FROM messages WHERE session_id=? AND content LIKE ? "
        "ORDER BY id DESC LIMIT 1", (child_id, f"%{FROZEN_OPEN}%"))
    if not rows:
        return None
    m = re.search(re.escape(FROZEN_OPEN) + r"(.*?)" + re.escape(FROZEN_CLOSE),
                  str(rows[-1]["content"]), re.DOTALL)
    if not m:
        return None
    try:
        rec = json.loads(m.group(1))
        return rec if isinstance(rec, dict) else None
    except Exception:
        return None


def _append_frozen(db, child_id: str, rec: dict, speech_labeled: str) -> None:
    body = {k: v for k, v in rec.items() if k != "speech"}
    content = (speech_labeled or "")[:4000] + "\n" + FROZEN_OPEN + \
        json.dumps(body, ensure_ascii=False) + FROZEN_CLOSE
    db.append_message(child_id, role="assistant", content=content)


# --- ядреный декоратор метки консоли (плагин сам «⎇ …:» не печатает) -----------

def _decorate(db, child_id: str, text: str) -> str:
    """Метку наносит ядро: gateway.branch_prefix.decorate_response (резолвит
    immutably-имя ветки по session_branches). Плагин к тексту префикс не
    добавляет; после закрытия ветки (verdict-строка) ядро метку уже не даёт —
    там берётся ядреный же форматтер format_branch_prefix, не самопал."""
    try:
        from gateway.branch_prefix import decorate_response, format_branch_prefix
        labeled = asyncio.run(decorate_response(db, child_id, text))
        if labeled and labeled != text:
            return labeled
        name = _branch_name_for_child(db, child_id)
        if name:
            return format_branch_prefix(name) + text
        return text
    except Exception:
        logger.debug("write-dialog: core label decorator unavailable", exc_info=True)
        return text


def _branch_name_for_child(db, child_id: str) -> str:
    try:
        row = db._read_all("SELECT meta_json FROM session_branches WHERE child_session_id=?",
                           (child_id,))
        return _branch_name(dict(row[0])) if row else ""
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Matrix: лог комнаты — единственный достоверный источник решений
# --------------------------------------------------------------------------

def _mx_creds() -> tuple[str, str]:
    hs = tok = ""
    try:
        from gateway.platforms._shared import get_scoped_secret
        hs = str(get_scoped_secret("MATRIX_HOMESERVER", "") or "")
        tok = str(get_scoped_secret("MATRIX_ACCESS_TOKEN", "") or "")
    except Exception:
        pass
    hs = hs or os.environ.get("MATRIX_HOMESERVER", "")
    tok = tok or os.environ.get("MATRIX_ACCESS_TOKEN", "")
    if not (hs and tok):
        try:
            from hermes_constants import get_hermes_home
            env = get_hermes_home() / ".env"
            if env.exists():
                for line in env.read_text(encoding="utf-8").splitlines():
                    k, sep, v = line.partition("=")
                    if not sep:
                        continue
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    hs = hs or (v if k == "MATRIX_HOMESERVER" else "")
                    tok = tok or (v if k == "MATRIX_ACCESS_TOKEN" else "")
        except Exception:
            pass
    return hs, tok


def _ch_for(room: str) -> dict | None:
    hs, tok = _mx_creds()
    return {"room": room, "hs": hs, "token": tok} if (room and hs and tok) else None


def _matrix_channel() -> dict | None:
    try:
        from gateway.session_context import get_session_env
        plat = get_session_env("HERMES_SESSION_PLATFORM") or os.environ.get("HERMES_PLATFORM", "")
        room = get_session_env("HERMES_SESSION_CHAT_ID") or os.environ.get("HERMES_SESSION_CHAT_ID", "")
        if str(plat) != "matrix" or not room:
            return None
        return _ch_for(room)
    except Exception:
        return None


def _mx(ch: dict, method: str, path: str, body: dict | None = None, tries: int = 2):
    import urllib.parse
    import urllib.request
    url = ch["hs"].rstrip("/") + path.format(room=urllib.parse.quote(ch["room"], safe=""))
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    last: Exception | None = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(
                url, data=data, method=method,
                headers={"Authorization": f"Bearer {ch['token']}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read() or b"{}")
        except Exception as exc:
            last = exc
            time.sleep(0.5)
    raise last  # type: ignore[misc]


def _mx_post_message(ch: dict, text: str, root: str | None) -> str:
    txn = f"wd-{os.urandom(5).hex()}"
    body = {"msgtype": "m.text", "body": text}
    if root:
        body["m.relates_to"] = {"rel_type": "m.thread", "event_id": root,
                                "is_falling_back": True,
                                "m.in_reply_to": {"event_id": root}}
    ev = _mx(ch, "PUT", f"/_matrix/client/v3/rooms/{{room}}/send/m.room.message/{txn}", body)
    return str(ev.get("event_id") or "")


def _mx_event_visible(ch: dict, event_id: str, root: str | None = None,
                      timeout_s: float = 8.0) -> bool:
    """Read-back события в лог комнаты (лог — источник истины)."""
    from urllib.parse import quote as _q
    if not event_id:
        return False
    deadline = time.time() + timeout_s
    while True:
        try:
            if root and root != event_id:
                chunk = _mx(ch, "GET",
                            f"/_matrix/client/v1/rooms/{{room}}/threads/{_q(root, safe='')}/events?limit=100",
                            tries=1).get("chunk", [])
                if any(ev.get("event_id") == event_id for ev in chunk):
                    return True
            chunk = _mx(ch, "GET",
                        "/_matrix/client/v3/rooms/{room}/messages?dir=b&limit=150",
                        tries=1).get("chunk", [])
            if any(ev.get("event_id") == event_id for ev in chunk):
                return True
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(0.7)


def _mx_post_verified(ch: dict, text: str, root: str | None = None,
                      tries: int = 3) -> str:
    """Пост с read-back подтверждением. Не видно в логе — исключение.
    «Сообщили, но никто не увидел» отсюда structurally невозможно."""
    last = ""
    for _ in range(tries):
        try:
            ev = _mx_post_message(ch, text, root)
        except Exception:
            continue
        last = ev
        if ev and _mx_event_visible(ch, ev, root or ev):
            return ev
    raise RuntimeError(f"matrix-пост не подтверждён read-back (last={last or 'без event_id'})")


def _mx_thread_texts(ch: dict, root: str, since_ts: float, *,
                     allow_top_level=None) -> list[dict]:
    """Текстовые события треда позже since_ts из лога homeserver:
    GET /_matrix/client/v1/rooms/{room}/threads/{root}/events, фолбэк /messages.

    Разрешённость top-level реплик (без m.relates_to) решает `allow_top_level(body)`
    — см. _tick_branch: токен в основной ветке не законен, пока консоль комнаты
    не переключена в сессию этой ветки (Вл 30.10: «записать/не записывать в
    основной ветке не законно — рушится сама идея write-dialog»)."""
    from urllib.parse import quote as _q
    def _fallback() -> list:
        return _mx(ch, "GET",
                   "/_matrix/client/v3/rooms/{room}/messages?dir=f&limit=80").get("chunk", [])
    try:
        events = _mx(ch, "GET",
                     f"/_matrix/client/v1/rooms/{{room}}/threads/{_q(root, safe='')}/events?limit=100")
        chunk = events.get("chunk", [])
        if not chunk:
            # Synapse thread-index отстаёт (натура 30.10, bbaa0f713): threads-endpoint
            # отвечает 200, но пусто, пока индекс не догнал реплики — фолбэк /messages
            # обязан отрабатывать и при пустом успехе, не только при исключении.
            chunk = _fallback()
    except Exception:
        chunk = _fallback()
    out = []
    for ev in chunk:
        content = ev.get("content") or {}
        if content.get("msgtype") != "m.text":
            continue
        rel = content.get("m.relates_to") or {}
        if rel.get("rel_type") == "m.thread" and rel.get("event_id") != root:
            continue
        ts = (ev.get("origin_server_ts") or 0) / 1000.0
        if ts and ts > since_ts:
            body = str(content.get("body") or "")
            if rel.get("rel_type") != "m.thread" and ev.get("event_id") != root:
                # top-level основной ветки: законен только при консоли в ветке
                if not (allow_top_level and allow_top_level(body)):
                    continue
            out.append({"sender": str(ev.get("sender") or ""),
                        "body": body, "ts": ts})
    return out


def _stream_thread_texts(rec: dict, since_ts: float) -> list[dict]:
    """E2EE-путь (v7): в зашифрованной комнате REST отдаёт только
    m.room.encrypted — тред читается из дешифрованного потока гейтвея
    (sessions/messages в state.db). Отправитель — владелец сессии треда."""
    out: list[dict] = []
    try:
        db = _db()
        try:
            sid = db.find_session_by_origin(platform="matrix", chat_id=rec["room"],
                                            thread_id=rec["root"])
            if not sid:
                return []
            owner = ""
            with suppress(Exception):
                r = db._read_all("SELECT user_id FROM sessions WHERE id=?", (sid,))
                owner = str(r[0]["user_id"] or "") if r else ""
            rows = db._read_all(
                "SELECT timestamp, content FROM messages "
                "WHERE session_id=? AND role='user' AND timestamp>? ORDER BY timestamp",
                (sid, since_ts))
            for r in rows:
                body = str(r["content"] or "")
                if body.startswith("[work-note from branch") or "[CONTEXT COMPACTION" in body[:60] \
                        or body.startswith("⎇"):
                    continue
                out.append({"sender": owner, "body": _strip_mentions(body),
                            "ts": float(r["timestamp"])})
        finally:
            db.close()
    except Exception:
        logger.debug("write-dialog: stream thread read failed", exc_info=True)
    return out


def _strip_mentions(body: str) -> str:
    # Element вставляет «[Replying ...]» и метку «[Имя Фамилия]» — решение
    # ищем в последней строке без метки.
    lines = [l for l in (body or "").strip().splitlines() if l.strip()]
    if not lines:
        return ""
    last = lines[-1].strip()
    last = re.sub(r"^\[[^\]]{1,80}\]\s*", "", last)
    return last.strip()


_ENC_TTL: dict[str, tuple[float, bool]] = {}


def _room_is_encrypted(ch: dict) -> bool:
    if not ch or not ch.get("token"):
        return True
    now = time.time()
    hit = _ENC_TTL.get(ch.get("room", ""))
    if hit and now - hit[0] < 300:
        return hit[1]
    try:
        d = _mx(ch, "GET", "/_matrix/client/v3/rooms/{room}/state/m.room.encryption", tries=1)
        enc = bool(d.get("algorithm"))
    except Exception:
        enc = False  # 404 — комната не зашифрована, хватает REST
    _ENC_TTL[ch.get("room", "")] = (now, enc)
    return enc


def _session_chat_of(db, session_id: str) -> str:
    try:
        row = db._read_all("SELECT chat_id FROM sessions WHERE id=?", (session_id,))
        return str(row[0]["chat_id"] or "") if row else ""
    except Exception:
        return ""


def _session_decision_msgs(db, parent_sid: str, since_ts: float) -> list[dict]:
    """CLI/деградированный контур: решение доказывается историей сессии
    родителя (stage000: «CLI-контур читать сессионный history»)."""
    out: list[dict] = []
    try:
        row = db._read_all("SELECT user_id FROM sessions WHERE id=?", (parent_sid,))
        owner = str(row[0]["user_id"] or "") if row else ""
        rows = db._read_all(
            "SELECT timestamp, content FROM messages "
            "WHERE session_id=? AND role='user' AND timestamp>? ORDER BY timestamp",
            (parent_sid, since_ts))
        for r in rows:
            body = str(r["content"] or "")
            if body.startswith("[work-note from branch") or body.startswith("⎇") \
                    or "[CONTEXT COMPACTION" in body[:60]:
                continue
            out.append({"sender": owner, "body": _strip_mentions(body),
                        "ts": float(r["timestamp"])})
    except Exception:
        logger.debug("write-dialog: session history read failed", exc_info=True)
    return out


# --------------------------------------------------------------------------
# речь ветки (сообщает, не спрашивает)
# --------------------------------------------------------------------------

def _speech(rec: dict, note: str = "") -> str:
    t = str(rec["text"])
    t = t[:MAX_BODY] + ("…" if len(t) > MAX_BODY else "")
    parts = [note or f"Считаю нужным сохранить: {rec['summary'].lower()} ({rec['subsystem']}).",
             "",
             "Текст, который ляжет:",
             t,
             ""]
    if rec.get("budget"):
        parts.append(f"По бюджету: {rec['budget']}.")
    parts.append(f"Зачем: {rec.get('rationale') or 'обоснования не дал'}.")
    parts.append(f"Критик: {rec.get('critic') or 'молчал — это не допуск'}.")
    parts.append("Решение — обычным сообщением сюда: «записать», «не пиши» или "
                 "«правка: <новый текст>». Возражения и вопросы — тоже сюда.")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# открытие ветки (заморозка)
# --------------------------------------------------------------------------

def _current_msg_id() -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_MESSAGE_ID") or ""
    except Exception:
        return ""


def _current_session_id() -> str:
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_ID") or ""
    except Exception:
        return ""


def _open_branch(rec: dict) -> dict | None:
    """Ядренный session_fork + сообщениe ветки + анонс в комнату.
    None — ветку не поднять (нет родительской сессии)."""
    parent_sid = rec.get("parent_session_id") or _current_session_id()
    if not parent_sid:
        return None
    ch = _matrix_channel()
    db = _db()
    try:
        rec["parent_session_id"] = parent_sid
        branch_id, child_id = _core_fork(db, parent_sid, _fork_name(rec["wd_key"]))
        rec.update({"branch_id": branch_id, "child_session_id": child_id,
                    "ts0": time.time() + 0.01, "revisions": 0})
        speech = _speech(rec)
        # Заморозка ложится на диск ПЕРВОЙ и проверяется read-back: активная
        # строка без payload (случай wd:memory 30.10) стала невозможна.
        _append_frozen(db, child_id, rec, _decorate(db, child_id, speech))
        if _frozen_of(db, child_id) is None:
            raise RuntimeError("frozen-сообщение не читается обратно после append")
        if ch is not None:
            rec["room"] = ch["room"]
            rec["contour"] = "matrix"
            try:
                rec["root"] = _mx_post_verified(ch, _decorate(db, child_id, speech), None)
            except Exception as exc:
                # Анонс не виден в комнате — откат заморозки. Открытого диалога
                # без видимого вопроса больше не бывает (Вл 30.10: НЕВОЗМОЖНО).
                _stats("branch_post_failed", wd_key=rec["wd_key"], error=str(exc)[:200])
                with suppress(Exception):
                    db.append_message(child_id, role="assistant",
                                      content=f"Откат: анонс не виден в комнате ({str(exc)[:120]}).")
                    db.set_branch_status(branch_id, "joined", join_strategy="discard")
                return None
            _append_frozen(db, child_id, rec, _decorate(db, child_id, speech))  # root в записке
        else:
            rec["contour"] = "session"
            rec["room"] = _session_chat_of(db, parent_sid)
        _stats("branch_opened", wd_key=rec["wd_key"], subsystem=rec["subsystem"],
               branch=branch_id, contour=rec["contour"])
        return rec
    except Exception:
        logger.exception("write-dialog: branch open failed")
        return None
    finally:
        db.close()


def _re_issue(rec: dict, new_text: str, note: str) -> dict:
    """Перевыпуск с новым текстом (агентский перевызов и «правка: …»):
    дописывается НОВОЕ сообщение ветки с новым <wd-frozen>, анонс уходит
    заново; старое сообщение остаётся историей. Диск до решения не тронут."""
    payload = dict(rec["payload"])
    args = dict(payload.get("args") or {})
    if isinstance(args.get("content"), str):
        args["content"] = new_text
    elif isinstance(args.get("new_string"), str):
        args["new_string"] = new_text
    elif isinstance(args.get("operations"), list) and len(args["operations"]) == 1:
        op = dict(args["operations"][0])
        key = "content" if "content" in op else ("new_string" if "new_string" in op else "new_text")
        op[key] = new_text
        args["operations"] = [op]
    payload["args"] = args
    rec["payload"] = payload
    rec["text"] = new_text
    rec["budget"] = _budget_note(rec["subsystem"], _new_text(args))
    rec["critic"] = _critic_verdict(rec)
    rec["revisions"] = int(rec.get("revisions", 0)) + 1
    speech = _speech(rec, note=note)
    db = _db()
    try:
        child = _current_child(db, rec)
        _append_frozen(db, child, rec, _decorate(db, child, speech))
        if rec.get("root") and rec.get("room"):
            ch = _ch_for(rec["room"]) or {"room": rec["room"], "hs": "", "token": ""}
            try:
                _mx_post_message(ch, _decorate(db, child, speech), rec["root"])
            except Exception:
                logger.exception("write-dialog: announce repost failed")
    finally:
        db.close()
    _stats("branch_revised", wd_key=rec["wd_key"], revisions=rec["revisions"])
    return rec


def _current_child(db, rec: dict) -> str:
    """Текущий child ветки (он мог быть перепривязан к тред-сессии гейтвея)."""
    try:
        row = db._read_all("SELECT child_session_id FROM session_branches WHERE branch_id=?",
                           (rec["branch_id"],))
        if row:
            return str(row[0]["child_session_id"])
    except Exception:
        pass
    return str(rec.get("child_session_id") or "")


# --------------------------------------------------------------------------
# спуск (исполнение в родителе с обходом гейта) и закрытие (join-семантика)
# --------------------------------------------------------------------------

def _fire(rec: dict) -> tuple[str, Any]:
    from tools.registry import registry
    tool, args = rec["payload"]["tool"], rec["payload"]["args"]
    _arm_exec()
    try:
        out = registry.dispatch(tool, args)
    finally:
        with _EXEC_LOCK:
            _EXEC_ARMED[0] = 0.0
    try:
        parsed = json.loads(out) if isinstance(out, str) else dict(out)
        ok = bool(parsed.get("success", True)) and "error" not in parsed
    except Exception:
        ok, parsed = True, {}
    return ("ok" if ok else "core-refused"), out


def _post_verdict(rec: dict, text: str) -> None:
    if not (rec.get("root") and rec.get("room")):
        return
    ch = _ch_for(rec["room"]) or {"room": rec["room"], "hs": "", "token": ""}
    db = _db()
    try:
        body = _decorate(db, _current_child(db, rec), text)
    finally:
        db.close()
    try:
        _mx_post_verified(ch, body, rec["root"])
    except Exception:
        logger.exception("write-dialog: verdict post failed")
        _stats("verdict_post_failed", wd_key=rec.get("wd_key"))


def _execute(rec: dict, decision_body: str, decision_sender: str) -> None:
    db = _db()
    try:
        # атомарная защёлка: UPDATE…WHERE status='active' — ровно один исполнитель
        if not db.set_branch_status(rec["branch_id"], "joined", join_strategy="note"):
            return
        cls, out = _fire(rec)
        verdict = "записано дословно" if cls == "ok" else f"ядро отказало: {str(out)[:200]}"
        note = (f"[work-note from branch {_fork_name(rec['wd_key'])}] {verdict}. "
                f"Решение {decision_sender}: «{(decision_body or '')[:120]}». "
                f"Текст: {str(rec['text'])[:400]}")
        try:
            db.append_message(rec["parent_session_id"], role="user", content=note[:4000],
                              display_kind="work_note")
        except Exception:
            logger.exception("write-dialog: work-note append failed")
        _post_verdict(rec, verdict)
        _stats("branch_fired", wd_key=rec["wd_key"], outcome=cls)
    finally:
        db.close()


def _discard(rec: dict, decision_body: str, decision_sender: str) -> None:
    db = _db()
    try:
        if not db.set_branch_status(rec["branch_id"], "joined", join_strategy="discard"):
            return
        note = (f"[work-note from branch {_fork_name(rec['wd_key'])}] отклонено человеком "
                f"({decision_sender}): «{(decision_body or '')[:120]}». Диск не тронут.")
        try:
            db.append_message(rec["parent_session_id"], role="user", content=note[:4000],
                              display_kind="work_note")
        except Exception:
            logger.exception("write-dialog: work-note append failed")
        _post_verdict(rec, "отклонено, запись не исполнена.")
        _stats("branch_discarded", wd_key=rec["wd_key"])
    finally:
        db.close()


def _decide(body: str) -> str | None:
    first = (body or "").strip().split("\n", 1)[0].strip().lower().rstrip(".!«»\"' ")
    for p in _EDIT_PREFIXES:
        if first.startswith(p):
            return "edit"
    if any(first == t or first.startswith(t + " ") for t in _EXECUTE_TOKENS):
        return "execute"
    if any(first == t or first.startswith(t + " ") for t in _DISCARD_TOKENS):
        return "discard"
    return None


def _edit_body(body: str) -> str:
    first_lines = (body or "").strip().split("\n", 1)
    first = first_lines[0]
    for p in _EDIT_PREFIXES:
        if first.lower().startswith(p):
            rest = first[len(p):].strip()
            return rest or (first_lines[1].strip() if len(first_lines) > 1 else "")
    return (body or "").strip()


# --------------------------------------------------------------------------
# предикат тика: состояние стора, а не ждущий разговор
# --------------------------------------------------------------------------

def _bind_thread_session(db, row: dict, rec: dict) -> None:
    """Когда гейтвей завёл сессию для треда анонса, ветка перепривязывается на
    неё: метка консоли и overlay полной истории родителя дают ветке контекст
    беседы. Сообщения замороженного диалога переезжают вместе с ней (истина —
    сообщения, а не id)."""
    if not rec.get("root") or not rec.get("room"):
        return
    old_child = str(row.get("child_session_id") or "")
    try:
        real = db.find_session_by_origin(platform="matrix", chat_id=rec["room"],
                                         thread_id=rec["root"])
        if real and real != old_child:
            db._write_sql("UPDATE session_branches SET child_session_id=? WHERE branch_id=? AND status='active'",
                          (real, row["branch_id"]))
            db._write_sql("UPDATE messages SET session_id=? WHERE session_id=?", (real, old_child))
            _stats("branch_bound", wd_key=rec.get("wd_key"), child=real)
    except Exception:
        logger.debug("write-dialog: child bind failed", exc_info=True)


def wd_watch_tick(now_ts: float) -> None:
    """Предикат: в открытой ветке wd: есть замороженная запись и более новое
    сообщение решателя с токеном. Решения — из лога Matrix (аксиома), без
    канала — из истории сессии родителя."""
    _ensure_wraps()
    db = _db()
    try:
        branches = _wd_branches(db)
    finally:
        db.close()
    for row in branches:
        try:
            _tick_branch(row)
        except Exception as exc:
            logger.debug("write-dialog: branch tick failed %s: %r", row.get("branch_id"), exc)


def _console_in_branch(db, room: str, child_sid: str) -> bool:
    """Консоль комнаты переключена в сессию ветки (/resume wd) — тогда top-level
    реплика принадлежит ветке законно; иначе она из основной ветки и не законна."""
    try:
        rows = db._read_all(
            "SELECT entry_json FROM gateway_routing "
            "WHERE session_key GLOB '*matrix*:' || ?", (room,))
        import json as _json
        for r in rows:
            key = _json.loads(r["entry_json"]).get("session_key", "")
            if "$" in key.rpartition(room)[2] or not key.endswith(room):
                continue  # тред-ключ, не консоль комнаты
            if str(_json.loads(r["entry_json"]).get("session_id") or "") == child_sid:
                return True
    except Exception:
        logger.debug("write-dialog: console probe failed", exc_info=True)
    return False


# --- watchdog видимости (Вл 30.10: «такого не могло быть. просто НЕВОЗМОЖНО») ---
# Инвариант: активный wd-диалог существует, только пока его вопрос ВИДИМ в
# комнате. _VIS_OK/_VIS_REPOST — кэш частоты проверок, НЕ состояние (истина —
# строки session_branches + сообщения, аксиома stage001 п.4).

_VIS_OK: dict[str, float] = {}
_VIS_REPOST: dict[str, float] = {}


def _ensure_visible(db, row: dict, rec: dict) -> bool:
    """True — вопрос видим, можно читать решения. False — в этом тике решать
    нельзя (перепубликуем/промотируем; следующий тик увидит анонс)."""
    key = str(row["branch_id"])
    now = time.time()
    contour = rec.get("contour")
    if contour == "matrix" and rec.get("root") and rec.get("room"):
        if now - _VIS_OK.get(key, 0.0) < 300.0:
            return True
        if now - float(rec.get("ts0") or 0.0) < 120.0:
            _VIS_OK[key] = now  # свежий анонс — synapse обязана его индексировать
            return True
        ch = _ch_for(rec["room"])
        if ch and _mx_event_visible(ch, str(rec["root"]), timeout_s=3.0):
            _VIS_OK[key] = now
            return True
        if now - _VIS_REPOST.get(key, 0.0) < 60.0 or not ch:
            return False
        _VIS_REPOST[key] = now
        try:
            child = _current_child(db, rec)
            new_root = _mx_post_verified(
                ch, _decorate(db, child,
                              _speech(rec) + "\n(watchdog: прежний анонс не найден "
                              "в комнате — перепубликовано.)"), None)
        except Exception:
            logger.debug("write-dialog: watchdog repost failed", exc_info=True)
            return False
        rec["root"] = new_root
        rec["ts0"] = now + 0.01
        _append_frozen(db, child, rec, _decorate(db, child, _speech(rec)))
        _VIS_OK[key] = now
        _stats("branch_republished", wd_key=rec.get("wd_key"), branch=key)
        return False
    if contour == "session":
        ch = _ch_for(str(rec.get("room") or "")) or _matrix_channel()
        if ch is None:
            return True  # CLI/API-контур: решения из истории сессии, как раньше
        try:
            child = _current_child(db, rec)
            root = _mx_post_verified(ch, _decorate(db, child, _speech(rec)), None)
            rec.update({"contour": "matrix", "root": root, "room": ch["room"],
                        "ts0": time.time() + 0.01})
            _append_frozen(db, child, rec, _decorate(db, child, _speech(rec)))
            _stats("branch_promoted", wd_key=rec.get("wd_key"))
            return False
        except Exception:
            logger.debug("write-dialog: promotion post failed", exc_info=True)
            return True  # канал сломался — решения из истории читаем, не встаём
    return True


def _tick_branch(row: dict) -> None:
    db = _db()
    try:
        child = str(row["child_session_id"])
        rec = _frozen_of(db, child)
        if rec is None or not rec.get("branch_id"):
            return
        rec.setdefault("contour", "matrix" if rec.get("root") else "session")
        _bind_thread_session(db, row, rec)
        if not _ensure_visible(db, row, rec):
            return
        if rec.get("root") and rec.get("room"):
            rec["child_session_id"] = _current_child(db, rec)
            ch = _ch_for(rec["room"]) or {"room": rec["room"], "hs": "", "token": ""}
            since = float(rec.get("ts0") or 0)
            child_now = rec["child_session_id"]
            def _allow_top(body: str, _r=rec["room"], _c=child_now) -> bool:
                # токен из основной ветки законен только при консоли в ветке
                return (_decide(body) is not None
                        and _console_in_branch(db, _r, _c))
            try:
                msgs = _mx_thread_texts(ch, rec["root"], since,
                                        allow_top_level=_allow_top)
                if _room_is_encrypted(ch):
                    msgs += _stream_thread_texts(rec, since)
            except Exception as exc:
                logger.debug("write-dialog: thread read failed %s: %r", rec["branch_id"], exc)
                return
        else:
            msgs = _session_decision_msgs(db, str(row["parent_session_id"]),
                                          float(rec.get("ts0") or 0))
        for m in sorted(msgs, key=lambda x: x["ts"]):
            if not _sender_is_decider(m["sender"]):
                continue
            act = _decide(m["body"])
            if act is None:
                continue
            if act == "edit":
                rec["ts0"] = m["ts"] + 0.001
                _re_issue(rec, _edit_body(m["body"]), note="Правка принята — новая формулировка:")
                # продолжаем цикл по уже прочитанным сообщениям: следующее может быть решением
                continue
            if act == "execute":
                _execute(rec, m["body"], m["sender"])
            else:
                _discard(rec, m["body"], m["sender"])
            break
    finally:
        db.close()


class _WatchThread:
    """Тик-счётчик плагина (не watchdog ветки): один тонкий поток на все ветки.
    Состояние — в state.db, рестарт теряет максимум 5 секунд наблюдения."""

    _th: threading.Thread | None = None
    _stop = threading.Event()

    @classmethod
    def start(cls) -> None:
        if cls._th and cls._th.is_alive():
            return
        cls._stop.clear()
        cls._th = threading.Thread(target=cls._loop, name="write-dialog-watch", daemon=True)
        cls._th.start()

    @classmethod
    def stop(cls) -> None:
        cls._stop.set()

    @classmethod
    def _loop(cls) -> None:
        while not cls._stop.wait(_POLL_S):
            try:
                wd_watch_tick(time.time())
            except Exception:
                logger.exception("write-dialog: watch tick failed (loop survives)")


# --------------------------------------------------------------------------
# основной перехват
# --------------------------------------------------------------------------

def _dialog(subsys: str, tool: str, args: dict) -> str | None:
    """None = пропускай к ядру. Иначе — готовый JSON-ответ инструмента."""
    if _exec_armed():
        return None
    rat = str(args.get("rationale") or "").strip()
    if rat:
        _RATIONALE.set(rat)
    key = _key_for(subsys, tool, args)
    payload = _payload_of(subsys, tool, args)
    db = _db()
    try:
        active = _wd_branches(db)
        row = _find_wd_branch(db, key)
        existing = _frozen_of(db, str(row["child_session_id"])) if row else None
        first_frozen = _frozen_of(db, str(active[0]["child_session_id"])) if (active and not existing) else None
    finally:
        db.close()
    if existing:
        if json.dumps(existing["payload"], sort_keys=True) == json.dumps(payload, sort_keys=True):
            return json.dumps({"success": True, "dialog_open": True, "key": key,
                               "message": f"⎇ {_fork_name(key)}: диалог уже открыт в ветке "
                                          f"{existing.get('branch_id')}. Решение — сообщением в "
                                          "тред (записать / не пиши / правка: …)."},
                              ensure_ascii=False)
        if not _edit_slot(payload.get("args") or {}):
            return json.dumps({"success": True, "dialog_rejected": True, "key": key,
                               "message": f"⎇ {_fork_name(key)}: ветка открыта, а новый текст — "
                                          "multi-op пакет; правкой не покрывается. Закрой ветку "
                                          "(«не пиши» в треде) и перевызови."}, ensure_ascii=False)
        merged = dict(existing, payload=payload,
                      rationale=_RATIONALE.get() or existing.get("rationale", ""))
        _re_issue(merged, _new_text(args), note="Перевыпуск — новая формулировка:")
        return json.dumps({"success": True, "dialog_revised": True, "key": key,
                           "message": f"⎇ {_fork_name(key)}: правка принята, ветка перевыпущена; "
                                      "диск до решения не тронут."}, ensure_ascii=False)

    if active:
        # WD — состояние: один открытый диалог, всё остальное write-стоит
        cur = first_frozen or {}
        cur_key = cur.get("wd_key") or _branch_name(active[0]).removeprefix(NAME_PREFIX)
        # Перед блоком — форсируем видимость вопроса в комнате (watchdog):
        # блокировать запись чужим невидимым диалогом невозможно.
        try:
            _vdb = _db()
            try:
                _VIS_OK.pop(str(active[0]["branch_id"]), None)
                _ensure_visible(_vdb, active[0], cur)
            finally:
                _vdb.close()
        except Exception:
            logger.debug("write-dialog: force-visible failed", exc_info=True)
        return json.dumps({"success": False, "dialog_state": True, "blocked_by": cur_key,
                           "message": f"⎇ {_fork_name(str(cur_key))}: состояние открыто — запись "
                                      "ждёт решения в ветке. Вопрос виден в комнате (watchdog "
                                      "перепубликует его в течение минуты, если нет); эта запись "
                                      "не исполнена (перевызови после закрытия)."},
                          ensure_ascii=False)

    text = _new_text(args)
    base = {"wd_key": key, "subsystem": subsys,
            "summary": _summary(subsys, tool, args), "text": text,
            "budget": _budget_note(subsys, text), "payload": payload,
            "rationale": _RATIONALE.get()}
    base["critic"] = _critic_verdict(base)
    opened = _open_branch(base)
    if opened is not None:
        if opened["contour"] == "matrix":
            return json.dumps({"success": True, "dialog_open": True, "key": key,
                               "branch_id": opened["branch_id"],
                               "message": f"⎇ {_fork_name(key)}: запись заморожена в ветке "
                                          f"{opened['branch_id']} (анонс в комнате); основной трёд "
                                          "свободен."}, ensure_ascii=False)
        if not _live_channel():
            return json.dumps({"success": False, "dialog_refused": True,
                               "message": "Write-dialog: нет ни Matrix-канала, ни живого "
                                          "человека в трёрне — тихая запись отменена (гейт Вл "
                                          "28.10). Повтори при человеке."}, ensure_ascii=False)
        return json.dumps({"success": True, "dialog_asked": True, "key": key,
                           "branch_id": opened["branch_id"],
                           "message": "Ветка открыта, Matrix-канала нет — реши в этом чате: "
                                      "«записать» / «не пиши» / «правка: …». "
                                      + _speech(opened)}, ensure_ascii=False)
    return json.dumps({"success": False, "dialog_refused": True,
                       "message": "Write-dialog: ветку поднять не удалось (нет родительской "
                                  "сессии) — запись не исполнена."}, ensure_ascii=False)


_WRAP_NAMES = ("memory", "skill_manage", "write_file", "patch")


def _ensure_wraps() -> None:
    """Инструменты регистрируются позже плагина — дооборачиваем по тик-счётчику."""
    for name in _WRAP_NAMES:
        try:
            _wrap(name)
        except Exception:
            logger.debug("write-dialog: wrap %s retry failed", name, exc_info=True)


def _patch_rationale_schema(entry) -> None:
    try:
        schema = getattr(entry, "schema", None)
        if not isinstance(schema, dict):
            return
        props = schema.setdefault("parameters", {}).setdefault("properties", {})
        props.setdefault("rationale", {
            "type": "string",
            "description": "write-dialog (Вл 28.10): обязательное обоснование формулировки — "
                           "из каких фактов/решений трёрна она следует и почему должна жить "
                           "в постоянном контексте."})
    except Exception:
        logger.debug("write-dialog: rationale schema patch failed", exc_info=True)


def _wrap(name: str) -> None:
    from tools.registry import registry
    entry = registry.get_entry(name)
    if entry is None:
        return  # ещё не зарегистрирован — попробуем на следующем тике
    if name in ("memory", "skill_manage"):
        _patch_rationale_schema(entry)
    if getattr(entry.handler, "_wd_wrapped", False):
        return
    orig = entry.handler
    _ORIG[name] = orig

    @functools.wraps(orig)
    def handler(args, **kw):
        subsys = _subsystem_for(name, args if isinstance(args, dict) else {})
        if subsys is None:
            return orig(args, **kw)
        out = _dialog(subsys, name, args if isinstance(args, dict) else {})
        return orig(args, **kw) if out is None else out

    handler._wd_wrapped = True
    entry.handler = handler


def _live_channel() -> bool:
    """True если трёрн живёт в чате с человеком."""
    try:
        from gateway.session_context import get_session_env
        platform = (get_session_env("HERMES_SESSION_PLATFORM")
                    or os.environ.get("HERMES_PLATFORM", "")).strip().lower()
        if not platform or platform in {"webhook", "msgraph_webhook", "api_server"}:
            return False
        from tools.approval_context import _no_user_can_answer
        return not _no_user_can_answer()
    except Exception:
        return False


# --- safety-net для inline-вызовов ядра (write_approval.evaluate_gate) ----------

def _patch_write_approval() -> None:
    from tools import write_approval as wa
    if getattr(wa.evaluate_gate, "_write_dialog", False):
        return
    orig_eval = wa.evaluate_gate

    @functools.wraps(orig_eval)
    def evaluate_gate(subsystem, *, inline_summary: str = "", inline_detail: str = ""):
        cfg = {"memory": "memory", "skills": "skills", "files": "files"}.get(subsystem, subsystem)
        if (_on(cfg) and wa.current_origin() != "background_review" and not _exec_armed()
                and not _is_plugin_writer()):
            if not _live_channel():
                return wa.GateDecision(blocked=True,
                                       message="Write dialog is ON but there is no live "
                                               "human channel this turn — the write was NOT "
                                               "performed (silent writes are abolished, Вл "
                                               "28.10). Do it in a turn with the user present.")
            if not _RATIONALE.get():
                return wa.GateDecision(blocked=True,
                                       message="Write-dialog refused: обоснование обязательно "
                                               "(поле rationale). Ничего не записано.")
            return wa.GateDecision(blocked=True,
                                   message="Write-dialog: запись идёт через ветку — вызови "
                                           "инструмент (memory/skill_manage/write_file) и он "
                                           "заморозится в wd-ветку. Ничего не записано.")
        return orig_eval(subsystem, inline_summary=inline_summary, inline_detail=inline_detail)

    evaluate_gate._write_dialog = True
    wa.evaluate_gate = evaluate_gate


def _is_plugin_writer() -> bool:
    return False


# --------------------------------------------------------------------------
# tool write_dialog (обзор/ручное закрытие при доказуемой реплике человека)
# --------------------------------------------------------------------------

WRITE_DIALOG_SCHEMA = {
    "name": "write_dialog",
    "description": (
        "Manage open write-dialogs (frozen memory/skill/file writes). Each dialog IS a "
        "core fork branch (session_fork 'wd:<key>'): the frozen payload lives in the "
        "branch's messages, the human decides in the room ('записать'/'не пиши'/"
        "'правка: …'), and the per-tick predicate reads the decision from the Matrix "
        "log. This tool mirrors branch state and executes or discards a dialog by its "
        "semantic key — ONLY with a provable human decision (room log or session "
        "history); silence is not consent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "status", "execute", "discard", "sweep"]},
            "key": {"type": "string",
                    "description": "Semantic dialog key (memory:memory, skills:<name>, "
                                   "file:<name>). Optional when exactly one dialog is open. "
                                   "action=sweep force-closes branch rows whose frozen note is "
                                   "lost on disk (no key needed)."},
        },
        "required": ["action"],
    },
}


def _human_voice() -> str:
    msg_id = _current_msg_id()
    if not msg_id:
        return ""
    try:
        from hermes_constants import get_hermes_home
        import sqlite3
        db_path = get_hermes_home() / "state.db"
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            row = con.execute(
                "SELECT content FROM messages WHERE role='user' "
                "AND platform_message_id=? ORDER BY id DESC LIMIT 1", (msg_id,)).fetchone()
        finally:
            con.close()
        return str(row[0]) if row and row[0] else ""
    except Exception:
        return ""


def write_dialog(action: str = "list", key: str | None = None) -> str:
    db = _db()
    try:
        dialogs: dict[str, dict] = {}
        for row in _wd_branches(db):
            rec = _frozen_of(db, str(row["child_session_id"]))
            if rec and rec.get("wd_key"):
                rec["branch_id"] = rec.get("branch_id") or row["branch_id"]
                dialogs[rec["wd_key"]] = rec
            else:
                # Строка активна, записки нет (потеря сообщения) — не «ключ не
                # найден», а честная запись в list: диагноз + close=sweep.
                dialogs[f"broken:{str(row['branch_id'])[:8]}"] = {
                    "wd_key": f"broken:{str(row['branch_id'])[:8]}",
                    "branch_id": row["branch_id"], "root": None, "contour": "row-only",
                    "summary": "(записка ветки потеряна на диске — action=sweep)",
                    "revisions": 0}
    finally:
        db.close()
    if action == "sweep":
        swept = []
        db2 = _db()
        try:
            for row in _wd_branches(db2):
                if _frozen_of(db2, str(row["child_session_id"])) is None:
                    with suppress(Exception):
                        db2.append_message(
                            str(row["child_session_id"]), role="assistant",
                            content="Закрыто sweep'ом: frozen-записка ветки не найдена "
                                    "на диске — диалог был невозможным состоянием.")
                        _post_verdict({"root": row.get("root"), "room": row.get("room"),
                                       "wd_key": str(row["branch_id"])[:8]},
                                      "sweep: ветка закрыта (записка потеряна).")
                    if db2.set_branch_status(row["branch_id"], "joined",
                                             join_strategy="discard"):
                        swept.append(row["branch_id"])
        finally:
            db2.close()
        _stats("sweep", swept=len(swept))
        return json.dumps({"success": True, "swept": swept}, ensure_ascii=False)
    if action in ("list", "status"):
        out = {k: {"branch_id": v.get("branch_id"), "root": v.get("root"),
                   "contour": v.get("contour"), "summary": v.get("summary"),
                   "revisions": v.get("revisions", 0)}
               for k, v in dialogs.items()}
        return json.dumps(out, ensure_ascii=False)
    if key is None:
        if len(dialogs) != 1:
            return json.dumps({"success": False,
                               "error": "key required (открыт не один диалог)"},
                              ensure_ascii=False)
        key = next(iter(dialogs))
    rec = dialogs.get(key)
    if rec is None:
        return json.dumps({"success": False, "error": f"нет открытого диалога '{key}'"},
                          ensure_ascii=False)
    if rec.get("contour") == "row-only":
        return json.dumps({"success": False,
                           "error": "у этой ветки нет frozen-записки на диске — "
                                    "execute/discard невозможны, закрой action=sweep"},
                          ensure_ascii=False)
    voice = _human_voice()
    if not voice and rec.get("root") and rec.get("room"):
        try:
            ch = _ch_for(rec["room"])
            thr = [m for m in (
                (_mx_thread_texts(ch, rec["root"], float(rec.get("ts0") or 0)))
                + (_stream_thread_texts(rec, float(rec.get("ts0") or 0))
                   if ch and _room_is_encrypted(ch) else []))
                if _sender_is_decider(m["sender"])] if ch else []
            if thr:
                voice = max(thr, key=lambda m: m["ts"])["body"]
        except Exception:
            pass
    if not voice:
        return json.dumps({"success": False, "error":
                           "нет доказуемой реплики человека (ни в трёрне, ни в логе "
                           "комнаты). Молчание — не согласие."}, ensure_ascii=False)
    if action == "discard":
        _discard(rec, voice, "main-room")
        return json.dumps({"success": True, "discarded": key}, ensure_ascii=False)
    if action == "execute":
        _execute(rec, voice, "main-room")
        return json.dumps({"success": True, "executed": key}, ensure_ascii=False)
    return json.dumps({"success": False, "error": "нет такого действия"}, ensure_ascii=False)


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------

def _reannounce_open() -> None:
    """Рестарт-безопасность: ветка пережила процесс (state.db внешняя) —
    install() пересообщает открытые ветки в их треды (переспрос, не потеря)."""
    db = _db()
    try:
        for row in _wd_branches(db):
            rec = _frozen_of(db, str(row["child_session_id"]))
            if not rec:
                continue
            _stats("restart_reannounce", wd_key=rec.get("wd_key"), branch=row["branch_id"])
            if rec.get("root") and rec.get("room"):
                ch = _ch_for(rec["room"])
                if ch is None:
                    continue
                try:
                    _mx_post_verified(
                        ch, _decorate(db, str(row["child_session_id"]),
                                      "Гейтвей перезапущен, ветка жива — решение не получено. "
                                      "«записать» / «не пиши» / «правка: …»."),
                        rec["root"])
                    _VIS_OK[str(row["branch_id"])] = time.time()
                except Exception:
                    logger.debug("write-dialog: restart re-announce failed", exc_info=True)
                    _stats("restart_reannounce_failed", wd_key=rec.get("wd_key"))
            else:
                with suppress(Exception):
                    db.append_message(
                        str(row["child_session_id"]), role="assistant",
                        content="Гейтвей перезапущен, ветка жива — решение не получено "
                                "(записать / не пиши / правка: …).")
    except Exception:
        logger.exception("write-dialog: restart re-announce failed")
    finally:
        db.close()


def install() -> None:
    _patch_write_approval()
    _ensure_wraps()
    _WatchThread.start()
    _reannounce_open()
    logger.info("write-dialog v8: core-fork dialog installed")


def reload_plugin() -> None:
    _WatchThread.stop()


def pre_tool_call(tool_name: str = "", args=None, **kw) -> dict | None:
    """Канонический шов: agent-loop дёргает хук до dispatch на любом профиле.
    block-сообщение и есть ответ ветки; ядро запись не исполняет."""
    subsys = _subsystem_for(tool_name, args if isinstance(args, dict) else {})
    if subsys is None:
        return None
    out = _dialog(subsys, tool_name, args if isinstance(args, dict) else {})
    if out is None:
        return None
    try:
        msg = json.loads(out).get("message", out)
    except Exception:
        msg = out
    if _exec_armed():
        return None
    return {"action": "block", "message": msg}


def _tool_handler(args=None, **kw) -> str:
    # Контракт диспетчера: handler(dict_args) первым позиционным параметром,
    # НЕ kwargs. Прямая передача write_dialog рождала «action = весь dict» —
    # отсюда «нет такого действия» / «key required» (ошибка с v8.0, Вл 30.10).
    a = dict(args or {})
    a.update(kw)
    return write_dialog(action=str(a.get("action") or "list"), key=a.get("key"))


def register(ctx) -> None:
    ctx.register_tool(name="write_dialog", toolset="write_dialog",
                      schema=WRITE_DIALOG_SCHEMA, handler=_tool_handler,
                      description=WRITE_DIALOG_SCHEMA["description"])
    for hook_name, cb in (("pre_tool_call", pre_tool_call), ("post_tool_call", _post_stats)):
        try:
            ctx.register_hook(hook_name, cb)
        except Exception:
            logger.debug("write-dialog: hook %s unavailable", hook_name, exc_info=True)
    install()


def _post_stats(tool_name: str = "", **kw) -> None:
    if tool_name in _WRAP_NAMES and _exec_armed():
        _stats("exec_pass", tool=tool_name)
