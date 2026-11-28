# ToDo — намерения

## stage004 thinking-enable (Вл 17.11)

ТЗ «автономность мышления — от включённого кода к живым строкам»: текст ТЗ — раздел
«thinking-enable (stage004)» в README.md (раздел «Проект hermes autonomous (elk)»);
рабочие заметки — append-only journal/20261117-thinking-enable.md. Этапы S0–S3 ветками
`elk/thinking-enable-s<n>`, коммиты с префиксом `ops: thinking-enable S<n> …`.
Закрытие записи в этом файле — только после приёмки Вл (Вл 15.11: ToDo.md = намерения,
запись хода этапов в нём не ведётся — ход в journal).

=======
## Дешёвый калиброванный судья-каскад (возможный вариант, отложен Вл 18.11)

Взять из донора `leepokai/llm-prompt-techniques-on-jev` (аудит: ~/projects/donor-audits/20261118-jev-prompt-techniques.md): typed-вердикт (PASS|FAIL|…) с калиброванной вероятностью как первый дешёвый каскад перед дорогой моделью; применить к classify_outcome, claim-verify gate, verify_on_stop, L1-deaf-sensor. Попутно: вердикты формулировать enum/criteria-структурой (не прозой); GEPA не запускать при val < ~30 строк. Сам облачный Jev не берём — внешний API. Реализовать локально на ninfer. Статус: вариант отложен, мандата нет.

## Сканер сторонних скиллов как машинный gate (возможный вариант, отложен Вл 18.11)

Взять из донора `hashgraph-online/awesome-ai-plugins` (аудит форка: ~/projects/donor-audits/20261118-awesome-ai-plugins.md): `hol-guard` / `plugin-scanner verify --format json` (pipx, 0–100, pass ≥80 без critical/high; секреты, hooks, env-vars, hardcoded paths, shell-injection) как машинная check-команда wiki-gate перед INSTALL-VERBATIM чужого SKILL.md и для set_check в task_queue. Попутно: реестр `hol.org/api/v1/plugins` как discovery-канал для CLI-делегатов; контракт marketplace.json (install_url→нативный манифест) — форма для нашего skills-реестра. Registry Broker не берём — дублирует controller/A2A. Статус: вариант отложен, мандата нет.

## Ветка форка в заданную Matrix-комнату (/fork-to) (возможный вариант, отложено Вл 19.11)

Постановка Вл: раз ветка форка — отдельный контекст, интересует передача её вывода в конкретную Matrix-комнату, чтобы один контекст шёл двумя путями в разных диалогах.

Что показал разбор: примитивы уже в проде — `session_detached` + `SessionAuthority.name_lane` (комната «называет» любой разговор через /new|/resume|/branch|/handoff, `route_named`-анонс), уведомления следуют за роутом (`get_bound_active_branch`). Рабочий рецепт сегодня: `/fork имя` в комнате А → `/resume <branch-id>` в комнате Б → ветка живёт в Б, родитель остаётся в А, сшивка `/join` из любой точки.

Что осталось сделать: (1) e2e-тест «комната Б встаёт на ветку комнаты А» — кросс-роут по чужому id в бою не проверялся; (2) сахарная `/fork-to <комната> [имя]` = fork + name_lane одним ходом; (3) гарантия доктрины 03.11 (ветка обязана давать вывод всегда) при срыве роута — проверить detached-путь. Работа на отдельном /fork с worktree, мандата нет.

## Экспортёр трасс в ShareGPT (elk/trace-export-sharegpt) — доделать в отдельном /fork (отложено Вл 18.11)

Постановка Вла: добавить в lambda/hermes-agent-reasoning-traces «строки по нашим решениям в форке»; работу обещал вести в отдельном /fork, а не в диалоговой ветке.

Состояние (закоммичено 11122a72cd3, worktree ~/ha-sharegpt, ветка от origin/main, прод-дерево не тронута): экспортёр `scripts/trace_export_sharegpt.py` + 4 теста зелёные; пилот `--grep '[claim-verify]' --limit 3` выгружен (exported=3, ~3 МБ, scratch/elk-traces-pilot.jsonl).

Осталось: (1) линейный валидатор JSONL (мой текущий парсер вложенных вызовов квадратичный на 16-КБ блоках — вешает проверку); (2) вербатим-просмотр ~30 эпизодов человеком; (3) push ветки в origin по команде; (4) discovery-вывод вечерней разведки: отдельная таблица turn_meta почти не нужна — recall уже персистится в api_content, скилл-индекс — в system_prompts; реальный остаток: router_decision и considered-but-not-loaded.

## Слой 2 надзора для LoRA: replay-бенч + GEPA как evaluator (Вл 20.11, после вечернего baseline)

Постановка Вл: находить в наших логах признаки «метода тыка» и прикидывать контрфакт («что бы было, если бы сделали иначе») — сильный кандидат в датасет LoRA. Измерено по state.db (20.11): template→rework ~15k сырых эпизодов, A→B-переключений (тыкал в тулзу A, сработала B) ~2.1k, «тыки в A до успеха» ~1.3k. Сравнить обе гипотезы надзора — «сразу B» (слой 2a) и «сначала наблюдательный шаг, потом B» (2b); допускать, что победители разные по классам (таблица `класс × адаптер → first-action match`).

Роль GEPA по слову Вл: **только оценка**, промпты в этом проекте не меняем. GEPA-контур (фиксированная метка first-action на replay-эпизоде, Парето по классам, рефлексия по провалам как диагностика) — поверх замороженного системного промпта форка. Разметка проходит причинный гейт {agent/environment/external} из trace-стека (21.09) — environment-провалы не в разметку. узлы-менее-30-меток в карантин (GEPA-guard).

## Лестница песочниц для чужого кода (возможный вариант, отложен Вл 18.11)

Один узел — исполнение непроверенного чужого кода (build-probe доноров, install-probe внешних SKILL.md, кандидаты из реестров) — тремя ступенями доверия:
1. **LayerFS** (`Ephemeral-AI-Lab/layerfs`; аудит ~/projects/donor-audits/20261118-layerfs.md): FUSE-воркспейс mount→exec→commit→unmount над SQLite-стором с content-addressed дедупом; откат = unmount. Попутные конвенции: `.lww` (победил последний) для journal/watchlist, рекурсивный fork коммита как фундамент fork_explore. Не substrate: стор без durability (их README), API ломается каждый мажор, запись 0.31–0.40× против passthrough.
2. **docker (уже есть)** — дефолт против случайности; общие ядро/namespace, от враждебного импорта не защищает.
3. **Firecracker** (`firecracker-microvm/firecracker`; заметка ~/projects/donor-audits/20261118-firecracker.md) — adversarial-ступень: микро-ВМ с отдельным ядром + jailer/seccomp, boot ~125 мс, snapshot прогретой базы → новый агент-VM за десятки мс. На qwen-MS-7E54 осуществимо (проверено: /dev/kvm rw, vhost-vsock, nested=Y); минус — 4 подвижные части (VMM, jailer, rootfs-образы, tap-сеть) и GPU внутрь без VFIO не идёт (ninfer остаётся на хосте).
Канон — и остаётся — в git. Дешёвые гейты: G0 LayerFS smoke (собрать, mount/commit/conflict/.lww); G0′ Firecracker smoke (бинарь из релиза + Alpine rootfs, чужой pip install внутри, snapshot/restore, замеры). Статус: вариант отложен, мандата нет.

## Архитектура hermes КРИВАЯ (Вл 29.10)

Мы в долгом пути исправления hermes, а не в попытке «из говна сделать конфетку». Единственный достоверный источник данных — анализ лога сообщений matrix. Все журналы и базы самого hermes — косвенные улики.

## Свидетельство исполнения (Вл 01.11)

Единственное достоверное свидетельство исполнения — история Matrix по client API, а в CLI — history сессии. Внешний разбор предлагал четыре разных доказательства (разрешение / принятое намерение / фактическое исполнение / увиденное человеком); по вердикту Вл все они доказываются одним и тем же источником — лентой (Matrix API / history сессии), а не внутренним состоянием: то, чего нет в ленте, не считается исполненным, что есть в ленте — не пересказывается базой. Внутренние durable-записи (write-ahead намерение, provenance join-фиксации) — обязательный механизм, но улики, не свидетельство: они обязаны сходиться с лентой, расхождение = инцидент.

## Свой Matrix-слой (Вл 30.10)

Архитектурные аксиомы намерения:
1. **Комната — телеграф.** У комнаты нет состояний (ни «родитель», ни «дочка»); что в неё напечатали, то она и рисует. Комната - это однин из видов консли. ак cli. механищмы потоков матрикс никаким образои не являются потоками автономного агента.

## Harness-адаптеры LoRA под Hermes (HF harness-hub) — прогон paired-замера (отложено Вл 18.11)

Донор линии: https://huggingface.co/collections/harness-hub (блог «Harness adapters: Safe RL for efficient agentic development»). Роллаут = живая сессия `hermes chat -q` в песочнице Harbor/OpenEnv (наш же харнес, Responses→chat через capture-прокси); reward = correctness верификатора + tool-efficiency bonus 0.1; AsyncGRPO (TRL), LoRA r=32. Бюджет прогона измерен ими: 400 шагов на 2×H200, ~474 задачи.

Замеры серии (все paired, held-out):
- LFM3-1.7B-hermes: **+16.0±7.0** pass@1 в домене data-analysis, **−38%** тул-коллов; на чужих repo-задачах (traj2envs) −7.5±10.8 (шум со знаком минус).
- LFM2.5-24B-A2B-hermes: точность ±0 (0.73→0.72), −20% тул-коллов; чекпоинт выбран на eval-задачах (сами оговорили оптимизм).
- TwIL-8B-hermes: held-out −2.0±6.9, чужой домен −12.9±17.6 — отрицательный перенос.
- Для контраста: Qwen3.8-27B-pi-adapted +25.5 п.п. — но на ином миксе задач (scale800+TermGrade, 1,919 групп) и против слабой под pi базы.

Вывод-гипотеза для нас: эффективность (меньше вызовов/ход) воспроизводится на нашем харнесе стабильно (−16…−38%), рост точности — только внутри обучающего домена и на малых моделях; в чужой домен перенос нулевой/отрицательный. Ожидаемый честный эффект для нашего контура — S-трек метрика «ходов до решения», не качество.

Возможный первый шаг (мандата нет): взять готовый LFM3-1.7B-hermes-GGUF как дешёвый worker на узкие роли (tick-обработка, классификация) и померить парой «адаптер vs база» на закреплённом наборе наших задач; полный цикл own-recipe — после публикации recipe в пятницу + наличие досуга на GRPO-прогон. Риск: адаптеры обучены под их промпт-форму Hermes (наш форк с авто-аттачем/гейтами — другой OOD-вход).

**СТАТУС 21.11 — парный замер сделан (Trek 0/1, без LFM3 — сразу 27B).** Recipe опубликован 20.11; harness-hermes-adapted (r=32) поднят третьим слотом на нашей же паре vLLM :8006 + gateway :8007; 6 фикстур × 3 руки (base / theirs / ours-V5), sandbox-чекеры. Итог: pass 6/6 · 5/6 · 4/6; tok_in base 384k → theirs 269k (−30%) → ours 236k (−39%). Гипотеза «эффективность воспроизводится, качество — нет» ПОДТВЕРЖДЕНА на нашем харнесе и нашем OOD-входе. Падения обоих адаптеров — один класс int-каста id (0772→772) + у ours индекс поля awk $9; replay-сет 18 записей: ~/projects/hermes-train/trek1/{error_catalog.md,replay_set.jsonl}. S-трек «ходов до решения» не нужен — чистые pass-провалы, не только неэффективность. Блокер «ждём recipe» снят.
- [ ] Guard: запрос на approve не должен приходить без содержимого. Внести изменения в код guard (fork-guardrails / approval-путь): карточка approve для write/patch защищённых файлов обязана нести фактический diff/текст вставляемых, заменяемых и удаляемых строк — не заглушку вида «write to AGENTS.md». Урок 21.11: за два часа агенты просили approve на AGENTS.md четырежды, ни разу не показав содержимое правки; владелец не может одобрять то, чего не видел.

## Проприоцепция одного Hermеса — один агент не знает, чем заняты его руки (СРОЧНО)
Один Hermес, много сессий; сессии не знают о действиях друг друга в текущем тике. Доказательная база 21.11: дубль строки в need2lora/AGENTS.md (два писателя за минуту), остановка serving :8006 веткой grpo-smoke без владельца-согласования (лежал 45 мин), двойной rg-шторм.

### 1. Арбитраж разделяемых ресурсов, а не расписание
Разделение по расписанию — тупейшее из возможных. В ОС и транспьютерах дефицит ресурсов решается очередями ожидания, приоритетами, вытеснением, бронированием — взять готовый класс механизмов и приложить: у ресурса (юнит, порт, GPU, файл-цель) есть владелец и очередь; stop/start чужого сервиса без выигранного арбитража запрещён в write-пути ядра, не в промпте.

### 2. Одновременная запись — решена в Unix 50 лет; тупость — одновременное редактирование
Никто не мешает двум писателям добавить запись. Мешает read-modify-write двух сессий в один файл. Следствие для руководящих документов: в AGENTS.md не ведётся индекс — руководящий документ определяет, куда и в каком порядке ДОБАВЛЯЮТСЯ записи (append-реестры, по одному на писателя или с атомарным append). Исправить need2lora/AGENTS.md: индекс вынесен (INDEX.md уже так), правило «документ = диспетчер мест добавления, не хранилище мутируемого состояния».

### 3. Имя сессии — имя, а не хэш
Каждая сессия/ветка получает человекочитаемое имя (не plan-114130 и не hex), оно носит каждый след: work-note, запись журнала, commit, владение ресурсом. Без именованного агента аудит «кто это сделал» остаётся археологией.

### 4. Семафор на проекты и данные (Вл 21.11)
«мало семафорам на ресурс, тут еще проекты и данные надо семафорить». Кроме арбитража ресурса (1) и формы записи (2) нужна привязка намерения к проекту: один проект (заказ) ведёт одна сессия; чужая сессия о чужом проекте даёт только findings, не правки. Проверимо в write-пути, не в промпте.


## Починка описаний каталога умений (Вл 26.11)

Постановка Вл: «внеси в ToDo починку, потом проверим надстройку».

Слабые однострочные descriptions — подтверждённый измерением главный рычаг качества
роутинга: обогащение представления умения (name+use-when+заголовки) подняло recall@5
с 0.202 до 0.298 без всякой интеграции (замеры 26.11, /home/qwen/projects/gepa/router/README.md).
Те же кривые строки читает и сама модель, выбирая skill из индекса вручную — починка
описаний поднимает оба маршрута одновременно и не требует рестартов.

Работа: корпусная правка SKILL.md-описаний (критерий строки уже в правиле авторинга —
«Use when <trigger>. <behavior>.», ≤60 символов; дополнять use_when:/«Use when»-строками
и содержательными заголовками там, где их нет), приёмка каждой партии — рост
recall@5 в selftest роутера (/home/qwen/projects/gepa/router/skill_router.py selftest,
коридор n=84 @5; рост над 0.40 — новый коридор selftest переназначить).

После починки — вторая половина: проверка надстройки (вариант 3: роутер раскрывает
тела 2-3 вероятных умений поверх целого индекса), решение по ней от Вла отдельно.


## Активация фич Tier M/L — продолжение позже (Вл 27.11)

Постановка Вл: «тогда отметь в ToDo, что продолжим позже».

Состояние на 27.11 22:50: активированы L1 (decision-server :8002, enabled),
M4 (message_shadow + judge_model=glm-5.3-flash-libert), M3ф1 (turn_shadow.enabled),
M1 (target_accept), M2 (result_acceptance). Прод fed22d33fe7, все гейты зелёные
(L1 9/9, M-тесты 58, M2 8/8, M1 10/10, shadow 4/4).

Осталось в очереди (каждый пункт — отдельная команда Вл):
1. M3ф2 (turn_shadow.wrap_up_enabled): включить после ~недели естественного
   накопления ~/.hermes/logs/turn_shadow.jsonl; сведение A1: распределение скоров,
   доля would_wrap_up, совпадение с реальными зависаниями; синтетику
   session=m3f1-pipeline-probe-2711 отсечь. Пороги при ложных — править до активации.
2. L2 (agent_periodic_bounds.enabled).
3. L3 (bot_relay_mentions.enabled; проверить roster перед включением).

Вне очереди: Tier A (elk/openhuman-autonomy-tire-a @00eef5f2dee на GH) — не мержен;
p.1 residual на ноде rtx5000 (terminal-default печатает process-cwd вместо workdir) —
копает plan-200255; TZ-openhuman-autonomy-v2.md не прочитан.

## Доноры тренировочной/RL-линии: Automodel + Motif (сгруппировано по слову Вл 28.11)

Общая линия: тренировочный харнес и данные для НАШИХ моделей. Оба донора размечены;
тиры заимствования ждут «иди» Вл. Рядом стоит секция «Harness-адаптеры LoRA под Hermes»
(paired-замер 21.11 сделан) — та же линия, третья нога.

- **Automodel** (NousResearch/Automodel = зеркало NVIDIA-NeMo, Apache-2.0; клон ~/zoo/automodel
  @922e6b0; разбор automodel-analysis-v1.md + cluster-A1..A4-v1.md там же). Главное: официальный
  тренировочный путь Qwen3.8-27B/Flash-Next/GLM-5.3; JSONL agent-траектории как вход (agent_chat.py) —
  кандидат-движок harness-rl-data; EAGLE-3 speculative drafter — единственная полностью отсутствующая
  у нас фича. На .100: LoRA/Full-SFT 27B идут (gbs 8→6), блокер диск (195G free против DCP-чекпойнта
  27B ≈162ГБ); Flash-Next 180B мёртв (нужно 64×H100). NVFP4-тренировок у донора нет. Supply chain:
  пин transformers==5.12.1, megatron-fsdp из личного форка, wandb default-on. Тиры S1/S2/S3 не объявлены.
- **Motif stack** (Motif-Technologies; разбор ~/projects/donor-audits/motif-analysis-v1.md, 28.11):
  - HF-kernels `optimizer` — Parallel Muon с произвольным N-D шартингом (проверен до 2TP×2DP-R×2DP-S),
    Polar Express NS, MoE-расщепление 3D, Per-Head Muon, QK clipping; 19 бинарей torch 2.7–2.10 ×
    CUDA/ROCm, x86_64-linux. Ценен для много-GPU full FT/pretrain; на одном GPU избыточен
    (torch.optim.Muon с 2.9 хватает).
  - Харнес **AgentTracer** (github MotifTechnologies/AgentTracer, Apache-2.0, pip motif-agent-tracer) —
    прямой аналог нашего harness-rl-data: vLLM-first эндпоинт, docker-песочница на задачу,
    MCP attach-серверы, схема на 12 типов событий, экспортёры ShareGPT/HF/**GRPO**, resume прогонов.
  - Датасеты: `agenttracer` (1 023 траектории Kimi-K3, 999 отфильтрованы correct=True) +
    `agenttracer-tooluse` (5 756, из них 72% — не траектории, а чтение; у web-search все tool-результаты
    вырезаны в плейсхолдер — дефектный учебный материал). Точечно брать: ~1 300 честных траекторий +
    категории abstention (89) / memory (180) / multi-turn-tool-use (101) как шаблоны классов задач.
    Лицензии mixed (HotpotQA CC BY-SA, Kimi K3 §2/§3) — при редистрибуции резать subset'ы.
- Точка стыка двух доноров: вход Automodel-RL = JSONL траекторий; AgentTracer — референсная форма
  контракта траектории/GRPO-экспортёра для сверки нашей схемы harness-rl-data.
- Статус: «иди» не дано; решение о заимствованиях за Вл.
- Fable-датасет (MoreThought/Fable-5.1-Max-Reasoning-Filtered-10000x, разбор 28.11 в чате: 5k —
  100% плейсхолдер-аргументы, 10k — 53% пустых, 0% видимого thinking, 82% обрывов трасс):
  **вердикт Вл 28.11 — не берём.**

## Разобрать датасет google/multimodal-knowledge-agent (отложено Вл 28.11)

https://huggingface.co/datasets/google/multimodal-knowledge-agent — «Multimodal Knowledge
Agent (MKA) Showcase Examples», Google, Apache-2.0, <1K строк (imagefolder: image/document/
geospatial), обновлён 10.11.2026, 10.5k скачиваний.
Что разобрать: формат агентных мультимодальных трасс (документы/карты/VQA), схема
шагов и тулзов; пригодность как (а) референс формы наших intake-трасс pass2/pass3
(Qwen3.8-27B/Flash-Next :8001/:8002 читают растры PDF — прямой стык), (б) eval-набор
длинных мультимодальных эпизодов, (в) кандидаты в dataloader Automodel.
Ссылка рядом: секция «Доноры тренировочной/RL-линии» (Automodel+Motif) — тот же контур.

## Пилот Heretic: training-free правка наших моделей (отложено Вл 28.11)

https://github.com/p-e-w/heretic (★43.5k, AGPL-3.0, автор p-e-w, живой — коммит 27.11, v2.2.dev;
разбор 28.11 в чате). Правит веса БЕЗ обучения: nosyco/noslop/terse/refusal- removal на
контрастных промптах; модификатор ARA (его методика, PR #211) или классический abliteration
(проектный, norm-preserving); скореры-плагины (keyword_rate, KL-страховка к оригиналу,
длина, BERT-классификатор) + Optuna TPE. model.py знает GatedDeltaNet/linear_attn и MoE —
наша линейка Qwen3.8/Flash-Next в фокусе поддержки.

Что сделать (по мандату): пилот nosyco или noslop на bf16-HF базе одной рабочей модели на .100
(27B bf16 в одну карту; правка только по bf16-базе, NVFP4/GGUF переквантовывать после),
A/B по нашей агентской фикстуре (та же схема, что Trek 0/1: pass/tok_in до-vs-после).
AGPL — только внешний CLI (pip heretic-llm), в форк не вливать.
Заимствуемые паттерны без инструмента: «изменение поведения = контракт скореров + KL-гейт»;
Grimoire-манифест reproduce.json (9КБ, побайтовое воспроизведение + append-only коллекция).
Соседи автора: lophius (LM-workbench, AGPL), llm-beliefs (визуализация убеждений моделей) —
не разобраны. Статус: мандата нет.

## Router-signal suite: фикстуры для линии роутеров/GEPA (донор оценки, отложено Вл 28.11)

https://huggingface.co/datasets/vllm-sr/router-signal-suite (vLLM Semantic Router, IBM;
600 959 строк, текстов 86%, обновлён 28.11; разбор 28.11 в чате). Бенчмарк 10
сигналов-классификаторов: domain, tool_need, fact_check, hallucination, safety, hazard,
jailbreak, pii, feedback, modality. НЕ учебные данные для LoRA — донор ОЦЕНКИ и фикстур.

Три применения:
1. tool_need (26 654 стр., when2call/BFCL, поле tools с описаниями функций) — фикстур и
   донор для гейта «звать ли tool_search/skill или ответить прямо» (L1 decision-server
   :8002, wake-seam ≤15 с). Их latency-замеры 0.6B на CPU: ms-бюджет у такого гейта реален.
2. fresh-ru-pii (1 842 стр. русских реквизитов: ООО/адреса/телефоны — проверено, наше
   intake-сырьё один в один) — фикстур PII-маскирования перед облачным фолбэком wake-proxy.
3. Методология для GEPA-week (каркас остановлен): frozen fresh set до тренировок, сплиты по
   group_sha256 (целые треды), paired bootstrap + BH-коррекция, построчные предсказания всех
   сравниваемых моделей в комплекте, docker-билдер с sha256-манифестами (36 ГБ rebuild).

Их честные цифры (калибровка ожиданий): tool_need AUC 0.91 при recall@5%FPR 0.26-0.31;
hallucination AUC 0.70-0.73; domain acc 0.64; два fact_check-holdout выдают метку длиной
текста. Вывод совпадает с нашим: дешёвый шлюзовый сигнал не пускают напрямую в вердикт.
Риски: лицензия per-source (14 non-commercial, Stack Exchange CC BY-SA), 4 гейтнутых корпуса
(WildGuard/WildJailbreak/HackAPrompt/AggreFact — токен), часть меток модельная.
Первый шаг по мандату: прогнать наш skill-router на их tool_need-корпусе. Статус: мандата нет.

## Форк Hermes-3-Dataset как общий инсттакт-фундамент LoRA (отложено Вл 28.11)

https://huggingface.co/datasets/NousResearch/Hermes-3-Dataset (apache-2.0 — единственный
чистый лицензионно датасет серии; 1.695 ГБ, ~762 тыс. строк, срез 11.07.2025, не живой;
разбор 28.11 в чате — 3 среза, 46 717 строк).

Роль в нашем форке: СЛОЙ 0 = общий инсттакт-фон (regularizer к mail-gate траекториям,
иначе LoRA на 1 098 трассах перекашивается). НЕ агентский корпус: медиана диалога — 3
сообщения (system+1 пара), tool_calls/```tool_code/[API calls] = 0 на 104 МБ, tool-ролей
нет; файл НЕ перемешан (head=персон-ролеплей, mid=Q&A без system, tail=креатив).

Обязательные наши изменения перед использованием:
1. provenance-поля (source/subset/sha/lang) — в исходнике их нет вовсе + перемешать;
2. схема: слой 1 = наши агентские траектории (mail-gate LoRA-корпус + trace-export) в
   расширенной схеме с tool-ролями; не валить в одну схему со слоем 0;
3. system-промпты персон перегенерировать под наш chat template (срез эпохи Hermes-3/Qwen2);
4. RU-подмешивание (кириллицы в корпусе 1%) из наших intake/mail-корпусов.
Формат from/value ShareGPT совпадает с нашим export_to_sharegpt.py — конвертер не нужен.

Первый шаг по мандату: скрипт fork_hermes3_base.py (схема+provenance+shuffle+языковые
метки) + mixing-ratio слой0:слой1 для первого LoRA-прогона. Статус: мандата нет.

## Donors «agents»: эвалы и self-judgement-слой (отложено Вл 28.11)

Проход https://huggingface.co/datasets?search=agents (664 репо, разбор 28.11 в чате;
контент-проверка 12 верхних кандидатов). Полезное — в основном ЭВАЛЫ, не тренировка.

Eval-линия (приоритет):
- meta-agents-research-environments/gaia2 (CC-BY) — 6 осей ×160 задач
  (execution/search/ambiguity/adaptability/time) — готовые оси для eval-матрицы
  hermes-train;
- agents-last-exam/agents-last-exam (★238, CC-BY) — v1.1=122 таска + qcow2-VM,
  потолок-бенч 2026, тяжёлый обвес;
- AgentSuite/tau-bench-trajectories — 30 файлов сравнительных траекторий по моделям
  (DeepSeek-R1/Kimi-K2/thinking on-off) — реплеи и negative-корпус; ЛИЦЕНЗИИ НЕТ — риск;
- oro-agents/smb-bench (CC-BY, живой) — 192 таска ×16 моделей + prices/controls
  (экономика прогонов) — ближайший к elk/закупочному домену;
- mercor/apex-agents (★227, CC-BY) — бизнес-таски с gold-файлами (docx/xlsx/pptx-эталоны)
  — формат «сделай документ как эталон»; репаки-паразиты того же — не брать;
- crawlcheck/agent-safety-benchmark (CC-BY) + sixujinzhu/agent-security-exam-bank —
  кейсы для верификации fork-guardrails.

LoRA-линия (в candidates/ staged-пака ~/need2lora/hermes-staged-sft-v1 поACCEPT):
- Self-Improving-Coding-Agents/SI2CA-Training-Trajectories (CC-BY, 10 780 ×4 среза:
  standard/discovered_strategy/full_self_judgement/tasks) — агент судит себя, прямо в
  supervisor/critic-линию и hermes-agent-self-evolution; кандидат в этап 2.5;
- nex-agi/agent-sft (ODC-BY, ★129, 5 271 строк: agent/coding/research/chat,
  messages+tools) — компактный чистый SFT-донор (проверено);
- Autonomous-Scientific-Agents/{requests,results,groundtruth} (Apache) — request-level
  дампы + groundtruth — образец артефактной культуры для trace-export.

Шум: agents-course/* (сертификаты курса, топ dl — обманка), agentshot (99 998 файлов с
.venv), agent-swarm-traces (62 478 логов, лицензий нет), apex-репаки, emgena-teasers,
D2E/Minecraft-видео, SciPhi/AgentSearch-V1 (20.9M, 2024, тяжёлый/древний).
Статус: мандата нет. Обучение hermes-staged-sft-v1 — «пока не проводить» (Вл 28.11).

## L8-RU: вердикт-парсеры и русские диалоги (отложено Вл 28.11: «отложи в todo, вернемся еще к теме»)

L8 (vote+quorum) не выкидывать. Гипотеза Вл: диалоги и промпты на русском — вердикты
могут проходить мимо EN-парсеров; либо наш форк мимо стандартных механизмов hermes.
Измерено 28.11 (пилот `~/zoo/tinyhumansai/thl8/ru/`, реальные русские сообщения из state.db):
— harness-hermes на EN-промпте ask_judge: 0/24 parser-miss (отвечает по-английски, мимо не проходит);
— production-судья message_shadow: модель `critic` на :8012 НЕ обслуживается (там glm53-алиасы)
  → каждый вызов ask_judge = HTTP 400 → статус `unavailable`; message_shadow.jsonl пуст.
Что вернуться просит: решение по живому судье (алиас/эндпоинт), промер false-rate на
mention-gated комнатах, затем quorum-надстройка поверх ask_judge.

## stage001 — свой Matrix-слой (одобрено Вл 30.10)

Архитектурные аксиомы намерения:
1. **Комната — телеграф.** У комнаты нет состояний (ни «родитель», ни «дочка»); что в неё напечатали, то она и рисует. Комната - это однин из видов консли. ак cli. механищмы потоков матрикс никаким образои не являются потоками автономного агента.

## stage003 — тематическая память ядра (донор dsh-superagent @6337fc8; намерение Вл 01.11)

Намерения (дословно):
1. «этотдля нашего проекиамавтономного агента https://github.com/deepseek-ai/dsh-superagent-prototype» +
   «сейчас дам ссылку. надо тщательнотпроанализировать проект по ссылке. вплоть до анализа кода реализации.
   на возможность заимсвования компонентов и идей для ядра.» — исполнено: донор-аудит (клон
   /data/watch-repos/deepseek-ai--dsh-superagent-prototype @6337fc8, прогон тестов 1196/1197, отчёт
   ~/projects/hermes-autonomous/reports/deepseek-ai--dsh-superagent-prototype.md); порты механизмов
   (ctl_effect/outbox/mandate/event_disposition/input_receipt) в ядре коммитом `31955a1626`.
2. Два текста-предложения 01.11 (полные тексты — в ленте этого DM, аксиома свидетельства = лента;
   сжатие и сверка с кодом — в отчёте ниже): (а) маршрутизатор контекста — детекция неявных рабочих тем,
   scope-resolver корректировок, консервативная запись («отсутствие указанного scope не является
   разрешением на глобализацию»), read-gate на выдаче; (б) изоляция памяти по namespace темы —
   неизменяемый ExecutionContext (user/topic/branch/run/memory_revision), ScopedMemory с подстановкой
   scope сервером, тема берётся из задания а не из текущего чата, история/summary по темам отдельно,
   fork = snapshot+overlay, join = структурированный merge, MEMORY.md остаётся только политикой.
3. «проанализировать и дать заключение, как вставить в ядро нашего автономного агента такой слой памяти» —
   исполнено: ~/projects/hermes-autonomous/reports/topic-memory-integration.md.
4. «можно это намерение и метод реализации зафиксировать в ToDo.md как stage003?» — эта запись.

Заключение (изложение агента, обоснование — path:line в отчёте):
- Единица темы в ядре УЖЕ существует: topic_id = `ctl_task.task_id` (+`project_id` надтема),
  branch_id = `session_branches`, user_id = Hermes-профиль, run_id = `ctl_run.run_id`.
  Автодетектор неявных тем для v1 НЕ нужен — он последний шаг плана.
- Точки перехвата (проверены по коду): worker контроллера = one-shot `hermes chat --oneshot`
  (workers.py:162–180, env HERMES_CONTROLLER_TASK_ID/RUN_ID уже передан) — снапшот памяти собирается
  заново на каждый шаг, prompt-cache не задевается; memory tool получает третий target `task`
  (targets {memory,user} сейчас) с записью в SQL и provenance, глобальные targets для scoped-воркера
  read-only (механика target_enabled есть); инжекция — volatile-сегмент после static prefix
  (снапшот грузится agent_init.py:1252 / system_prompt.py:834); компрессия — существующий
  focus_topic ← title/goal задачи; межтемный доступ — контролируемый импорт reference/copy с provenance,
  семантическая похожесть тем права чтения не даёт.
- Хранение: mem_scope / mem_item / mem_binding / mem_import в каноничном SCHEMA_SQL по ctl_-конвенции
  (writers = controller package); kind ∈ fact|constraint|decision|preference; источник ∈
  user-directive|tool-result|agent-proposal|hypothesis — последние два активным правилом становятся
  только после подтверждения.
- Правило записи Вл: отсутствие указанного scope = сохранить локально (scope текущей задачи), never
  global; повторяемость — основание предложить обобщение, не доказательство универсальности.
  Иерархия применимости task → project → global(readonly); «новее затирает старое» только при
  совпадении предмета И области действия.
- Соответствие вердикту 01.11 (механизм/мысль): изоляция техническая — различает источники и области
  действия записей, reasoning не ограничивает; агент вправе искать и предлагать обобщения между темами,
  повышать scope молча не вправе; к человеку — только человеческое намерение/полномочия.

Хвост донора (исполнено 02.11, `controller/live_ports.py` + тикер): durable effects/outbox подключены
к живому циклу — pump-тик дренаит очередь ПОД своим lock'ом (single-flight, bounded passes 32);
producer «задача провалилась» = idempotent-уведомление на failed-RUN через ctl_outbox → Home-канал
(deliver_fn или alerts.deliver; отказ доставки растит attempts, барьер порядка снимается после
FAILURES_BEFORE_ERROR, по донору). Отправка в чат из тестов не стреляет (guard-фикстура).

Порядок работ (каждый шаг обратим):
1. ✅ Схема mem_* в SCHEMA_SQL + writer controller/topic_memory.py (CRUD, provenance, supersede).
   Исполнено: коммит `5fb31072af` (ctl_mem_scope/item/binding/import; чтение = SQL-фильтр scope ДО поиска).
2. Env → scope в worker_fn (HERMES_SCOPE_KIND/ID); центральной сессии pump — scope='global'.
   — исполнено тем же коммитом (`workers.scope_env`; отсутствие переменных = global на стороне чтения).
3. Memory tool: третий target `task`; ограничение глобальных writes у scoped-воркеров.
   — исполнено: `tools/topic_memory_bridge.py` + routing в `tools/memory_tool.py`
   (target 'task' появляется в схеме ТОЛЬКО при HERMES_SCOPE_*; запись через controller.topic_memory;
   источник по умолчанию agent-proposal, правила — после confirm; гвард глобальных writes
   memory.task_restrict_globals, по умолчанию OFF до shadow-приёмки).
4. Инжекция тематического блока при сборке промпта (global-политика + project/task блоки из SQL,
   фильтр scope ДО поиска) — исполнено: volatile-сегмент после memory-блоков
   (`agent/system_prompt._topic_memory_part` → bridge.render_prompt_block; без env — ноль байт).
5. focus_topic компрессии ← title/goal задачи; summary шагов линковать через ctl_artifact_revision,
   не дублировать в память.
   — исполнено (первая половина): `tools.topic_memory_bridge.task_goal_topic` приоритетнее
   эвристики `_derive_auto_focus_topic` в `agent/context_compressor_summary.py:89` только у
   scoped-процессов; prepared_compaction (интерактивные fork'и) не тронут — там нет scope-env.
6. Shadow-режим: тематические записи пишутся параллельно со старой логикой MEMORY.md; счёт трёх ошибок
   Вл — (i) локальная корректировка стала глобальной, (ii) чужое правило попало в контекст, (iii) при
   возврате к теме не найдено ранее принятое решение. Первые две дороже третьей; политика консервативна.
   — каркас исполнен: таблица ctl_mem_shadow + writer `controller/topic_memory_shadow.py`;
   авто-детекторы: (i) scoped-запись через builtin store при OFF-гварде, (iii) подтверждённые правила
   за шапкой рендера (_MAX_PROMPT_ITEMS=40); (ii) в v1 — ручная разметка (нужна семантика);
   поведение shadow НЕ меняет; интерпретация и приёмка — Вл.
7.  детектор неявных тем для интерактивного агента (наблюдателем, по образцу
   embedding-классификатора из разбора 01.11).
   — каркас исполнен (`bebbe94f5b`): controller/topic_detector.py — на каждый
   интерактивный user-оборот одна строка в ctl_topic_detect (corpus = активные
   ctl_task goals + прошлые предложения; embed через эндпоинт плагина
   sqlite-memory, lexical-фолбэк; fail-open). Хук в gateway _run_agent_inner,
   отдельный поток; heartbeat'ы и HERMES_SCOPE-процессы пропускаются.
   Гейт memory.topic_detect_enabled по умолчанию OFF — ноль строк, ноль HTTP.
   Классификация НЕ читается в поведение до приёмки Вл (шаг 6 тени).

Урок item1 (инцидент 02.11, PATH-замечание Вл): `hermes chat --oneshot -Q` воркера
исполняет ход В ПРОЦЕССЕ ШЛЮЗА (gateway-hosted turn) — env-зависимости ядра возить
только именованными переменными через client_env/BEHAVIOUR_ENV, не через окружение
процесса. PATH-канал тулчейна (TOOL_ENV, client_env_passthrough) — best-effort для
интерактивных тулов: conda/uv/venv'ы тусуют PATH как хотят, на нём ничего не строить;
тесты не должны зависеть от окружения запуска (герметизировать monkeypatch'ем
`_HERMES_BIN_DIR`, см. tests/gateway/test_session_client_env.py).

Урок инцидента 03.11 (разбор по ленте 00:07–00:11; «гробил проект выводами»): все четыре
ошибки — вывод раньше опоры на источник. Код-механизмы (правила в промпте недостаточны,
прецедент session.py boot-identity):
1. **Медиа-происхождение кодом** (`1fcf646d12`): каждый media-оборот открывается строкой
   `[Media origin (resolved by gateway code...): platform=…, chat=…]` — платформа приходит
   фактом, vision отвечает за контент (лечит угадывание «Telegram vs Matrix» по пикселям).
2. **Пины свидетельств** (`e6e4a62223`): gateway/platforms/media_pin.py — цитированные медиа
   переживают age-sweep (rolling 24ч, кэп 7д), housekeeping чистит реестр.
3. **Рождение сессии O(1)** (`7191bbe8ec`): `**Session started:** …` в Current Session Context
   из created_at (cache-stable) — «уже был /new?» не реконструируется из нарратива.
4. **Гейт деклараций** (`933f251aa0`): финальный текст с success-словами без артефакта проверки
   (тесты N/N, хеш, путь, tool run) получает маркер `◌ Декларация без артефакта проверки`.
   Off-switch: HERMES_DECLARATION_GATE=0.
