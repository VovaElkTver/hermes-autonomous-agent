"""managed_refine — управляемый прогон /refine (ТЗ «Управляемый анализ», правки Вл 10.11).

Перехватывает ВЕСЬ бэкграунд-рефайн (ручной и авто-nudge, ТЗ §0 в редакции Вл
«перехватывать весь(!!!) бэкграунд рефайн») на chokepoint'е
``AIAgent._spawn_background_review_now``. Владелец на каждой стадии видит материал;
любой гейт ожидания владельца живёт максимум ``REFINE_GATE_TIMEOUT_S`` секунд
(вариант А, 10.11: молчание > 3 мин → ожидающие намерения записи дописываются в
``need2write.md`` штатным ``record_need2write``, прогон сохраняется как DEFERRED и
возобновим через ``/refine resume <id>``).

Машина состояний и персистентность — код, не промптная дисциплина (§0): каждый
переход пишет ``state.json`` в ``<hermes home>/refine_runs/<run_id>/``.

Запись при APPLY — только штатными путями: под administrator_mode намерение уходит
в need2write через те же ``frozen_memory_response``/``frozen_skill_result``
(с добавленной ссылкой на run_id), без него — прямой вызов хранилища темы /
``skill_manage``.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("gateway.refine_managed")

#: Молчание владельца на гейте дольше этого => отложить в need2write (вариант А, Вл 10.11).
REFINE_GATE_TIMEOUT_S = 180

#: Read-only набор инструментов форка фазы ANALYZE (форк предлагает, но не пишет).
READONLY_WHITELIST = frozenset({"skills_list", "skill_view", "read_file", "search_files"})

DEFAULT_TEMPLATE = (
    "You are a knowledge-curation reviewer. Analyse the conversation below and propose "
    "durable lessons worth saving.\n"
    "{specialty_context}"
    "Material — the conversation transcript:\n{material}\n\n{focus_block}\n"
    "End your reply with a fenced ```json block: a list of candidate cards, each\n"
    '{{"kind": "memory"|"skill", "topic": "<registered topic name or skill name>", '
    '"action": "create"|"patch", "content": "...", "old_text": null, '
    '"new_text": null, "rationale": "..."}}\n'
    'For skills use kind="skill": either action="create" (topic="create", content=SKILL.md body) '
    "or action=\"patch\" with old_text/new_text against an existing skill. "
    "Propose ONLY candidates whose target is listed above or an existing skill. "
    "Do not write anything yourself."
)


# --------------------------------------------------------------------------- config

def _cfg(key: str, default: Any) -> Any:
    try:
        from hermes_cli.config import load_config
        return load_config().get(key, default)
    except Exception:
        return default


def managed_refine_enabled() -> bool:
    """ТЗ §5: верхнеуровневый флаг инстанса; false = прежнее атомарное поведение."""
    return bool(_cfg("managed_refine", False))


def _specialty_slot() -> str:
    """Слот {specialty_context}: пустой конфиг → слот исчезает из промпта (T7)."""
    value = str(_cfg("specialty_context", "") or "").strip()
    return f"Specialty context (mark judgements accordingly): {value}\n\n" if value else ""


def _template() -> str:
    return str(_cfg("refine_prompt_template", None) or DEFAULT_TEMPLATE)


# --------------------------------------------------------------------------- persistence

def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def runs_root() -> Path:
    return _hermes_home() / "refine_runs"


@dataclass
class ManagedRun:
    run_id: str
    chat_key: str
    source: str                       # "manual" | "auto"
    focus: Optional[str]
    messages_snapshot: list
    review_memory: bool = False
    review_skills: bool = False
    task_cfg: Optional[dict] = None
    agent_ref: Any = None             # агент-родитель (публикация/спавн продолжения)
    publish_fn: Any = None            # платформо-независимая публикация (cold-сессии)
    agent_builder: Any = None         # сборка transient-агента для ANALYZE (cold-сессии)
    transient_agent: bool = False     # True: агента создали мы — после форка отпускаем клиенты
    stage: str = "PREPARE"
    cards: list = field(default_factory=list)      # валидированные карточки PROPOSE
    approved: dict = field(default_factory=dict)   # idx(str) -> "reject"|...
    history: list = field(default_factory=list)

    @property
    def dir(self) -> Path:
        return runs_root() / self.run_id

    def persist(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "run_id": self.run_id, "chat_key": self.chat_key, "source": self.source,
                "focus": self.focus, "review_memory": self.review_memory,
                "review_skills": self.review_skills, "stage": self.stage,
                "cards": self.cards, "approved": self.approved, "history": self.history,
            }
            tmp = self.dir / "state.json.tmp"
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.dir / "state.json")
        except Exception:
            logger.warning("managed_refine: cannot persist %s", self.run_id, exc_info=True)

    @staticmethod
    def load(run_dir: Path) -> ManagedRun:
        data = json.loads((Path(run_dir) / "state.json").read_text(encoding="utf-8"))
        return ManagedRun(
            run_id=data["run_id"], chat_key=data.get("chat_key") or "",
            source=data.get("source") or "manual", focus=data.get("focus"),
            messages_snapshot=[], review_memory=bool(data.get("review_memory")),
            review_skills=bool(data.get("review_skills")), stage=data.get("stage") or "PREPARE",
            cards=list(data.get("cards") or []), approved=dict(data.get("approved") or {}),
            history=list(data.get("history") or []))

    def note(self, event: str) -> None:
        self.history.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event})
        self.persist()


RUNS: dict[str, ManagedRun] = {}          # активный прогон на сессию
_GATES: dict[str, _Gate] = {}


def _gate(chat_key: str) -> _Gate:
    gate = _Gate(event=threading.Event())
    _GATES[chat_key] = gate
    return gate


@dataclass
class _Gate:
    event: threading.Event
    verdict: Optional[str] = None
    payload: Optional[str] = None         # поправка промпта при PREPARE

    def resolve(self, verdict: str, payload: Optional[str] = None) -> None:
        self.verdict, self.payload = verdict, payload
        self.event.set()


# --------------------------------------------------------------------------- helpers

def _publish(agent: Any, text: str) -> None:
    cb = getattr(agent, "background_review_callback", None)
    if callable(cb):
        try:
            cb(text)
        except Exception:
            logger.debug("managed_refine: callback publish failed", exc_info=True)
        return
    print_fn = getattr(agent, "_safe_print", None)
    if callable(print_fn):
        try:
            print_fn(text)
        except Exception:
            logger.debug("managed_refine: print publish failed", exc_info=True)


def _emit(run: ManagedRun, text: str) -> None:
    """Публикация материала прогона: в сессию (callback/печать) или через publish_fn cold-пути."""
    if getattr(run, "publish_fn", None) is not None:
        try:
            run.publish_fn(text)
        except Exception:
            logger.debug("managed_refine: publish_fn failed", exc_info=True)
        return
    _publish(run.agent_ref, text)


def _digest(messages_snapshot: list, head: int = 1200) -> str:
    lines = []
    for msg in messages_snapshot[-12:]:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "?"
        content = msg.get("content")
        if isinstance(content, list):
            content = " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
        text = str(content or "").strip().replace("\n", " ")
        if text:
            lines.append(f"{role}: {text[:300]}")
    return "\n".join(lines)[:head]


def _compile_prompt(run: ManagedRun, prompt_edit: Optional[str]) -> str:
    base = _template()
    if prompt_edit:                       # владелец замещает шаблонные слоты этого прогона (§2.1/T2)
        base = prompt_edit
    text = (base
            .replace("{material}", _digest(run.messages_snapshot))
            .replace("{focus_block}", f"Focus: {run.focus}" if run.focus else "")
            .replace("{specialty_context}", _specialty_slot()))
    topics = _topic_names()
    if topics:
        text += "\nRegistered memory topics (valid card targets):\n" + "\n".join(f"- {t}" for t in topics)
    return text


def _registry_path() -> Path:
    return _hermes_home() / "memories" / "topics.yaml"


def _topic_names() -> list[str]:
    try:
        from tools import memory_topic_gate as gate
        return [r.name for r in gate.load_registry(_registry_path())]
    except Exception:
        return []


def _registry_rule(topic_name: str):
    from tools import memory_topic_gate as gate
    for rule in gate.load_registry(_registry_path()):
        if rule.name == topic_name:
            return rule
    return None


def parse_cards(raw_text: str) -> list[dict]:
    """Достаёт последний ```json блок со списком карточек; толерантно к прозе вокруг."""
    if not raw_text:
        return []
    start = raw_text.rfind("```json")
    fence_len = len("```json")
    if start == -1:
        start = raw_text.rfind("```")
        fence_len = 3
    if start == -1:
        return []
    end = raw_text.find("```", start + fence_len)
    blob = raw_text[start + fence_len:end].strip() if end != -1 else raw_text[start + fence_len:].strip()
    try:
        data = json.loads(blob)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def validate_card(card: dict) -> tuple[bool, str]:
    """ТЗ §4: цель записи обязана существовать ДО показа карточки владельцу."""
    kind = card.get("kind")
    name = str(card.get("topic") or "").strip()
    if kind == "memory":
        if not name:
            return False, "memory-card without topic"
        if _registry_rule(name) is None:
            return False, f"topic '{name}' is not in the registry"
        if not str(card.get("content") or "").strip():
            return False, "memory-card without content"
        return True, ""
    if kind == "skill":
        action = str(card.get("action") or "").strip().lower()
        if action == "create":
            return True, ""
        if action == "patch":
            from tools.skill_manager_tool import _find_skill
            if not name:
                return False, "skill patch without name"
            if _find_skill(name) is None:
                return False, f"skill '{name}' does not exist (use action=create)"
            if not str(card.get("old_text") or "").strip():
                return False, "skill patch without old_text"
            return True, ""
        return False, f"unknown skill action {action!r}"
    return False, f"unknown card kind {kind!r}"


# --------------------------------------------------------------------------- interception

def intercept_spawn(agent: Any, *, messages_snapshot: list, review_memory: bool = False,
                    review_skills: bool = False, focus: Optional[str] = None,
                    task_cfg: Optional[dict] = None, explicit: bool = False,
                    prompt_override: Optional[str] = None,
                    resume_run_id: Optional[str] = None, chat_key: Optional[str] = None,
                    **_ignored) -> bool:
    """True — спавн потреблён управляемой машиной (форк сейчас не запускается)."""
    if not managed_refine_enabled():
        return False
    if prompt_override or resume_run_id:   # управляемое продолжение идёт мимо перехвата
        return False
    key = chat_key or _session_key(agent)
    if key in RUNS:
        _publish(agent, "[managed_refine] предыдущий прогон ещё не завершён — /refine status, /refine go или /refine stop.")
        return True
    run_id = f"refine_{time.strftime('%Y%m%d-%H%M%S')}_{uuid.uuid4().hex[:4]}"
    run = ManagedRun(
        run_id=run_id, chat_key=key, source="manual" if explicit else "auto",
        focus=(focus or None), messages_snapshot=messages_snapshot,
        review_memory=review_memory, review_skills=review_skills, task_cfg=task_cfg, agent_ref=agent)
    if not _begin_run(run):
        return False
    gate = _gate(run.chat_key)
    threading.Thread(target=_wait_prepare, args=(run, gate), daemon=True, name="managed-refine").start()
    return True


def intercept_cold(*, messages_snapshot: list, chat_key: str, publish: Any, builder: Any,
                   focus: Optional[str] = None, review_memory: bool = True,
                   review_skills: bool = True, task_cfg: Optional[dict] = None) -> bool:
    """Cold/local сессия: cached-агента нет; transient-агент для ANALYZE собирает ``builder``."""
    if not managed_refine_enabled():
        return False
    if chat_key in RUNS:
        publish("[managed_refine] предыдущий прогон ещё не завершён — /refine status или /refine stop.")
        return True
    run_id = f"refine_{time.strftime('%Y%m%d-%H%M%S')}_{uuid.uuid4().hex[:4]}"
    run = ManagedRun(
        run_id=run_id, chat_key=chat_key, source="manual", focus=(focus or None),
        messages_snapshot=messages_snapshot, review_memory=review_memory,
        review_skills=review_skills, task_cfg=task_cfg, agent_ref=None,
        publish_fn=publish, agent_builder=builder, transient_agent=True)
    if not _begin_run(run):
        return False
    gate = _gate(run.chat_key)
    threading.Thread(target=_wait_prepare, args=(run, gate), daemon=True, name="managed-refine").start()
    return True


def _begin_run(run: ManagedRun) -> bool:
    """Общий хвост intercept_*: каталог прогона, digest/prompt, публикация PREPARE."""
    try:
        run.dir.mkdir(parents=True, exist_ok=True)
        (run.dir / "digest.txt").write_text(_digest(run.messages_snapshot, head=8000), encoding="utf-8")
        (run.dir / "prompt_used.txt").write_text(_compile_prompt(run, None), encoding="utf-8")
    except OSError:
        logger.warning("managed_refine: cannot create run dir %s", run.dir, exc_info=True)
        return False
    RUNS[run.chat_key] = run
    run.note("prepared")
    header = "PREPARE (auto)" if run.source == "auto" else "PREPARE"
    _emit(run, (
        f"[refine {run.run_id}] {header}\n"
        f"Digest:\n{_digest(run.messages_snapshot)}\n\n"
        f"Prompt → {run.dir / 'prompt_used.txt'}\n"
        "Commands: /refine go · /refine <исправленный промпт> · /refine stop · /refine resume <id>"
    ))
    return True


def _session_key(agent: Any) -> str:
    return str(getattr(agent, "session_chat_id", "") or getattr(agent, "chat_id", "") or f"agent-{id(agent)}")


# --------------------------------------------------------------------------- stage workers

def _wait_prepare(run: ManagedRun, gate: _Gate) -> None:
    """Гейт PREPARE: го / поправка промпта / стоп / отложи; тишина 3 мин → need2write (вариант А)."""
    if not gate.event.wait(timeout=REFINE_GATE_TIMEOUT_S):
        _defer(run, f"owner silent {REFINE_GATE_TIMEOUT_S}s at PREPARE", intents=[{
            "kind": "refine-request",
            "summary": f"source={run.source} focus={run.focus!r}\n"
                       f"digest_head: {_digest(run.messages_snapshot, head=600)}"}])
        return
    if gate.verdict == "stop":
        run.stage = "STOPPED"
        run.note("stopped at PREPARE")
        RUNS.pop(run.chat_key, None)
        _emit(run, f"[refine {run.run_id}] остановлен на PREPARE.")
        return
    if gate.verdict == "postpone":
        run.stage = "DEFERRED_PREPARE"
        run.note("postponed at PREPARE")
        RUNS.pop(run.chat_key, None)
        return
    # go (+ возможная поправка промпта в payload); T2: поправка реально уезжает в ANALYZE
    _analyze(run, prompt_edit=gate.payload)


def _analyze(run: ManagedRun, prompt_edit: Optional[str] = None) -> None:
    agent = run.agent_ref
    final_prompt = _compile_prompt(run, prompt_edit)
    try:
        (run.dir / "prompt_used.txt").write_text(final_prompt, encoding="utf-8")
    except OSError:
        pass
    run.stage = "ANALYZE"
    run.note("analysis started")
    try:
        raw = _run_analysis_fork(agent, run, final_prompt)
    except Exception as exc:
        logger.warning("managed_refine: analysis fork failed: %s", exc, exc_info=True)
        run.stage = "FAILED"
        run.note(f"analysis failed: {exc}")
        RUNS.pop(run.chat_key, None)
        return
    try:
        (run.dir / "response_raw.txt").write_text(raw or "", encoding="utf-8")
    except OSError:
        pass
    _emit(run, f"[refine {run.run_id}] RAW VERDICT:\n{(raw or '(пусто)')[:4000]}")
    validated, rejected = [], []
    for card in parse_cards(raw or "")[:12]:
        ok, reason = validate_card(card)
        (validated if ok else rejected).append((card, reason))
    if rejected:
        _emit(run, "[managed_refine] отклонены до показа: " +
              "; ".join(f"{c.get('topic', '?')}: {r}" for c, r in rejected))
    if not validated:
        run.stage = "FAILED"
        run.note("no valid cards proposed")
        RUNS.pop(run.chat_key, None)
        return
    run.cards = [c for c, _r in validated]
    run.stage = "PROPOSE"
    run.note(f"proposed {len(run.cards)} cards")
    _emit(run, _render_cards(run))
    run.stage = "DISCUSS"
    _wait_discuss(run, _gate(run.chat_key))


def _wait_discuss(run: ManagedRun, gate: _Gate) -> None:
    if not gate.event.wait(timeout=REFINE_GATE_TIMEOUT_S):
        pending = [{"kind": f"card-{i}-{c.get('kind')}",
                    "summary": f"target={c.get('topic')}\n{(c.get('content') or '')[:300]}"}
                   for i, c in enumerate(run.cards, 1) if run.approved.get(str(i)) != "reject"]
        _defer(run, f"owner silent {REFINE_GATE_TIMEOUT_S}s at DISCUSS", intents=pending)
        return
    if gate.verdict == "stop":
        run.stage = "STOPPED"
        run.note("stopped at DISCUSS")
        RUNS.pop(run.chat_key, None)
        return
    if gate.verdict == "approve":
        _apply_cards(run)


def _apply_cards(run: ManagedRun) -> None:
    """APPLY: память — целевой тем store через MemoryStore._topic_add (минуя глобальный
    MEMORY.md по построению, ТЗ §4); навыки — штатный ``skill_manage``. Под
    administrator_mode обе ветки сводятся к штатным frozen-ответам (need2write + run_id)."""
    run.stage = "APPLY"
    from tools.fork_mode import administrator_mode_enabled, frozen_memory_response, frozen_skill_result
    admin = administrator_mode_enabled()
    results = []
    for i, card in enumerate(run.cards, 1):
        if run.approved.get(str(i)) == "reject":
            continue
        try:
            if card.get("kind") == "memory":
                rule = _registry_rule(str(card["topic"]))
                summary = f"run={run.run_id} card-{i} target=topic:{card['topic']}\n{(card.get('content') or '')[:400]}"
                if admin:
                    resp = frozen_memory_response(target=f"topic:{card['topic']}", op="managed_apply", summary=summary)
                else:
                    from tools.memory_tool_store import MemoryStore
                    resp = MemoryStore()._topic_add(rule, str(card.get("content") or ""))
            else:
                action = "create" if str(card.get("action")).lower() == "create" else "patch"
                name = "create" if action == "create" else str(card["topic"])
                if admin:
                    frozen_skill_result(
                        action, name,
                        f"run={run.run_id} card-{i} content={(card.get('content') or '')[:150]} "
                        f"old={(card.get('old_text') or '')[:100]}")
                    resp = {"success": True, "_frozen": True}
                elif action == "create":
                    from tools.skill_manager_tool import skill_manage
                    resp_raw = skill_manage(action="create", name=name, content=str(card.get("content") or ""))
                    resp = json.loads(resp_raw) if isinstance(resp_raw, str) else resp_raw
                else:
                    from tools.skill_manager_tool import skill_manage
                    resp_raw = skill_manage(action="patch", name=name,
                                            old_string=str(card.get("old_text") or ""),
                                            new_string=str(card.get("new_text") or card.get("content") or ""))
                    resp = json.loads(resp_raw) if isinstance(resp_raw, str) else resp_raw
            results.append(f"card-{i}: {resp}")
        except Exception as exc:
            results.append(f"card-{i}: error {exc}")
    run.stage = "APPLIED"
    run.note("applied: " + "; ".join(results)[:500])
    RUNS.pop(run.chat_key, None)
    _emit(run, f"[refine {run.run_id}] APPLY done:\n" + "\n".join(results))


def _render_cards(run: ManagedRun) -> str:
    parts = [f"[refine {run.run_id}] CANDIDATES:"]
    for i, card in enumerate(run.cards, 1):
        parts.append(
            f"--- card-{i} [{card.get('kind')}] target={card.get('topic')} ---\n"
            f"{(card.get('content') or '')[:600]}\nrationale: {card.get('rationale', '')}")
    parts.append("Commands: /refine approve · /refine reject N · /refine stop · /refine status")
    return "\n".join(parts)


def _defer(run: ManagedRun, reason: str, intents: list[dict]) -> None:
    """Таймаут гейта / явная отложка: намерения записи → need2write, прогон возобновим."""
    from tools.fork_mode import record_need2write
    run.stage = "DEFERRED_" + run.stage.split("_")[0]
    run.note(f"deferred: {reason}")
    path = None
    for intent in intents:
        path = record_need2write(intent["kind"], f"({run.run_id}) {intent['summary']}")
    RUNS.pop(run.chat_key, None)
    tail = f" → {path}" if path else ""
    _emit(run, f"[refine {run.run_id}] отложено ({reason}); намерения записаны в need2write{tail}. "
               f"Возобновление: /refine resume {run.run_id}")


def _run_analysis_fork(agent: Any, run: ManagedRun, final_prompt: str) -> str:
    """ANALYZE: стандартный ревью-форк с read-only whitelist'ом (кодовый запрет записи
    форку) и захватом финального текста ответа через ``_managed_response_sink``."""
    from agent.background_review import (
        finish_background_review_run, prepare_background_review_run, spawn_background_review_thread)
    from tools.thread_context import propagate_context_to_thread

    if run.agent_builder is not None:
        agent = run.agent_builder()          # transient-агент cold-сессии (живёт одну фазу)
        run.agent_ref = agent
    review_run = prepare_background_review_run(agent)
    if review_run is None:
        raise RuntimeError("another review is already running")
    target, _p = spawn_background_review_thread(
        agent, run.messages_snapshot, review_memory=run.review_memory, review_skills=run.review_skills,
        focus=None, task_cfg=run.task_cfg, review_run=review_run, explicit=True,
        prompt_override=final_prompt)
    sink: list[str] = []
    agent._managed_readonly_whitelist = set(READONLY_WHITELIST)
    agent._managed_response_sink = sink

    def with_suppress_release(ag: Any) -> None:
        try:
            ag.release_clients()
        except Exception:
            logger.debug("managed_refine: release_clients failed", exc_info=True)

    def body() -> None:
        try:
            target()
        finally:
            finish_background_review_run(agent, review_run)
            if run.transient_agent:
                with_suppress_release(agent)

    th = threading.Thread(target=propagate_context_to_thread(body), daemon=True, name="managed-refine-fork")
    th.start()
    th.join(timeout=1800)
    cleanup_agent_flags(agent)
    return sink[-1] if sink else ""


def cleanup_agent_flags(agent: Any) -> None:
    for attr in ("_managed_readonly_whitelist", "_managed_response_sink"):
        try:
            delattr(agent, attr)
        except AttributeError:
            pass


# --------------------------------------------------------------------------- commands (/refine dispatch)

def handle_command(chat_key: Optional[str], sub: str, rest: str, agent: Any = None) -> Optional[str]:
    """Вход из диспетчера /refine: sub — первое слово аргументов, rest — остаток строки.
    None — команда потреблена управляемой машиной (дальше как новый прогон её не трактовать)."""
    chat_key = str(chat_key or "")
    gate = _GATES.get(chat_key)
    if sub in ("go", "иди"):
        if gate:
            gate.resolve("go")
            return None
        return "открытого гейта нет"
    if sub in ("stop", "стоп"):
        if gate:
            gate.resolve("stop")
            return None
        return "нет открытого гейта"
    if sub in ("postpone", "отложи"):
        if gate:
            gate.resolve("postpone")
            return None
        return "открытого гейта нет"
    if sub == "approve":
        if gate:
            gate.resolve("approve")
            return None
        return "открытого гейта DISCUSS нет"
    if sub.startswith("reject"):
        run = RUNS.get(chat_key)
        if run:
            idx = "".join(ch for ch in sub + " " + rest if ch.isdigit()).strip()
            run.approved[idx or "-"] = "reject"
            run.persist()
            return f"карточка {idx or '-'} снята"
        return "активного прогона нет"
    if sub == "status":
        run = RUNS.get(chat_key)
        return f"stage={run.stage} run={run.run_id}" if run else "активных прогонов нет"
    if sub == "resume":
        target = rest.strip().split()[0] if rest.strip() else ""
        d = runs_root() / target
        if not (d / "state.json").exists():
            return f"прогон {target or '?'} не найден"
        run = ManagedRun.load(d)
        run.agent_ref = agent
        if chat_key:
            run.chat_key = chat_key
        RUNS[run.chat_key] = run
        g = _gate(run.chat_key)
        if run.stage.startswith("DEFERRED_PREPARE"):
            run.stage = "PREPARE"
            run.persist()
            _emit(run, f"[refine {run.run_id}] RESUME из PREPARE:\n"
                       f"Digest:\n{_digest(run.messages_snapshot)}\nPrompt → {run.dir / 'prompt_used.txt'}")
            threading.Thread(target=_wait_prepare, args=(run, g), daemon=True).start()
        elif run.stage.startswith("DEFERRED_DISCUSS") or run.stage == "PROPOSE":
            run.stage = "DISCUSS"
            run.persist()
            _emit(run, _render_cards(run))
            threading.Thread(target=_wait_discuss, args=(run, g), daemon=True).start()
        else:
            return f"прогон {run.run_id} в стадии {run.stage} — resume неприменим"
        return f"возобновлён {run.run_id}"
    if gate is not None and gate.verdict is None:
        # свободный текст при открытом PREPARE-гейте = поправка промпта этого прогона (§2.1)
        gate.resolve("go", payload=(sub + " " + rest).strip())
        return None
    return None


__all__ = [
    "READONLY_WHITELIST",
    "REFINE_GATE_TIMEOUT_S",
    "ManagedRun",
    "handle_command",
    "intercept_cold",
    "intercept_spawn",
    "managed_refine_enabled",
    "parse_cards",
    "runs_root",
    "validate_card",
]
