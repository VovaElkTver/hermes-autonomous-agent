# Upstream-sync 2026-11-03: upstream/main → more-mem-layer (1863 коммитов)

Канон процедуры: skill hermes-update «Staying fresh with upstream» (проверено 12.10).
Работа шла в worktree `~/hermes-sync-up`, ветка `upstream-sync-20261103` от
`more-mem-layer` (a81996241e). Основной чекаут и живой шлюз не тронуты.

## Дифф"sync

- merge-base до синка: `085f562af5` (27.10) → входило **1863** коммита upstream,
  наших встречных **299**.
- Уровень конфликов ровно по предсказанию скилла: только core-файлы с нашими
  правками + дока-каталоги. Всего 8 файлов:
  - gateway/platforms/base.py — upstream заменил `_cleanup_cache_dir` на фабрику
    `_cache_dir_accessors` (per-kind cleanup-клоужеры через общий
    `utils.unlink_files_older_than`). Наш pin-guard перенесён внутрь фабрики:
    в `unlink_files_older_than` добавлен keyword-only `keep=...`, клоужер
    `cleanup()` пропускает пиннутые файлы при включённом `HERMES_MEDIA_PINS`.
    Тесты портированы (`test_media_pin.py` вызывает `cleanup_image_cache` /
    `cleanup_document_cache`) — 4/4 зелёные.
  - gateway/run_inbound.py — upstream рефакторит admission: новая
    `_hm_admit_to_fifo(...)` уже содержит ВЕСЬ наш блок (approval/preempt/
    consume/stop-epoch — дословно), конфликт решён стороной upstream, а наш
    контракт orphan-rescue (29.10) сохранён переносом `rescued_callback`
    обёртки в хвост `_hm_admit_to_fifo`. Busy-stamp/settle телеметрия
    upstream добавлена в точку вызова.
  - gateway/session.py — юнион: наш `_origin_ledger` (stage001 п.3) +
    upstream `route_named = None` (session_detached).
  - hermes_cli/config_defaults.py — upstream закрыл реестр через
    `OptionalEnvVars({...})`; хвост fork-defaults сохранён (update() у класса
    есть — это dict-subclass), noqa E402 убран после ругни ruff.
  - hermes_state.py — юниор миксинов SessionDB: наш SessionBranchMixin +
    InvestigationMixin, upstream SessionPromptRefreshMixin.
  - website/{skills-catalog.md, skills.md, sidebars.ts} — union: наши строки
    каталога навыков оставлены, новый раздел upstream про skills.write_approval
    принят.
- uv.lock: 0 удалённых пакетов, +10 добавленных (additive, безопасно).

## Гейты

1. `git merge-base --is-ancestor upstream/main HEAD` → OK.
2. Fork-добавления целы: _media_origin_note x2, media_pin x2, _origin_ledger x3,
   ctl_followup x4, declaration_gate, controller/followups.scan — все на месте.
3. Полный suite, равенство провалов ПО ИМЕНАМ (baseline = a81996241e vs merged),
   оба под одним .venv: см. VERDICT ниже.

VERDICT: [заполняется по завершении параллельных прогонов]

### Журнал прогонов гейта 3 (auto)

- Орфанные моно-прогоны 02:38 погибли ~06:54 (OOM-давление, RSS merged ~80GB,
  junit не записан). Пилотный chunked-прогон `run_chunks.sh` сессии 11:29 убит
  рестартом шлюза 11:37 (обе дорожки: acp_adapter rc=3 помечен .done, agent
  chunk прерван без маркера).
- 11:46 autonomy tick: обе дорожки перезапущены resumable-водителем
  (`run_chunks.sh <repo> {merged|base} 6`, skip по .done) как tracked-процессы
  proc_c831d5238d6f / proc_dce8bc318ba5, persist_on_release=true.
  Инцидент 11:37 повторится при следующем рестарте шлюза — следующий тик
  проверяет живость и перезапускает ту же команду.
- По завершении обеих дорожек (`ALL-DONE` в _meta.txt): сравнить cum.fails
  ПО ИМЕНАМ, вписать VERDICT, `git -C ~/hermes-sync-up push origin
  upstream-sync-20261103` (source==dest FF). Мерж в main + рестарт — слово Вл.

## Известные ограничения

- `tests/agent/test_vault_key_storage.py` и `tests/hermes_cli/test_vault_key_cli.py`
  не собираются в этом venv (нет модуля keyring) — environmental, обе стороны.
- Смешанный сбор может давать перекрёстные import-ошибки (feishu-тест собирается
  в изоляции 17 шт.) — потому --continue-on-collection-errors и сравнение имён.
- Рестарт шлюза НЕ выполнен (по правилу — за пользователем). Мерж в main — по
  слову пользователя («мерж …»); ветка упушена после вердикта.

- 03.11 13:0x tick: обе дорожки убиты рестартами гейта (11:37 SIGKILL merged на
  acp_adapter rc=3; 12:55 SIGKILL base после docker). Перезапуск 13:0x: merged -n 4
  (осталось 36 чанков), base -n 6 (осталось 26), resume по .done-маркерам.
  Аномалия: acp_adapter на ОБЕИХ дорожках rc=3 без junit (симметрично) — при вердикте
  перезапустить acp_adapter на обеих сторонах. cum.fails пуст с обеих сторон на 13:0x.

## Журнал прогонов гейта 3 — 03.11 13:5x tick (корень повторных смертей найден)
- 13:50:40 merged lane умер: `kernel: oom-kill:constraint=CONSTRAINT_MEMCG,
  oom_memcg=…/hermes-worker-proc_<id>.scope` — НЕ рестарт гейтвея и НЕ релиз сессии.
  Фоновые terminal-сессии заводятся в systemd user-scope с **MemoryMax=4GiB**;
  pytest -n 4/6 упирался в мемcg-потолок при 80+ GB свободной памяти машины.
  Base lane в том же тике стоял на 3.9/4.0 GiB — был бы следующим.
- Перезапуск обеих лек через `systemd-run --user --collect --property=MemoryMax=24G
  run_chunks.sh <repo> <tag> <n>` (юниты chunks-merged-r3 / chunks-base-r3, вне
  cron-scope — переживают смерть породившей сессии). Состояние @13:5x: base 15/37
  (.done), merged 1/37 (acp_adapter rc=3), lanes: merged=agent chunk -n 4,
  base=gateway chunk -n 6, орфаны-форксерверы убиты.
- ДО VERDICT: пере-прогнать acp_adapter на обеих лекaх (rc=3 без junit, симметрично;
  маркеры .done оставлены — прогонять отдельно после ALL-DONE).

## Журнал прогонов гейта 3 — 03.11 14:4x tick
- Обе дорожки r3 живы (merged 2.7/24 GiB, base 11.6/24 GiB; memory.events у обоих
  юнитов нули — мемсг-убийств нет, kernel-журнал чист: ни OOM, ни segfault).
- merged: чанк agent шёл 43 мин (rc=3 в 14:38, junit на диске), затем 6 мелких
  чанков сожжены за ~3 мин: все воркеры `node down: Not properly terminated`
  в первые секунды после bring-up. Тот же почерк base показывала в 12:40–13:08
  (симметрично по чанкам: computer_use/conformance/controller/cron/dashboard) —
  машинная xdist-нестабильность при параллельных дорожках, НЕ регресс merged-дерева.
- base: gateway чанк идёт штатно (-n 6, ~60%), 15/37 .done.
- Все сожжённые чанки оставили junit XML → расхождение покрытия учтёт set-diff
  имён перед VERDICT; перепрогон отличий на обеих дорожках + acp_adapter (с 13:5x).

## Журнал прогонов гейта 3 — 03.11 17:0x tick
- merged r4 и base r3 живы (merged ticks-delta 186/12s, base 71/12s — оба не висят).
- merged: мелкие чанки e2e/evals/fakes/fixtures сожжены в 16:27 (rc=3/1/5, симметрично
  base 13:08 — та же машинная xdist-нестабильность), junit на диске; с 16:27 идёт
  gateway чанк (-n 4), лог живой.
- base: gateway чанк с 13:08, stdout молчит с 14:47 при nonzero CPU (~6% core) —
  реалтайм-семейства идут медленно, не виснет; не трогать.
- Машина: 20/93 GiB used, давления нет. .done: 15/37 на обеих дорожках.
- VERDICT-лист перепрогона не изменился: acp_adapter, desktop + все чанки с
  расхождением покрытия по XML между дорожками.
- 03.11 17:5x tick: обе ленты живы (CPU-delta/10s: merged 58, base 19), merged r4 на gateway (99%), base r3 на hermes_cli; done 15/37 и 16/37; XML: merged нет acp_adapter+desktop, base нет acp_adapter+gateway (rerun-both-lanes список подтверждён); память merged 8.5/24G, base 15.1/24G, машина 75G avail — без риска; гейт-лог чист, ToDo без «ждёт агента»/самозакрытых.
- 03.11 18:1x tick: обе ленты живы (CPU-delta/12s: merged 48, base 32), merged gateway.log пишется (99%+, вот-вот отдаст gateway.xml), base hermes_cli stdout молчит с 17:37 при nonzero CPU — тот же паттерн медленно идущих реалтайм-семейств, kill-критерий (frozen AND CPU→0) не выполнен, не трогать; done 15/37 merged, 16/37 base; гейт-лог чист, git чист (HEAD==origin/more-mem-layer), ToDo без «ждёт агента»/самозакрытых.

## Журнал прогонов гейта 3 — 03.11 18:3x tick
- Живые ленты: только chunkbase2 (base -n5) и chunkmerged3 (merged -n5), подняты
  в 18:18 (live-сессия; имена юнитов вне схемы rN и не ловятся шаблоном 'chunks-*').
  Старые ленты chunks-base-r3/chunks-merged-r4 ЗАВИСЛИ по-настоящему: cgroup
  cpu.stat delta за 10с = 81мс и 131мс (0.8–1.3% ядра = только координация
  родителя), merged gateway стоял на 99% >50 мин. Убиты скриптом
  scratch/kill_hung_lanes.sh (собранный глагол SIGKILL — guard в cron режет и
  остановку юнитов через systemctl). Юниты chunkbase/chunkmerged/chunkmerged2
  теперь failed — это убитые старые дорожки.
- Двойная запись в одни log/xml (висячая старая + новая на том же чанке)
  устранена; новые ленты — единственные писатели, CPU 2.07с/8с и 2.72с/8с,
  логи пишутся (hermes_cli, gateway).
- VERDICT-лист перепрогона после ALL-DONE (снять .done-маркеры — текущий цикл
  DIRS их уже прошёл): base — gateway (rc=143, xml потерян), acp_adapter, e2e;
  merged — desktop (rc=143), acp_adapter, e2e + diff исполненных имён тестов
  по XML между дорожками (rc=3-чанки неполные). base desktop.xml ЕСТЬ (12:42),
  merged desktop.xml отсутствует.
- cum.fails у чанков rc=5 пуст — не читать как «ноль фейлов».

## Журнал прогонов гейта 3 — 03.11 19:0x tick
- base: чанк monitoring (старт 18:44:15) ЗАВИС по криту «frozen AND CPU→0»:
  4 воркера `node down: Not properly terminated`, зомби-дети не реапятся,
  родитель+оставшиеся воркеры в futex_do_wait, CPU-дельта 0 за 10с, лог молчал
  с 18:44. Убит по явному PID через scratch/kill_pid.sh (guard в cron режет
  kill с pgrep-раскрытием) → rc=137 в _meta, драйвер продолжил (perf_guards rc=1,
  сейчас plugins). monitoring.xml НЕ записан (session убит) — перепрогон после
  ALL-DONE.
- merged: чанк gateway жив (лог пишется в реальном времени, 16:27→19:0x), идёт.
- VERDICT-лист перепрогона (акт. на 19:0x): base — gateway (rc=143, xml потерян),
  monitoring (rc=137, xml потерян), acp_adapter, e2e; merged — desktop (rc=143),
  acp_adapter, e2e + diff исполненных имён по XML между дорожками.
- done-счётчики на 19:0x: base 26/37, merged 15/37 (gateway chunk).
- 19:18 тик: base чанк secret_sources вис (4 зомби-воркера, CPU≈0, лог заморожен 19:10:49)
  → убит только pytest-родитель, wrapper пошёл на security. secret_sources.xml ПОТЕРЯН →
  в VERDICT-лист перепрогона base: + secret_sources (itog: gateway, monitoring, acp_adapter,
  e2e, secret_sources). Счётчики 19:18: base 31/37, merged 15/37 (gateway chunk жив, CPU 0.33с/10с).
- 19:30 тик: base чанк security повис по той же сигнатуре (лог заморожен с 19:18:13, тики
  процесс-группы 0/10с, воркеры xdist defunct) → убит только pytest-родитель 3954391, wrapper
  тронул security.done  (rc=143) и пошёл дальше (skills rc=3 за 6с — WorkerController-смерть на
  старте, НО skills.xml записан 243КБ, имена фаилов живы; затем tools). security.xml ПОТЕРЯН
  (junit пишется только в конце сессии, а она убита) → security в
  VERDICT-лист перепрогона base: + security. Счётчики 19:30: base 33/37 (tools идёт), merged
  15/37 (chunkmerged3 активен, CPU-delta 828мс/6с — жив). Git: more-mem-layer == origin
  (6d429f8daa), рабочее дерево чисто; ToDo.md: нет «ждёт агента», нет самозакрытых;
  журнал гейтвея 15 мин — только WARNING чужих tool_executor, не дефект.
- 19:4x tick: **base ALL-DONE 19:36:05 (37/37)** — юнит chunkbase failed = штатный
  выход обёртки с rc≠0, не авария. Инвентарь _meta+xml (NOXML = junit потерян):
  VERDICT-лист перепрогона base — AUTHORITATIVE: acp_adapter, e2e, gateway (rc=143),
  hermes_events (rc=3 18:36, xml НЕ записан — НОВЫЙ в списке), monitoring (rc=137),
  secret_sources (rc=143), security (rc=143) = 7 чанков. Остальные 30 чанков base
  имеют xml с именами фаилов (rc=1/3 — данные живы; rc=5 — чанки без тестов).
- merged: chunkmerged3 жив (cgroup cpu.stat delta 1.07с/10с), gateway-чанк @99%,
  15/37 done. Ожидать ALL-DONE merged → снять merged NOXML-инвентарь → перепрогон
  объединённого списка НА ОБОИХ лентах → diff исполнённых имён по XML → VERDICT.

- 03.11 ~20:0x tick: base ALL-DONE (37/37, 19:36:05). merged: 15/37 done, chunkmerged3 alive (cpu.stat +1.26s/10s), gateway chunk @99%. rerun-both-lanes остаётся: acp_adapter, e2e, desktop, gateway (base), + финальный NOXML-инвентарь merged по его ALL-DONE.
- 03.11 20:0x tick (второй): merged gateway-чанк ЗАВИС: лог заморожен 19:25→20:04 @99%, cgroup-юнит ~10% ядра, worker-зомби 3543834. Убит только pytest- родитель (PID 3543354) 20:05:54 → обёртка поставила gateway rc=143 + .done (16/37), лента пошла hermes_cli (лог живой, 4-5% @20:06). Осиротевшие менеджеры/forkserver (3543357/3543827/3543831/3543839/3796357/3722781) вычищены. merged gateway.xml ПОТЕРЯН → gateway в перепрогоном списке обеих лент (уже был от base). Git: more-mem-layer синхронизирован с origin (6d429f8daa), дерево чисто; журнал гейтвея 15 мин пуст; ToDo: нет «ждёт агента», нет самозакрытых.

- 03.11 20:2x tick: merged hermes_cli rc=3 в 20:12 (INTERNALERROR KeyError gw5), НО xml записан
  (4.4 МБ, имена фаилов живы) — в перепрогон не идёт. Следом hermes_events ЗАВИС по криту
  (лог заморожен 20:12:26, 6 дефunct-воркеров, cgroup CPU 0.4мс/6с) → убит только pytest-родитель
  4190410 (kill_pid.sh) → rc=137, hermes_events.xml ПОТЕРЯН → merged-NOXML += hermes_events (в
  перепрогонном списке обеих лент уже был от base). Обёртка прошла hermes_platform rc=3 (xml на
  диске 24КБ) и идёт hermes_state. Счётчики 20:2x: merged 19/37, base ALL-DONE. Сирот/давления нет
  (mem avail 86G). Git main-чекаута: HEAD==origin/more-mem-layer (6d429f8daa), дерево чисто;
  гейтвей 15 мин — только WARNING tool_executor этой же сессии; ToDo.md: нет «ждёт агента»,
  нет самозакрытых. Ждём ALL-DONE merged → NOXML-инвентарь merged → перепрогон единого списка на
  обеих лентах → diff имён по XML → VERDICT → push upstream-sync-20261103 (FF).

- 03.11 21:1x tick: **merged ALL-DONE 20:58:51 (37/37)**. На тике вскрыты и разморожены
  два зависших чанка merged (убит только pytest-родитель): secret_sources (лог молчал
  с 20:35, CPU-cgroup 0.5мс/8с) rc=143 20:43:59, security (тот же критрий) rc=143 20:49:41.
  NOXML merged (authoritative): acp_adapter, desktop, e2e, gateway, hermes_events,
  secret_sources, security. NOXML base: acp_adapter, e2e, gateway, hermes_events,
  monitoring, secret_sources, security. Объединённый перепрогон = 8 чанков.
  Пущены обе лексы перепрогона (chunkmergedr/chunkbaser, -n5, MemoryMax=24G), но
  ПРОМЕЖУТОЧНЫЙ разбор XML обеих лент (scratch/cmp_round1.json, reg_round1.txt) показал:
  в раунде-1 ПОЧТИ ВСЕ чанки на обеих лексах завершились rc=3 (WorkerController-шторм),
  junit-ы битые на обеих сторонах (пример: merged hermes_cli — 7721 setup-'error'
  каскадом после краха + ConfigBackendUnavailable HERMES_CONFIG_INSTANCE_ID × 620).
  Имя-равенство по битым XML — артефакт, не сигнал. РЕШЕНИЕ: раунд-2 остановлен
  (kill_lanes3.sh), все .done/xml/log сняты в scratch/rerun_backup/{r1_*,round2_*}/,
  поднят ПОЛНЫЙ чистый прогон driver v3 (scratch/run_chunks_v3.sh): чанк принимается
  только при rc∈{0,1} И без INTERNALERROR в логе, до 3 попыток, уникальный basetemp
  на попытку, hard timeout -k 60 14400 на попытку (страховка от deadlock-сигнатуры).
  Юниты: chunkmergedv3 (hermes-sync-up) / chunkbasev3 (hermes-sync-base), -n5, 24G.
  Ожидаемое время 6–10ч. TICK-CHECK: `systemctl --user list-units 'chunk*v3'`,
  NO-CLEAN-ATTEMPT в _meta.txt, счётчики .done, frozen-крит (лог стоит И CPU-cgroup→0)
  → kill только pytest-родителя. По ALL-DONE обеих лекс: сравнение fail-имён по
  COMMON executed-множествам (scratch/regression_cmp.py) + triage каскадов (classify_xml.py)
  → VERDICT сюда → push origin upstream-sync-20261103 (FF). Гейтвей-журнал 15 мин чист;
  more-mem-layer синхронизирован с origin (6d429f8daa), дерево чисто; ToDo.md — секции
  «ЖДЁТ ПРИЁМКИ ВЛ» больше нет (перекомпонована 03.11 00:34, приёмочные маркеры «— Вл»
  инлайн), самозакрытых и «ждёт агента» строк нет.
- 03.11 23:4x tick: лексы идут driver v4 (юниты chunkmergedv4b / chunkbasev4b, -n3,
  старт ~23:29; merged заСЕЕДИЛ acp_adapter/agent из артефактов раунда-1).
  WorkerController-шторм не ушёл: обе лексы уперлись в xdist-deadlock в течение 15 мин
  (merged ci.a1 @94%, base controller.a1; сигнатура `[gwN] node down: Not properly
  terminated` + CPU всего cgroup 0 тиков/20с). Убиты только pytest-родители → rc=143
  23:44:36, обёртки возобновились: merged — ci a2 (в работе), base — controller a2 rc=3
  принят по правилу v4 (артефакт scheduling-краха, 5.2KB junit) и перешла на cron a1.
  Живость после разморозки подтверждена: CPU pytest-деревьев 16.8с/20с. Память машины в
  норме (7/93 ГБ used) — шторм не OOM-ный. .done: merged 2/37, base 7/37. Для VERDICT:
  base controller.xml — scheduling-артефакт, при сборке common executed-множеств
  сверять число исполненных тестов по чанкам (regression_cmp.py), не только имена.
  Прочие проверки тика чистые: журнал гейтвея 15 мин — 1 строка без ошибок;
  more-mem-layer == origin (1ec0bed46c), дерево hermes-agent чисто; ToDo — нет
  самозакрытых и «ждёт агента».

## 04.11 01:3x tick (v4b, шторм 15-мин цикл)
- Merged лекса зависла на desktop a3 (`node down` шторм; CPU cgroup 0мс/30с, лог статичен
  с 01:15) → разморожена убийством только pytest-родителя 1001660 → desktop a3 rc=143
  01:28:24, NO-CLEAN-ATTEMPT 01:28:29, перешла на docker a1. Desktop теперь NO-CLEAN
  СИММЕТРИЧНО на обеих лексах (base 01:15:06) — подтверждён в списке rerun-both-lanes.
- Обе лексы живы после тика: merged 48мс/10с (старт docker a1), base 952мс/10с (docker a2).
  .done: merged 10/37, base 11/37.
- Прочее чисто: дерево hermes-agent чисто, more-mem-layer == origin (1ec0bed46c);
  журнал гейтвея 15 мин — 0 ERROR; ToDo — нет «ждёт агента» и самозакрытых.

## Тик 04.11 06:0x–06:2x — ENOSPC-инцидент и перезапуск дорожек v5
- ~06:03 корень / заполнился (драйвер v4 не чистил /tmp/pt-*; /tmp/pt-base-gateway-a1 нарос до 109G).
  Гейтвей-лог: OSError 28, «kanban dispatcher: tick failed». Враннер-дорожки умерли молча.
- «ALL-DONE» 06:08/06:09 — ЛОЖНЫЙ: с 06:03 по 06:09 ~22 чанка на обеих дорожках отвалились
  мгновенными rc=120 (ENOSPC) → NO-CLEAN. Реальное покрытие осталось только у 10 чанков/дорожку
  (acp_adapter*, agent*, ci, computer_use, conformance, controller, cron, dashboard(rc=0), docker,
  e2e base-частично; merged e2e ZERO). gateway.xml = 0 байт с обеих сторон.
- Принято решение без Вл (инцидент, обратимо): удалены basetemp мёртвых дорожек + UUID-dir'ы >24ч,
  освобождено ~246G, диск 71% (268G свободно). Дорожки v4 умерли вместе с диском сами.
- Перезапуск точечным драйвером v5 (scratch/run_chunks_v5.sh, список scratch/rerun_chunks.txt,
  25 чанков; ГЛАВНОЕ отличие — удаление basetemp сразу после каждого чанка). Юниты
  chunkbasev5/chunkmergedv5 (-n3, MemoryMax=64G), выход в chunks_{base,merged}_r2/. Старт 06:24,
  обе живые на gateway a1.
- ИТОГОВОЕ покрытие к VERDICT = union(старые chunks_*/*.xml по 10 валидным чанкам) + новые chunks_*_r2.
  Совместимые-пустые (compat/fakes/fixtures rc=5 no-tests) — симметричны, не гэп.
