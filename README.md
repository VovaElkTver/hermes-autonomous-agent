<p align="center">
  <img src="assets/banner-fork.png" alt="Hermes Autonomous" width="100%">
</p>

# Hermes Agent ☤
> [!IMPORTANT]
> **Hermes Autonomous** — a fork of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
> where the agent's authority is *granted, not assumed*. Upstream behavior is unchanged
> until you switch features on:
>
> - **Managed-autonomy controller** — tasks carry MATERIAL contracts that no model text can
>   approve: a human command routed by code *before* the agent turn («иди task-X» / `go task-X`)
>   readies a queued draft; autonomy grants are per-scope with epochs (pause/restart kills
>   stale authority); completion requires an independent pass-review bound to the artifact
>   hash — "worker said done" is never done; budgets and audited state transitions throughout.
>   Agent-facing tools (`ctl_status/enqueue/step_done/block`) answer with what the database
>   *actually stores* — read-back, not optimism. Flag: `controller.enabled` (off by default).
> - **Durable approvals** — pending approval answers survive gateway restarts (off by default).
> - **Agent orchestration** — `fork`/`join` conversation branches with work-note merge,
>   a supervisor plugin with judge-reviewed delegation, and fork guardrails (retry /
>   confirmation / delegation guards).
> - **Project-scoped tools** — lazy MCP tool loading per project via `.hermes/mcp-scope.yaml`.
> - **Persistent tmux sessions** — drive long-lived interactive terminals as a tool.
> - **One-line install of THIS fork** — Linux/WSL:
>   `bash <(curl -fsSL https://raw.githubusercontent.com/VovaElkTver/hermes-autonomous-agent/main/fork-install.sh)`
>   Windows: `iwr -useb https://raw.githubusercontent.com/VovaElkTver/hermes-autonomous-agent/main/fork-install.ps1 | iex`
>
> - **To-do intent layer** — `ctl_todo` holds intention-notes ("don't forget to pick this
>   up") above the obligations queue: no contract, invisible to step claims, and one
>   *materialize* operation turns an intention into a drafted contract (`controller.pump_cli todo …`).
> - **Instance identity & envelope** — each install carries `identity.name` / `identity.id`
>   in config (an external label, never a path); inter-agent messages and logs carry a
>   mandatory `from` / `to` / `about` envelope, so "who speaks about whom" is a resolved
>   field, not a guess from text.
>
> Mechanics: [`docs/controller.md`](docs/controller.md) · fork-only slash commands
> (`/for`, `/fork`, `/join`, `/att`, `/initiate-setup`): [`docs/controller.md#слэш-команды-этого-форка`](docs/controller.md#слэш-команды-этого-форка).

<p align="center">
  <a href="https://hermes-agent.nousresearch.com/">Hermes Agent</a> | <a href="https://hermes-agent.nousresearch.com/">Hermes Desktop</a>
</p>
<p align="center">
  <a href="https://hermes-agent.nousresearch.com/docs/"><img src="https://img.shields.io/badge/Docs-hermes--agent.nousresearch.com-FFD700?style=for-the-badge" alt="Documentation"></a>
  <a href="https://discord.gg/NousResearch"><img src="https://img.shields.io/badge/Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="https://github.com/NousResearch/hermes-agent/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License: MIT"></a>
  <a href="https://nousresearch.com"><img src="https://img.shields.io/badge/Built%20by-Nous%20Research-blueviolet?style=for-the-badge" alt="Built by Nous Research"></a>
  <a href="README.zh-CN.md"><img src="https://img.shields.io/badge/Lang-中文-red?style=for-the-badge" alt="中文"></a>
  <a href="README.ur-pk.md"><img src="https://img.shields.io/badge/Lang-اردو-green?style=for-the-badge" alt="اردو"></a>
  <a href="README.es.md"><img src="https://img.shields.io/badge/Lang-Español-orange?style=for-the-badge" alt="Español"></a>
</p>

**The self-improving AI agent built by [Nous Research](https://nousresearch.com).** It's the only agent with a built-in learning loop — it creates skills from experience, improves them during use, nudges itself to persist knowledge, searches its own past conversations, and builds a deepening model of who you are across sessions. Run it on a $5 VPS, a GPU cluster, or serverless infrastructure that costs nearly nothing when idle. It's not tied to your laptop — talk to it from Telegram while it works on a cloud VM.

Use any model you want — [Nous Portal](https://portal.nousresearch.com), OpenRouter, OpenAI, your own endpoint, and [many others](https://hermes-agent.nousresearch.com/docs/integrations/providers). Switch with `hermes model` — no code changes, no lock-in.

<table>
<tr><td><b>A real terminal interface</b></td><td>Full TUI with multiline editing, slash-command autocomplete, conversation history, interrupt-and-redirect, and streaming tool output.</td></tr>
<tr><td><b>Lives where you do</b></td><td>Telegram, Discord, Slack, WhatsApp, Signal, and CLI — all from a single gateway process. Voice memo transcription, cross-platform conversation continuity.</td></tr>
<tr><td><b>A closed learning loop</b></td><td>Agent-curated memory with periodic nudges. Autonomous skill creation after complex tasks. Skills self-improve during use. FTS5 session search with LLM summarization for cross-session recall. <a href="https://github.com/plastic-labs/honcho">Honcho</a> dialectic user modeling. Compatible with the <a href="https://agentskills.io">agentskills.io</a> open standard.</td></tr>
<tr><td><b>Scheduled automations</b></td><td>Built-in cron scheduler with delivery to any platform. Daily reports, nightly backups, weekly audits — all in natural language, running unattended.</td></tr>
<tr><td><b>Delegates and parallelizes</b></td><td>Spawn isolated subagents for parallel workstreams. In Desktop, choose their provider, model, and limits under Settings → Model → Subagents, then Apply the complete route. Write Python scripts that call tools via RPC, collapsing multi-step pipelines into zero-context-cost turns.</td></tr>
<tr><td><b>Runs anywhere, not just your laptop</b></td><td>Six built-in terminal backends — local, Docker, SSH, Modal, Daytona, and Vercel Sandbox — plus plugin backends such as Singularity/Apptainer. Daytona and Modal offer serverless persistence — your agent's environment hibernates when idle and wakes on demand, costing nearly nothing between sessions. Run it on a $5 VPS or a GPU cluster.</td></tr>
<tr><td><b>Research-ready</b></td><td>Batch trajectory generation, trajectory compression for training the next generation of tool-calling models.</td></tr>
</table>

---

## Проект hermes autonomous (elk) — описание и правила

**Область проекта:** автономный контур Hermes на нодах флота — managed-autonomy
controller и pump, durable approvals, dev-контур `@rtx5000` (комната отладки
`!yzPjsOSdMSfUfZKtVk`), политика записи в память. Это — дом всех текстов этого
направления.

**Правила ведения (Вл, 27.10):**
- Требования и приёмы проекта хранятся только здесь, в описании проекта (README/docs
  этого репо), а не в глобальной памяти экземпляров Hermes.
- Отдельные spec-артефакты не плодить: новый текст — разделом сюда. Родовые имена
  (`spec vN`, `plan`, `final`) без префикса проекта на флоте пересекаются.

**Состояние направления (срезы по датам):**

### thinking-enable (stage004) — ТЗ «автономность мышления: от включённого кода к живым строкам» (Вл 17.11)

**Слаг:** `thinking-enable` · **Исполнитель:** агент, ведущий форк (elk-контур) · **Заказчик/приёмка:** Вл
**База:** срез 16.11.2026, HEAD `eeb5ee52ea1`, ветка `elk/kanban-excision`, живой `~/.hermes/state.db`
**Формат ведения:** этот текст — раздел в `README.md` (раздел «Проект hermes autonomous (elk)») и запись
в `ToDo.md` как `stage004 thinking-enable`. Отдельный spec-файл не плодить (правило README:
«новый текст — разделом сюда»). Рабочие заметки — append-only в `journal/<дата>-thinking-enable.md`.

#### 0. Рамка (почему ТЗ про это)

Доктрина форка: «Ядро хранит условия рассуждения, решения принимает агент»
([docs/preface-autonomous-agent.md](docs/preface-autonomous-agent.md)).
Механизмы условий рассуждения написаны (5 таблиц `investigation_*` + хуки H1–H4, `ctl_todo`,
`ctl_followup`, `ctl_causal_link`, `run_validation`, приёмка по хешу артефакта). В бою они не
используются: **0 строк** во всех когнитивных таблицах при 25 151 строке транспортных
наблюдений и 7 115 рёбрах причинного графа. Значит задача не «придумать новое мышление», а
**подключить написанное и замкнуть обратные рёбра** — и перестать плодить новые оси.

Правила, которые запрещают уходить в сторону (нарушение = переделка, не «документированный
компромисс»):

| № | Правило | Источник |
|---|---|---|
| 1 | «Уже есть в коде» — не база, а кандидатура: сверить форму со словами контракта | cautionary §1 |
| 2 | Механизм — в горле процесса, поверхности — транспорты | cautionary §2 |
| 3 | Состояние — в ленте; состояние вне ленты = налог | cautionary §3 |
| 4 | Эпруве не бывает со стороны агента; правую половину решает приёмка Вл | cautionary §4 |
| 5 | «Работает» = доказано живым исполнением; иначе честно «есть, но не работает» | cautionary §5 |
| 6 | Костыль ликвидируется, а не объявляется избыточным | cautionary §6 |
| 7 | Молчание инструмента — данные: читать артефакты, а не интерпретировать | cautionary §7 |
| — | «Text instructions are not a reliability mechanism» | `AGENTS.md` § operating rules |

(cautionary = [docs/cautionary-autonomous-agent.md](docs/cautionary-autonomous-agent.md))

#### 1. Цель и определение готово

**Цель:** через 3 недели бою в живой БД экземпляра есть **ненулевые, осмысленные** строки в
когнитивных таблицах, и хотя бы одна задача дошла от `draft` до приёмки **без человеческой
команды в середине**, с машинным предикатом вместо вердикта «текст похож на успех».

**Готово** = выполнены S0–S3, метрики S0 собираются автоматически, и все числа раздела 6
улучшились относительно baseline-среза этого ТЗ. Ни один этап не считается сделанным на
«тесты зелёные»: зелёные тесты = существование кода (cautionary §5).

#### 2. Не делать (границы этапа)

- **Не** создавать новую подсистему, таблицу-ось или «механизм мышления» поверх неиспользуемых
  (урок write-dialog: 55 файлов на механизм, который клеем и был). Разрешённые изменения схемы:
  новые **колонки** в существующих `ctl_*`/`investigation_*` (миграций нет, `SCHEMA_SQL` +
  reconcile), никаких новых таблиц-осей.
- **Не** лечить ложные done текстом в промпте и **не** плодить блоки `# Execution discipline`.
- **Не** давать агенту право self-approve «успеха» (cautionary §4). Права на *когнитивные*
  действия — да (S2), права на *оценку успеха* — нет.
- **Не** тащить новые отчёты в «состояние вне ленты»: всё, что видит Вл, — сообщения в комнату/CLI.
- **Не** строить «автономное целегенерирование» до S0: без метрик это maintenance-epoch с нулём строк.
- **Не** трогать `hermes_events`, `tasks_kernel`, `honesty-gate`, `kanban`-остатки, `planning_multistage`
  как отдельную ось — их судьба решается в S4, до этого не развивать.
- Git: `origin` = VovaElkTver/* только; upstream read-only.

#### 3. Этапы

Порядок строгий. Каждый этап — отдельный коммит(ы) с префиксом `ops: thinking-enable S<n> …`,
append-строка в journal, и **машинная проверка** из колонки «проверка». Этапы S0–S1 не требуют
решений Вл; S2 требует одного (см. §5).

##### S0 — Метрики автономности (VIEW + суточный отчёт в ленту). 1 день.

Сейчас `evals/postmortem/forensics/*` измеряют токены/латентность/доставку; автономность не
измеряется нигде. Сначала — метр, потом «улучшения».

Горло: новый `scripts/autonomy_metrics.py` (read-only SQL к `state.db`, никакого нового состояния) +
one-shot выдача в ленту через штатный порт доставки (`controller/outbox.py`). Никаких новых
потоков/таймеров: вызов — из существующего тика как событие (правило «отложенное — событие, не
таймер»).

Метрики (все считаются из существующих таблиц, новых записей не нужно):

1. `completed` без человеческой команды между `draft` и приёмкой / все `completed`;
2. медиана шагов и времени до `needs_human` / `blocked`;
3. доля «done», у которых есть машинный предикат (`validation_cmd` прошёл) и непустой
   `criteria_covered` — ловец ложных done;
4. self-initiated wake vs human-initiated (по `ctl_event.origin` / `sessions.created_source`);
5. глубина pending-приёмки: открытые/закрытые записи реестра (правило «открытые не должны расти
   быстрее закрытых» измерялось один день 31.10 и умерло — возродить как метрику, не как обещание);
6. `count(investigation_*)`, `ctl_todo`, `ctl_followup`, `ctl_review` с `reviewer='agent'`;
7. цена одного человеческого вмешательства (терны/токены на задачу с вмешательством vs без).

Проверка:

```bash
python3 scripts/autonomy_metrics.py --db ~/.hermes/state.db --json | python3 -m json.tool >/dev/null
python3 scripts/autonomy_metrics.py --db ~/.hermes/state.db --markdown | head -20   # baseline в journal
```

Приёмочный признак этапа: цифры S0 напечатаны в ленту и записаны в journal как baseline, с
датой и с ссылкой на **копию-снимок** БД (`sqlite3 .backup` → `~/.hermes/snapshots/<ts>.db`).
Детерминированность проверяется на снимке, а не на живой базе: живой `state.db` меняется между
двумя прогонами одной минуты (`causal_links` 7 115 → 7 124, `transport_obs` 25 151 → 25 252 —
замерено дважды при написании ТЗ). При пустых когнитивных таблицах отчёт даёт нули, а не падение.

##### S1 — Подключить написанное (5 переключателей + гигиена тикера). 2–3 дня.

| # | Что | Горло | Проверка |
|---|---|---|---|
| S1.1 | Мыслительные инструменты в поверхности: toolset `sessions` (или новый `thinking` = `investigation` + `ctl_*` + `plan_multistage`) в `platform_toolsets.cli\|matrix` | `tools/investigation_tool.py:234`, `~/.hermes/config.yaml:319-351`, `model_tools.py:329-341` | рабочая проверка (реестр надо поднять, иначе `sessions` резолвится в `[]`): `python3 -c "from tools import registry; registry.discover_builtin_tools(); from toolsets import resolve_toolset as r; ts=set(r('sessions')); assert {'investigation','session_fork'} <= ts, ts; from hermes_cli.config import load_config as L; assert 'sessions' in (L().get('platform_toolsets') or {}).get('cli',[]), 'sessions не в поверхности'  # на 16.11 -> AssertionError, это и есть finding` |
| S1.2 | Автоподвязка наблюдения к пробе: при открытом investigation рантайм привязывает результат инструмента к последнему `dispatched`-пробу сессии; явный `probe_id` остаётся точным путём (снимает «модель должна помнить» — ту же ошибку, что `controller_task_id` в S1.4) | `agent/investigation_hooks.py:44-60` | новый тест в `tests/test_investigation_hooks.py`: результат без `probe_id` → `assessed=0`-строка с верным `probe_id` |
| S1.3 | Машинный предикат приёмки: вызвать `run_validation` в `verifying` **до** `review_fn`; починить форму (`acceptance` dict vs list); `validation_cmd` обязателен в draft-контракте, сгенерированном планом | `controller/workers.py:327-333`, `controller/model.py:98`, горло `controller/loop.py:207` | `python3 -m pytest -q tests/plugins/test_controller_loop.py` + регресс: `pass` судьи при failed `validation_cmd` → `revise` |
| S1.4 | Цена действия: проставить `controller_task_id` в dispatch-контексте (иначе бюджет по tool_calls тождественно False — тот же класс ошибки, что починенный 09.11 `budget_exhausted`); дефолт `budget_seconds` на автономный период | `plugins/controller/__init__.py:88-124`, `controller/workers.py:110-118` | тест: достигнутый лимит → `blocked` + alert в `ctl_event`, а не «тихий» продолженный ран |
| S1.5 | Два выключателя, убивающие честность и обучение: `verification.verify_on_stop: "auto"` (в живом конфиге секции нет → выключен) и `auxiliary.background_review.enabled: true` с опозданием в существующую idle-очередь `agent/review_idle_queue.py` | `hermes_cli/config_defaults.py:212-218` | доля финальных ответов со свежим read-back (метрика S0.3); шум нуджей считается отдельно и **не** является основанием выключить гейт (причина, по которой его вырубили, должна быть измерена, а не унаследована) |

S1.6 (гигиена, из остатков аудита 28.10, [docs/critiques/2026-10-28-ticker-pre-launch-audit.md](docs/critiques/2026-10-28-ticker-pre-launch-audit.md)):
`conn.rollback()` в `_guard` перед error-`heartbeat`; heartbeat на отдельном read-only-соединении;
внешний не-LLM проверитель `MAX(ts)` из `ctl_ticker_heartbeat` (systemd timer, не модель).
Проверка: тест, где `heartbeat` бросает `database is locked` → тред тикера жив (регесс на «тихую
смерть автономии»).

##### S2 — Права на мыслительные действия (не на мутации). 3–5 дней.

У агента ровно 4 инструмента (`plugins/controller/agent_tools.py:206-264`);
`followups.add()` вызывают только тесты, `ctl_todo` материализуется только из CLI
(`controller/pump_cli.py:36-114`). Отсюда — никто не будит агента «решить, что делать», и следствие
своего действия он назначить не может.

| # | Что | Примечание |
|---|---|---|
| S2.1 | Класс гранта `cognitive` вместо вечно `ordinary`: `barrier.check` вызывается всегда с `action_class="ordinary"` (`controller/barrier.py:38-45`), а `grant_approval` не вызывается никем (`controller/model.py:440` — весь `ctl_approval` декорация. Когнитивные действия (открыть investigation, гипотеза/проб/предсказание, `ctl_todo`, `ctl_followup`, приоритет draft/ready, read-only зонды) — разрешены всегда в рамках scope; MATERIAL-мутации — как сейчас, только с человека | Это **не** self-approve и снимает доктринальный тупик, из-за которого `docs/design/maintenance-epoch.md` завис в 4 вопросах к Вл: самовопрос не требует самодозвола, если он не MATERIAL |
| S2.2 | Инструменты `ctl_todo add/list/propose`, `ctl_followup add(reminder\|review)`, `ctl_reprioritize(head\|tail, basis)` — пишут в существующие таблицы, остаются «предложением» для очереди | мост к уже написанному `followups`-механизму (`controller/followups.py:8-18,89`) |
| S2.3 | Дефолт неопределённости: `retry_same_step` из clamp (`controller/failure_analysis.py:91-104`) → «повтор шага только при наличии свежего предсказания или изменённого контракта, иначе `needs_human` с названным недостающим знанием». Начать применять `modified_brief` (сейчас считается и выбрасывается) | отказ должен быть дешевле ложного done: сегодня `отказ = failed`, поэтому агенту выгодно объявить done |
| S2.4 | Последствия в контекст: перед сборкой брифа поднимать из `ctl_causal_link` подграф `about/causes` по задаче/проекту за последние N событий + последние `ctl_review.detail` (`controller/workers.py:264-291`, читатели графа сегодня shadow — `controller/causal.py:31-34`) | машинная версия cautionary §7; лечит «мой стоп+TRUNCATE породил вторую аварию» (08.11), «кем-то внешним снят interim-патч» (27.10) |
| S2.5 | Судья видит смысл, а не хвост: в `make_review` передавать `acceptance` + `facts` + diff/diffstat вместо `step.title` + `stdout_tail[-2000:]` (`controller/workers.py:251-306`; в `ctl_review` — колонка `criteria_covered` (покрытие критериев, а не уверенность). Форма — из `src/claims/{core,oracle}.py`: популяция вне кандидата, предикат заморожен, контрпример = отдельная строка отчёта | сейчас левое условие `completed` = «текст похож на успех» при zero-покрытии |
| S2.6 | Событие «решить, что делать»: wake-терн на `completed\|blocked\|needs_human` и на «очередь пуста + permission on» с материалом (`ctl_status` + `ctl_todo` + неоценённые наблюдения + прошлые `needs_human`). Выход — только `draft` / `reprioritize` / `noop(причина)`, где **`noop` = успех** | `noop` обязан быть ассерт-успешным: сегодня «отказ от действия» не ассертится нигде |

##### S3 — Стоп-условия, калибровка, риск. 1 неделя, по меркам из S0.

- **S3.1** Симметрия стопа архитектурно, не текстом: детерминированный гейт в существующем горле
  (`agent/turn_stop_gates.py:189` — N шагов без нового подтверждённого факта (ни одного `assessed=1`,
  ни одного изменившегося read-back) ⇒ новый проб либо `needs_human`). Сейчас H4 умеет пинать, но
  пинать не за что (0 наблюдений).
- **S3.2** Калибровка: сравнить `investigation_predictions` с `assess`-исходом, накопить точность
  самопрогноза по scope. Единственный правдоподобный путь к «агент знает, когда ему себе не
  верить»; сегодня нет ни одной величины, из которой это следует.
- **S3.3** Риск-предиктор до действия (один дешёвый aux-вызов) для класса «мутация вне workdir /
  `git push` / `rm` / рестарт own-сервиса», вердикт — в `ctl_event` и в бриф. Ловит инциденты
  журнала: git-коррупция основного репо (12.11), 87/240 рестартов за ночь (15.11/08.11),
  контекст 3.1M токенов за минуту до SIGTERM (15.11 — деградация была видна, датчика не было).
  Заодно: ни один контракт сегодня не ограничивает длину автономного периода.
- **S3.4** Убрать мёртвые declared-only механизмы или признать их: `memory_gate`, `preempt`,
  `receipts`, `disposition`, семантические поля контракта
  (`allowed_actions/stop_conditions/reclassification_rules/open_questions/hypotheses` — пишутся,
  не читаются: `controller/model.py:97-102`).
  По правилу §6: либо вписать в горло, либо удалить вместе с тестами. Не «задокументировать как известное ограничение».

##### S4 — Одна ось цели (решение Вл, не код). Обсуждать после S0–S3.

Сейчас пять осей «что делать дальше» с непересекающимися состояниями и разными «кто решает»:
`controller`-очередь (1 задача), `hermes_cli/goals.py` (0 состояний `goal:%` в `state_meta`),
`tasks_kernel/`, `hermes_events/` (1 строка в `events.db`), `planning_multistage` (0 вызовов) +
`honesty-gate` (0 вызовов `stage_open`, `provides_hooks: []`). Kanban вырезан (с crash-loop 16.11),
cron-инструментарий удалён, при этом 3 197 из 4 240 сессий кончились `startup_orphan_reap`.
Кандидат сварки: `ctl_task` = обязательство, `/goal` = исполняющий терн-цикл внутри одного `step`,
`investigation` = состояние рассуждения этого терна, `ctl_followup` = отложенные следствия;
остальное — ликвидировать по правилу §6.

#### 4. Baseline (срез 16.11, прод `~/.hermes/state.db`: 4 240 сессий, 192 407 сообщений)

Команда снятия (перезапускать перед каждым отчётом; расхождение baseline↔отчёт = инцидент):

> Снимок baseline **обязан быть с меткой времени и с пометкой «живая БД»**: экземпляр пишет
> непрерывно, и `causal_links`/`transport_obs` расходятся уже между двумя прогонами одной минуты
> (7 115 → 7 124 за время написания этого ТЗ). Не-ковидные метрики (когнитивные таблицы) —
> «0 → >0», они дрейфу не подлежат.

```bash
mkdir -p ~/.hermes/snapshots && S=~/.hermes/snapshots/$(date -u +%Y%m%dT%H%M%SZ).db
sqlite3 -readonly ~/.hermes/state.db ".backup '$S'"     # дальше мерить только снимок
sqlite3 -readonly "$S" <<'SQL'
select 'ctl_task', count(*) from ctl_task union all
select 'ctl_review', count(*) from ctl_review union all
select 'ctl_todo', count(*) from ctl_todo union all
select 'ctl_followup', count(*) from ctl_followup union all
select 'ctl_approval', count(*) from ctl_approval union all
select 'ctl_mem_item', count(*) from ctl_mem_item union all
select 'investigations', count(*) from investigations union all
select 'inv_observations', count(*) from investigation_observations union all
select 'causal_links', count(*) from ctl_causal_link union all
select 'transport_obs', count(*) from transport_observations;
select tool_name, count(*) from messages where tool_name in
 ('investigation','session_fork','plan_multistage','stage_open','ctl_step_done','ctl_enqueue','ctl_status')
 group by 1 order by 2 desc;
select count(*) from state_meta where key like 'goal:%';
SQL
```

| Показатель | 16.11 | Цель через 3 недели |
|---|---|---|
| `ctl_task` | 1 (`completed`) | ≥ 10 |
| `ctl_review` / с машинным предикатом | **0** / — | ≥ 8 / ≥ 5 |
| `investigations` / `observations` | **0** / **0** | ≥ 5 / ≥ 40 (с `assessed=1`) |
| `ctl_todo` / `ctl_followup` от агента | **0** / **0** | ≥ 10 / ≥ 5 |
| `ctl_approval` | **0** | > 0 или удалён механизм (S3.4) |
| вызовы `investigation` / `plan_multistage` / `stage_open` | 0 / 0 / 0 | > 0 / решение S4 / решение S4 |
| goal-состояний | **0** | решение S4 |
| задачи, дошедшие до приёмки без человека в середине | 0 | ≥ 3 |
| `ctl_mem_item` | **1** | > 0 или механизм удалён (S3.4) |

#### 5. Решения, которые нужны от Вл (без них S2 стоит)

1. **Класс `cognitive`: утвердить список разрешённых навсегда когнитивных действий** и где он
   живёт (реестр в коде vs таблица). Без этого S2.1/S2.2 не реализуемы, а доктринальный запрет
   «no model text can approve» блокирует и самовопрос (см. 4 открытых вопроса
   `docs/design/maintenance-epoch.md`, застрявших ровно здесь).
2. **`noop(причина)` как успех** — подтверждать, что «осмотрелся и осознанно ничего не сделал»
   не является отказом/`failed`.
3. **`verify_on_stop` и `background_review` на каких поверхностях включать** (CLI / matrix /
   worker): в живом конфиге не задано, по умолчанию выключено; измерение шума — после включения,
   не вместо.
4. **Машинный предикат обязателен для всех новых контрактов?** Тогда `pump_cli`/`/for` обязаны
   генерировать `validation_cmd`, а контракт без него — не уходит в `ready`.

#### 6. Ритм и отчётность

- Один этап = один заход до конца (не «напоминание на следующем бите»): конец захода —
  append в journal + метрики S0 + что не сделано. Ветки — `elk/thinking-enable-s<n>`, коммиты с
  префиксом `ops: thinking-enable S<n> …`; в `ToDo.md` этап закрывается только после приёмки Вл.
- Формат journal-секций — по [journal/README.md](journal/README.md):
  `## НАМЕРЕНИЕ` (дословно задание, без пересказа), `## ГРАНИЦА НАМЕРЕНИЯ`, `## РАУНД N — РЕЗУЛЬТАТ`,
  `## РАУНД N — ВЕРДИКТ КРИТИКА`, `## РАУНД N — НАМЕРЕНИЯ ПО ЗАМЕЧАНИЯМ`. Критик — внешний
  (critic-queue на `.100:8012`, паттерн будильника из скилла `critic-queue`), не самопроверка.
- Пакет критику: намерение + все предыдущие вердикты + намерения по замечаниям + новый результат.
- Каждый отчёт начинается с ограничений («A report starts with its limits», `AGENTS.md`),
  и в нём обязательно: *что не проверялось живым исполнением* — по cautionary §5 это помечается
  «есть, но не работает», а не «сделано».
- Права агента: read-only ко всем БД и логам; `kill/restart` own-сервиса только с явным
  риском-вердиктом S3.3; git-запись только в `origin`.

#### 7. Риски и стоп-условия ТЗ

| Риск | Митигание / стоп |
|---|---|
| Включённая поверхность инструментов не изменит поведение (модель не потянется к `investigation`) | Признак того, что дело не в инструментах, а в отсутствии события «решить, что делать»: S2.6 поднимается в приоритете над S3. Если к концу S2 `investigation_observations` = 0 при ненулевой поверхности — ТЗ возвращается Вл на пересмотр, а не обрастает новым гейтом |
| `verify_on_stop` вернёт шум нуджей, из-за которого его вырубили | Флаг `"auto"` (только interactive-кодовые поверхности), метрика «нуджей на терн» в S0.3, лимит `max_verify_nudges=3` не менять |
| Машинные предикаты станут новой вакуумной формой (гейт проходит, смысла нет) | Проверка S2.5: предикат обязан проверять **критерий приёмки**, а не «код компилируется»; контрпример = тест, где предикат зелёный, а `criteria_covered` пуст ⇒ `revise` |
| Разрастание агентной поверхности (ещё 3 инструмента = ещё 55 файлов) | Все S2-инструменты пишут в существующие таблицы и не имеют собственного состояния; приёмочный критерий: ни одного нового файла с `class`/`pending`-состоянием |
| S4 решится «потом» и оси останутся | S4 — единственный этап, который нельзя закрыть кодом; без него ТЗ не считается выполненным, даже если метрики S1–S3 зелёные |

## Quick Install

### Linux, macOS, WSL2

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash

For this fork's one-line install (Linux/WSL): `bash <(curl -fsSL https://raw.githubusercontent.com/VovaElkTver/hermes-autonomous-agent/main/fork-install.sh)`; Windows: `iwr -useb https://raw.githubusercontent.com/VovaElkTver/hermes-autonomous-agent/main/fork-install.ps1 | iex`
```

### Windows (native, PowerShell)

> **Heads up:** Native Windows runs Hermes without WSL — CLI, gateway, TUI, and tools all work natively. If you'd rather use WSL2, the Linux/macOS one-liner above works there too. Found a bug? Please [file issues](https://github.com/NousResearch/hermes-agent/issues).

Run this in PowerShell:

```powershell
iex (irm https://hermes-agent.nousresearch.com/install.ps1)
```

The source installer delegates Python 3.14, Node.js, npm, pnpm, ripgrep, FFmpeg,
and Python dependencies to PM. If Git is absent, it stages the verified Git
for Windows archive in Hermes' tool store. It does not replace your system Git.
See [installation methods](https://hermes-agent.nousresearch.com/docs/getting-started/installation)
for the separate MSIX/App Installer package and its update ownership.

> **Android / Termux:** A signed APT repository is available for aarch64 devices, with a `stable` channel (tagged releases) and a prerelease `canary` channel. The package includes Python, Node.js, and the TUI. Use the [Termux guide](https://hermes-agent.nousresearch.com/docs/getting-started/termux), not the desktop/server installer script.
>
> **Windows:** Native Windows is fully supported — the PowerShell one-liner above installs everything. If you'd rather use WSL2, the Linux command works there too. Native Windows install lives under `%LOCALAPPDATA%\hermes`; WSL2 installs under `~/.hermes` as on Linux.

After installation:

```bash
source ~/.bashrc    # reload shell (or: source ~/.zshrc)
hermes              # start chatting!
```

### Troubleshooting

#### Windows Defender or antivirus flags `uv.exe` as malware

If your antivirus (Bitdefender, Windows Defender, etc.) quarantines `uv.exe` from the Hermes `bin` folder (`%LOCALAPPDATA%\hermes\bin\uv.exe`), this is a **false positive**. The file is Astral's `uv` — the Rust Python package manager Hermes bundles to manage its Python environment. ML-based antivirus engines commonly flag unsigned Rust binaries that download and install packages.

**To verify your copy is authentic:**

```powershell
# Install GitHub CLI if needed
winget install --id GitHub.cli

# Login to GitHub
gh auth login

# Run verification
$uv = "$env:LOCALAPPDATA\hermes\bin\uv.exe"
$ver = (& $uv --version).Split(' ')[1]
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$zip = "$env:TEMP\uv.zip"
Invoke-WebRequest "https://github.com/astral-sh/uv/releases/download/$ver/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip -UseBasicParsing
gh attestation verify $zip --repo astral-sh/uv
Expand-Archive $zip "$env:TEMP\uv_x" -Force
(Get-FileHash "$env:TEMP\uv_x\uv.exe").Hash -eq (Get-FileHash $uv).Hash
```

If attestation says "Verification succeeded" and the last line prints `True`, you're good.

**To whitelist Hermes:**
- **Windows Defender:** Run PowerShell as Admin → `Add-MpPreference -ExclusionPath "$env:LOCALAPPDATA\hermes\bin"`
- **Bitdefender:** Add an exception in the Bitdefender console (Protection > Antivirus > Settings > Manage Exceptions)
- Whitelist the **folder**, not the file hash — Hermes updates `uv` and the hash changes every version

For more context, see the upstream Astral reports: [astral-sh/uv#13553](https://github.com/astral-sh/uv/issues/13553), [astral-sh/uv#15011](https://github.com/astral-sh/uv/issues/15011), [astral-sh/uv#10079](https://github.com/astral-sh/uv/issues/10079).

---

## Getting Started

```bash
hermes              # Interactive CLI — start a conversation
hermes model        # Choose your LLM provider and model
hermes tools        # Configure which tools are enabled
hermes config set   # Set individual config values
hermes config get   # Print individual config values
hermes gateway      # Start the messaging gateway (Telegram, Discord, etc.)
hermes setup        # Run the full setup wizard (configures everything at once)
hermes update       # Update to the latest version
hermes doctor       # Diagnose any issues
```

📖 **[Full documentation →](https://hermes-agent.nousresearch.com/docs/)**

---

## Skip the API-key collection — Nous Portal

Hermes works with whatever provider you want — that's not changing. But if you'd rather not collect five separate API keys for the model, web search, image generation, TTS, and a cloud browser, **[Nous Portal](https://portal.nousresearch.com)** covers all of them under one subscription:

- **300+ models** — pick any of them with `/model <name>`
- **Tool Gateway** — web search, image generation (FAL), text-to-speech (OpenAI), cloud browser (Browser Use), all routed through your sub. No extra accounts.

One command from a fresh install:

```bash
hermes setup --portal
```

That logs you in via OAuth, sets Nous as your provider, and turns on the Tool Gateway. Check what's wired up any time with `hermes portal info`. Full details on the [Tool Gateway docs page](https://hermes-agent.nousresearch.com/docs/user-guide/features/tool-gateway).

You can still bring your own keys per-tool whenever you want — the gateway is per-backend, not all-or-nothing.

---

## CLI vs Messaging Quick Reference

Hermes has two entry points: start the terminal UI with `hermes`, or run the gateway and talk to it from Telegram, Discord, Slack, WhatsApp, Signal, or Email. Once you're in a conversation, many slash commands are shared across both interfaces.

| Action                         | CLI                                           | Messaging platforms                                                              |
| ------------------------------ | --------------------------------------------- | -------------------------------------------------------------------------------- |
| Start chatting                 | `hermes`                                      | Run `hermes gateway setup` + `hermes gateway start`, then send the bot a message |
| Start fresh conversation       | `/new` or `/reset`                            | `/new` or `/reset`                                                               |
| Change model                   | `/model [provider:model]`                     | `/model [provider:model]`                                                        |
| Set a personality              | `/personality [name]`                         | `/personality [name]`                                                            |
| Retry or undo the last turn    | `/retry`, `/undo`                             | `/retry`, `/undo`                                                                |
| Compress context / check usage | `/compress`, `/usage`, `/insights [--days N]` | `/compress`, `/usage`, `/insights [days]`                                        |
| Browse skills                  | `/skills` or `/<skill-name>`                  | `/<skill-name>`                                                                  |
| Interrupt current work         | `Ctrl+C` or send a new message                | `/stop` or send a new message                                                    |
| Platform-specific status       | `/platforms`                                  | `/status`, `/sethome`                                                            |

For the full command lists, see the [CLI guide](https://hermes-agent.nousresearch.com/docs/user-guide/cli) and the [Messaging Gateway guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging).

---

## Documentation

All documentation lives at **[hermes-agent.nousresearch.com/docs](https://hermes-agent.nousresearch.com/docs/)**:

| Section                                                                                             | What's Covered                                             |
| --------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| [Quickstart](https://hermes-agent.nousresearch.com/docs/getting-started/quickstart)                 | Install → setup → first conversation in 2 minutes          |
| [CLI Usage](https://hermes-agent.nousresearch.com/docs/user-guide/cli)                              | Commands, keybindings, personalities, sessions             |
| [Configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration)                | Config file, providers, models, all options                |
| [Messaging Gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging)                | Telegram, Discord, Slack, WhatsApp, Signal, Home Assistant |
| [Security](https://hermes-agent.nousresearch.com/docs/user-guide/security)                          | Command approval, DM pairing, container isolation          |
| [Tools & Toolsets](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools)            | 40+ tools, toolset system, terminal backends               |
| [Skills System](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)              | Procedural memory, Skills Hub, creating skills             |
| [Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)                     | Persistent memory, user profiles, best practices           |
| [MCP Integration](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)               | Connect any MCP server for extended capabilities           |
| [Cron Scheduling](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)              | Scheduled tasks with platform delivery                     |
| [Context Files](https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files)       | Project context that shapes every conversation             |
| [Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)             | Project structure, agent loop, key classes                 |
| [Contributing](https://hermes-agent.nousresearch.com/docs/developer-guide/contributing)             | Development setup, PR process, code style                  |
| [CLI Reference](https://hermes-agent.nousresearch.com/docs/reference/cli-commands)                  | All commands and flags                                     |
| [Environment Variables](https://hermes-agent.nousresearch.com/docs/reference/environment-variables) | Complete env var reference                                 |

---

## Migrating from OpenClaw or another agent

If you're coming from OpenClaw, Claude Code, Codex CLI or Cursor, Hermes can import your settings, memories and skills (and, for OpenClaw, API keys when you ask for them).

**During setup:** `hermes setup` asks whether to import from another agent, pre-selects the ones it finds (`~/.openclaw`, `~/.claude`, `~/.codex`, `~/.cursor`), installs the official `agent-importers` plugin and runs the import with a preview first.

**Anytime after install:**

```bash
hermes plugins install agent-importers          # once per profile
hermes import-agent openclaw                    # interactive migration (full preset)
hermes import-agent openclaw --dry-run          # preview what would be migrated
hermes import-agent openclaw --migrate-secrets  # also bring allowlisted API keys
hermes import-agent claude-code                 # or codex / cursor
```

What gets imported from OpenClaw:

- **SOUL.md** — persona file
- **Memories** — MEMORY.md and USER.md entries
- **Skills** — user-created skills → `~/.hermes/skills/openclaw-imports/`
- **Command allowlist** — approval patterns
- **Messaging settings** — platform configs, allowed users, working directory
- **API keys** — allowlisted secrets (Telegram, OpenRouter, OpenAI, Anthropic, ElevenLabs), only with `--migrate-secrets`
- **TTS assets** — workspace audio files
- **Workspace instructions** — AGENTS.md (with `--workspace-target`)

`hermes claw migrate` still works as the older spelling. See the [OpenClaw migration guide](https://hermes-agent.nousresearch.com/docs/guides/migrate-from-openclaw) and [Import from Other Agents](https://hermes-agent.nousresearch.com/docs/user-guide/import-from-other-agents).

---

## Contributing

We welcome contributions! See the [Contributing Guide](https://hermes-agent.nousresearch.com/docs/developer-guide/contributing) for development setup, code style, and PR process.

Start with the [PM developer workflow](website/docs/reference/package-management.md#developer-workflow)
for activation, daily use, dependency changes, and leaving the environment.
[Development Setup](CONTRIBUTING.md#development-setup) covers the separate test environment and verification commands.

---

## Community

- 💬 [Discord](https://discord.gg/NousResearch)
- 📚 [Skills Hub](https://agentskills.io)
- 🐛 [Issues](https://github.com/NousResearch/hermes-agent/issues)
- 🔌 [computer-use-linux](https://github.com/avifenesh/computer-use-linux) — Linux desktop-control MCP server for Hermes and other MCP hosts, with AT-SPI accessibility trees, Wayland/X11 input, screenshots, and compositor window targeting.
- 🔌 [HermesClaw](https://github.com/AaronWong1999/hermesclaw) — Community WeChat bridge: Run Hermes Agent and OpenClaw on the same WeChat account.

---

## License

MIT — see [LICENSE](LICENSE).

Built by [Nous Research](https://nousresearch.com).
