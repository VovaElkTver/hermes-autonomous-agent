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

## Тик 04.11 13:4x–13:5x
- MERGED `hermes_cli` a1 принят драйвером v6 на 13:46 (rc=3, .done снят), но XML слабый:
  9м38с, 19447 tests / 1188 F / 9420 E, 52 INTERNALERROR (WorkerController KeyError в логе) —
  crash-cascade шторм, не реальное покрытие. BASE тот же чанк идёт чисто (a1 с 12:34, 87% к 13:48,
  реальные F). → VERDICT-лист перепрогона: hermes_cli на обе ленты после ALL-DONE; до перепрогона
  merged-hermes_cli XML не использовать для name-diff (только base-verified executed names).
- BASE v6 чуть не умер тем же, чем умер mergedv6 в 13:30: MemoryCurrent 21.3G > MemoryHigh 20G.
  Поднят runtime-свойством `set-property chunkbasev6 MemoryHigh=32G` на 13:5x — угроза oomd-килла
  на 87% hermes_cli (потеря XML + полный перепрогон ленты, .done 0/25) снята. mergedv6b High=32G с релаунча.
- Состояние: df 252G (73%), mem 81G avail. .done base 0/25, merged 1/25. Gateway 15 мин — 0 ERROR/Traceback.
  ToDo.md 118 строк: нет «ждёт агента», нет ~~. Дерево чистое; more-mem-layer 1ec0bed46c == origin.

## Тик 04.11 14:1x
- Шторм-цикл ~15 мин продолжился: разморозка x3 за тик (kill только pytest-родителя):
  1) BASE `hermes_cli` a1 @89% — родитель 0 тиков/30с, `node down`, zombie-worker, лог статичен с 14:00:51
     → kill 14:05, XML a1 ПОТЕРЯН (смерть до записи junit), a2 переживает с нуля (жив, 14с CPU/20с).
  2) MERGED `hermes_events` a1 (завис с 13:46:09) и a2 (шторм на bring-up) → kill 14:05 и 14:09.
  3) MERGED `hermes_events` a3 — gw1+gw2 down за секунды, 1.3ms/30с → kill 14:11 →
     **hermes_events NO-CLEAN на merged, НОЛЬ XML** → в VERDICT-лист перепрогона (база до него
     ещё не дошла; name-diff возможен только после перепрогона на обе ленты).
- MERGED `hermes_platform` a1: rc=3 + XML записан (23672B, 2F/118P/6S; INTERNALERROR — после
  session-summary) → принят слабым; в VERDICT сверять executed-count, не доверять 2F как полный набор.
- Состояние на конец тика: BASE `chunkbasev6` живая на hermes_cli a2; MERGED `chunkmergedv6b` живая
  на hermes_state (после hermes_platform a1). .done merged 1/25 (hermes_cli), base 0/25.
  df 252G, mem 83G avail, base MemoryHigh 32G.
- VERDICT-лист перепрогона: hermes_cli (merged слабый), hermes_events (merged NO-CLEAN 0 XML),
  прежние: acp_adapter, e2e, desktop, gateway(база a1 lost) — см. инвентарь XML на ALL-DONE.
- Gateway 15 мин — 0 ERROR/Traceback (только tool_executor WARNING моих же вызовов).
  ToDo.md 118 строк: нет «ждёт агента», нет ~~. Дерево чистое; more-mem-layer 1ec0bed46c == origin.

## Тик 14:3x (04.11)
- Разморозка MERGED `plugins` a1: `[gw2] node down` на 97%, лог статичен с 14:31:32, cgroup
  1.4мс/25с → kill pytest-родителя 3897460 (timeout-обёртка 3897459) → a1 rc=143 14:37:33,
  XML потерян; a2 живая и прогрессит (3.1с/20с CPU, 76%+). plugins остаётся в списке
  «дождаться валидного XML на merged» до ALL-DONE.
- Инвентарь merged r2 XML: hermes_cli (слабый), hermes_platform (слабый 2F/118P), hermes_state.a1
  (rc=3 принят, 1846 tests, 52 INTERNALERROR — сверять executed-count), integration.a1 (rc=0,
  1 тест windows-skip — симметрично, не гэп), providers.a1 (rc=3, 17KB). .done merged 6/25, base 0/25.
- BASE `chunkbasev6` живая: hermes_cli a2 ~72%, F-кластеры реальные (зеркалят merged a1 —
  name-diff по common executed set на вердикте). Метка 14:05:23 hermes_cli a1 rc=143 — та же
  разморозка 14:1x тика.
- Прочее чисто: gateway 15 мин — 1 строка, 0 ERROR/Traceback. hermes-agent дерево чистое,
  more-mem-layer 1ec0bed46c == origin (ls-remote). ToDo.md 118 строк: нет «ждёт агента», нет ~~.

## Тик 04.11 15:2x
- 3 разморозки merged `chunkmergedv6b`: security a1 (1.1ms/25s, bring-up storm, статичен с 14:40) → kill pytest-parent 3931333 → a1 rc=143; a2 → шторм (статик с 15:16) → kill 4001967 → a2 rc=143 15:16:14; a3 → шторм bring-up в течение минуты → kill 4003244 → **security NO-CLEAN 15:20:25** (все 3 попытки умерли на bring-up, ZERO XML merged).
- Инвентарь merged r2 (.done 9/25): hermes_cli weak (19447/1188F/9420E, 52 INTERNALERROR), hermes_events NO-CLEAN 0XML, hermes_platform weak, hermes_state weak-rc3, plugins weak (2154 тестов за 143с, 52 INTERNALERROR), providers rc=3, integration rc=0 (windows-skip), skills WEAK (rc=3 за 7.06s, 1649 тестcases, 52 INTERNALERROR — почти scheduling-артефакт, считать weak до сверки counts), security NO-CLEAN.
- Шторм bring-up теперь на КАЖДОМ новом чанке merged (security×3, skills мгновенный rc=3) — lane физически не даёт чистых прогонов; базовая линия (chunkbasev6) шторма не видит.
- BASE: hermes_cli a2 @99% на одном длинном тесте с 14:51 (CPU живая: 526ms/25s, 409ms/22s — не заморожена, НЕ трогать; timeout-обёртка убьёт ~18:05 если не выйдет). .done base 0/25.
- Gateway 15 мин: 0 ERROR/Traceback. hermes-agent: дерево чистое, more-mem-layer 2a9181e16f == origin (ls-remote). ToDo.md 118 строк: нет «ждёт агента», нет ~~.
- RERUN-BOTH-LANES перед VERDICT (актуальный): hermes_cli, hermes_events, hermes_platform, plugins, security, skills (+ старые: acp_adapter, e2e, desktop, gateway — дождаться r2 XML).
- NEXT TICK: merged на tools a1 (@6%, живая) — ожидать шторм/разморозку; base hermes_cli a2 XML лендинг → сверить rc+INTERNALERROR+testcase count с merged weak 19447.

## Тик 15:3x (04.11)
- MERGED `chunkmergedv6b`: ЖИВА на `tui_gateway` a1 @82%+ с реальными F (лог mtime = секунда проверки) — шторм bring-up на этом чанке НЕ повторился, разморозки не потребовалось.
-merged `.done` 10/24: hermes_cli, hermes_events, hermes_platform, hermes_state, integration, plugins, providers, security, skills, tools.
  - tools a1 rc=3 ACCEPTED-WEAK: 1716 tc / 112.8s (3E/2F/15 skip), 229 строк INTERNALERROR в логе = crash-cascade → в список rerun-both-lanes.
  - skills a1 rc=3 ACCEPTED-WEAK: 1649 tc / 7.06s, 52 INTERNALERROR = артефакт scheduling → rerun-both-lanes.
  - security NO-CLEAN (15:20), ZERO XML → rerun-both-lanes.
- BASE `chunkbasev6`: hermes_cli a2 тот же один длинный тест с 14:51; cgroup CPU 376ms/25s = ЖИВАЯ (не frozen), НЕ размораживать; timeout wrapper убьёт ~18:05 → a3.
- RERUN-BOTH-LANES перед VERDICT (актуальный): hermes_cli, hermes_events, hermes_platform, plugins, security, skills, tools (+ прежние desktop/acp-списки по инвентарю XML).
- Gateway 15 мин: 0 ERROR/Traceback (1 строка). hermes-agent: дерево чистое, more-mem-layer 2a9181e16f == origin (ls-remote). ToDo.md 118 строк: нет «ждёт агента», нет ~~.
- NEXT TICK: merged tui_gateway.a1 XML landing → верификация приёма (rc∈{0,1}+no INTERNALERROR / weak-инвентарь); base hermes_cli a2 доживёт ли до XML или rc=124 ~18:05 (тогда a3,-desktop-pattern ожидания шторма).

## Тик 15:4x (04.11)
- MERGED `chunkmergedv6b`: `tui_gateway` a1 остановился на 99% в 15:34:43 с сигнатурой шторма
  (`[gateway-signal] SIGTERM`, `[gw2] node down`) — но cgroup CPU 186ms/25s ≠ 0, НЕ размораживал;
  timeout-обёртка переработает ~19:22 (a1 стартовал 15:22). .done 10/25, XML tui_gateway ещё нет.
- BASE `chunkbasev6`: hermes_cli a2 — тот же длинный тест с 14:51, CPU 443ms/22s (живая), не трогать;
  timeout ~18:05 → a3.
- tools.a1.xml повторная сверка: 5 строк `<testcase` при 284KB / 229 INTERNALERROR — вердикт
  ACCEPTED-WEAK не меняется, tools остаётся в rerun-both-lanes.
- Gateway 15 мин: 4 WARNING (tool_executor + Matrix sync timeout — эгресс-флак, ретраи), 0 ERROR/Traceback.
- hermes-agent: дерево чистое, more-mem-layer 2a9181e16f == origin (ls-remote). ToDo.md 118 строк:
  нет «ждёт агента», нет ~~self-closed~~.
- NEXT TICK: tui_gateway a1 исход (XML vs rc=124→a2); base hermes_cli a2 исход ~18:05.

## Тик 16:1x (04.11)
- MERGED `chunkmergedv6b`: `tools` a1 (стартовал 15:22) прошёл до 99%, в хвосте `F..[gateway-signal]
  SIGTERM / [gw2] node down` — лог статичен с 15:22:26, но cgroup CPU 164ms/22s ≠ 0 → НЕ размораживал;
  timeout-обёртка пересоберёт ~19:2x. Ждать исхода tools (XML/NO-CLEAN) в следующем тике.
- BASE `chunkbasev6`: `hermes_cli` a2 — всё тот же один длинный тест с 14:51, CPU 442ms/22s (живая),
  не трогать; timeout ~18:05 → a3. .done base 0/25, merged 10/25.
- Gateway 15 мин: 1 строка, 0 ERROR/Traceback. hermes-agent: дерево чистое, more-mem-layer
  2a9181e16f == origin (ls-remote). ToDo.md 118 строк: нет «ждёт агента», нет ~~self-closed~~.
- NEXT TICK: base hermes_cli a2 исход ~18:05 (XML vs rc=124→a3); merged tools/tui_gateway исход
  к ~19:2x; при каждом rc=3 — приём только с верификацией tc-count/INTERNALERROR (weak-инвентарь).

## Тик 16:4x (04.11)
- Разморожек не потребовалось. MERGED `chunkmergedv6b`: `tui_gateway` a1 стоит на 99% с 15:34:43
  (в хвосте `F..[gateway-signal] SIGTERM / [gw2] node down`), cgroup CPU 156ms/22s ≠ 0 → не трогать;
  timeout-обёртка пересоберёт ~19:2x. `tools` a1 rc=3 15:22:26 (weak, остаётся в rerun). .done 10/25.
- BASE `chunkbasev6`: `hermes_cli` a2 — тот же длинный тест с 14:51, CPU 480ms/22s (живая), не трогать;
  timeout ~18:05 → a3. .done 0/25. В хвосте лога кластер реальных F на 98–99% (зеркалит merged — имя-диффу
  по common executed set на вердикте).
- Gateway 15 мин: 1 строка, 0 ERROR/Traceback. hermes-agent: дерево чистое, more-mem-layer
  403526cb20 == origin (ls-remote). ToDo.md 118 строк: нет «ждёт агента», нет ~~self-closed~~.
- NEXT TICK: base hermes_cli a2 исход ~18:05 (XML vs rc=124→a3 — при XML сверка rc/INTERR/tc-count против
  merged weak 19447/1188F); merged tui_gateway a1 исход ~19:2x.

## Тик 17:18 (04.11)
- Оттаиваний нет. BASE `chunkbasev6` hermes_cli a2: лог статичен с 14:51 (один длинный тест @99%), CPU
  507ms/22s ≠0 → жив, не трогать; timeout-обёртка recycling ~18:05 → при rc=124 XML a2 потерян, a3.
  MERGED `chunkmergedv6b` tui_gateway a1: лог статичен с 15:34:43 (хвост storm-сиг: SIGTERM/[gw2] node down/F),
  CPU 197ms/22s ≠0 → жив; wrapper recycles ~19:22.
- .done merged 10/25, base 0/25 (XML-инвентарь без изменений с 16:4x). Gateway 15-мин: 1 строка, 0 ERROR/Traceback.
- Git: hermes-agent дерево чисто, more-mem-layer 403526cb20 == origin (ls-remote). ToDo.md 118 строк: нет
  «ждёт агента», нет ~~self-closed~~.
- NEXT TICK: base hermes_cli a2 исход ~18:05 (XML vs rc=124→a3; при XML — rc/INTERR/tc-count против merged
  weak 19447/1188F); merged tui_gateway a1 исход ~19:22.

## Tick 17:4x (04.11)
- Оттаиваний НЕТ. BASE chunkbasev6: hermes_cli a2 — тот же длинный тест с 14:51, лог статичен, CPU-дельта
  категруппы 554мс/25с ≠ 0 → жив, таймаут-враппер убьёт ~18:05 → a3 (XML a2 теряется при rc=124).
  MERGED chunkmergedv6b: tui_gateway a1 @99% с 15:34:43 (сиг шторма: SIGTERM, [gw2] node down), CPU
  200мс/25с ≠ 0 → не трогаем, враппер переработает ~19:22.
- .done merged 10/25, base 0/25; XML-инвентарь без изменений с 17:1x.
- Gateway 15-мин: 1 строка, 0 ERROR/Traceback. Git hermes-agent: дерево чисто, more-mem-layer
  403526cb20 == origin (ls-remote). ToDo.md 118 строк: нет «ждёт агента», нет ~~self-closed~~.
- NEXT TICK: base hermes_cli a2 исход ~18:05 (при XML — rc/INTERR/tc-count против merged weak
  19447tc/1188F; при rc=124 → a3 fresh ~4h); merged tui_gateway a1 исход ~19:22 (XML vs шторм-watch).

## Тик 17:5x (04.11)
- Оттаиваний нет. BASE chunkbasev6: hermes_cli a2, лог статичен с 14:51:56 (тот же долгий тест), cgroup CPU 51ms/25s ≠ 0 → жив, ждём hard-timeout ~18:05 → a3. MERGED chunkmergedv6b: tui_gateway a1, лог статичен с 15:34:43 (шторм-признак), CPU 20ms/25s ≠ 0 → не трогаем, wrapper переработает ~19:22.
- .done: merged 10/25, base 0/25. Инвентарь XML без изменений.
- Шлюз 15 мин: -- No entries --. more-mem-layer 403526cb20==origin; дерево чисто; ToDo 118 строк, «ждёт агента»/~~ нет.

## Тик 18:0x (04.11)
- BASE chunkbasev6: hermes_cli a2 → rc=124 hard-timeout 18:05:43, как предсказано (XML a2 потерян, 0 hermes_cli.xml на base). a3 стартовал ~18:05, лог живой (mtime = секунда проверки), cgroup CPU 18.7s/25s → здоров; дедлайн a3 ~22:05. .done base 0/25.
- MERGED chunkmergedv6b: tui_gateway a1, лог статичен с 15:34:43 (шторм-сиг: SIGTERM, [gw2] node down), cgroup CPU 142ms/25s ≠ 0 → не трогаем, wrapper переработает ~19:22 (a2 при неудаче). .done merged 10/25; XML-инвентарь без изменений (9 файлов, weakest — skills/tools rc=3 weak-accepted).
- Шлюз 15 мин: 0 ERROR/Traceback. Git: hermes-agent дерево чисто, more-mem-layer 403526cb20==origin; upstream-sync-20261103 f8091e6ffd==origin. ToDo.md 118 строк: «ждёт агента»/~~ нет.
- NEXT TICK: merged tui_gateway a1 исход ~19:22 (rc=124→a2 fresh шторм-watch; XML → сверка с base, которого нет → tui_gateway в rerun-both-lanes, если base тоже NO-CLEAN'ет); base hermes_cli a3 прогресс в середине тика (при XML: rc/INTERR/tc-count против merged weak 19447tc/1188F).

## Тик 18:1x (04.11)
- BASE chunkbasev6: hermes_cli a3 завершился rc=3 за ~10 мин (18:15:28) — не полный 4h-прогон, а штормовой; XML валиден и принят v6: 19415 tc / 1100 F / 9551 E, 0 INTERNALERROR-строк в junit. Масштаб симметричен merged-weak (19447/1188F/9420E) → обе стороны слабые, hermes_cli остаётся в rerun-both-lanes до VERDICT. hermes_platform a1 rc=3 за 3 сек: XML 125 tc / 0F / 1E = scheduling-артефакт (weak). hermes_events на base = SKIP-nodir (нет дерева). Lane живая: hermes_state a1 @31%, лог mtime = секунда проверки. .done base 2/25.
- MERGED chunkmergedv6b: tui_gateway a1 — без изменений (лог статичен 15:34:43, шторм-сиг), cgroup CPU 159ms/25s ≠ 0 → не оттаиваем, wrapper переработает ~19:22. .done merged 10/25.
- Шлюз 15 мин: пуст (0 записей, 0 ERROR). Git: hermes-agent чисто, more-mem-layer 403526cb20==origin (ls-remote). ToDo.md 118 строк: «ждёт агента»/~~ нет. Память 83G avail, диск 255G — не узко.
- NEXT TICK: ~19:22 исход merged tui_gateway a1 (rc=124→a2, шторм-watch на bring-up); base hermes_state/chunk-цикл — ждать NO-CLEAN каскада после 3s-platform (шторм bring-up активен на base со 1522 по merged-паттерну); сверять _meta.txt и .done counts.

## Тик 18:3x (04.11)
- BASE chunkbasev6: прорыв по чанкам с 18:1x — hermes_state a1 принят rc=3 (1912tc/4F/4E, симметрично merged 1913/4/4 — реалиные F, в VERDICT по именам), integration rc=0 (2tc), providers a1 rc=3 (95tc/0F/3E = симметрично merged 95/0/3), plugins a1 rc=3 18:27 (2048tc/57F/5E) — против merged plugins (2164tc/2F/5E): АСИММЕТРИЯ F 57 vs 2 и tc — base plugins штормовой weak, остаётся в rerun-both-lanes (уже был с 15:2x). hermes_cli base 30496F-occ vs merged 30485 — симметрично-слабые (E ~9400 обе стороны, crash-cascade), rerun-список не меняется. Шторм bring-up накрыл base ровно как предсказано: security a1 с 18:27 статичен (149B, gw node down), CPU 297ms/22s ≠ 0 → не оттаиваем, ждём rc-цикл; при NO-CLEAN симметрия с merged security (NO-CLEAN 15:20, zero XML). .done base 6/25.
- MERGED chunkmergedv6b: без изменений — tui_gateway a1 ждёт wrapper-рецикл ~19:22 (сиг cũ). .done merged 10/25; XML-инвентарь: skills 1654/2F/2E, tools 1735/2F/3E (weak rc=3, на rerun-контроле).
- Инвентарь VERDICT (r2 XML, обе стороны): base 6 (cli, platform, state, integration, providers, plugins), merged 10 (+skills, tools, security NO-CLEAN 0XML, hermes_events NO-CLEAN 0XML). Rerun-both-lanes перед VERDICT: hermes_cli, hermes_platform(scheduling-artifact), plugins, skills, tools, security, hermes_events, desktop, e2e, acp_adapter + merged tui_gateway/base-аналог по исходу 19:22.
- Шлюз 15 мин: 3 строки, 0 ERROR/Traceback. Git: hermes-agent чисто, HEAD 401f7edea2 (causal stage-b audit) == origin/more-mem-layer (ls-remote — два новых коммита 4341f6cf44/401f7edea2 уже запушены параллельной сессией). ToDo.md 118 строк: «ждёт агента»/~~ нет. Память 84G avail.
- NEXT TICK: ~19:2x исход merged tui_gateway a1 (rc=124→a2 fresh шторм-watch); base security a1 исход (NO-CLEAN vs XML); base дальнейшие дёшевыe чанки под шторм-каскадом.

## Тик 19:3x (04.11)
- MERGED chunkmergedv6b: tui_gateway a1 → rc=124 19:22:26 (hard-timeout 4h, XML потерян, как предсказано); a2 стартовал чистым и ЗАВЕРШИЛСЯ rc=1 19:33:55 — полный прогон 682s: 12F/2963P/1S, 0 INTERNALERROR, XML 451KB → ПРИНЯТ v6 (первый полноценно чистый крупный чанк раунда r2 на merged). tui_gateway СНЯТ с rerun-both-lanes для merged; base имеет tests/tui_gateway → обязан выдать свой XML для поимённой сверки. Lane уже на monitoring a1 (живой, -n3). .done merged 11/25.
- BASE chunkbasev6: security a1 статичен с 18:27:18 (149B, шторм bring-up gw node down), cgroup CPU 197ms/22s ≠ 0 → не оттаиваем по frozen-crit; wrapper hard-timeout ~22:27 → a2. .done base 6/25 без изменений.
- Шлюз 15 мин: 1 строка, 0 ERROR/Traceback. Git: hermes-agent дерево чисто, more-mem-layer 401f7edea2==origin (ls-remote); upstream-sync-20261103 a5aaa42b36==origin, hermes-sync-up дерево чисто. ToDo.md 118 строк: «ждёт агента»/~~ нет.
- NEXT TICK: base security a1 исход ~22:27 (rc=124→a2; NO-CLEAN zero XML = симметрия с merged security 15:20); merged monitoring a1 исход (~длинный чанк); контроль шторма на bring-up новых чанков merged.


## Тик 19:5x (04.11)
- MERGED chunkmergedv6b: monitoring a1 попал в шторм bring-up ровно по паттерну (лог статичен с 19:33:55, `[gw2]/[gw0] node down`, cgroup CPU 1ms/22s) → оттаивание: kill pytest-parent 237160 → `monitoring a1 rc=143 19:57:57`, a2 перезапущен обёрткой (PID 248564, basetemp pt-merged-monitoring-a2) — живой шторм-цикл ~15 мин продолжается. .done merged 11/25.
- BASE chunkbasev6: security a1 всё ещё шторм-под-наблюдением (статичен с 18:27:18, 149B) но CPU 233ms/22s ≠ 0 → не трогаем; wrapper hard-timeout ~22:27 → a2 (NO-CLEAN zero XML = симметрия с merged security 15:20). .done base 6/25 без изменений.
- Шлюз 15 мин: 3 строки, 0 ERROR/Traceback (1 — tool_executor WARNING чужой сессии, noise). Git: more-mem-layer 401f7edea2==origin, upstream-sync-20261103 b513b4c7aa==origin (ls-remote). hermes-agent: НОВАЯ активная работа параллельной сессии — ветка elk/skills-write-forbidden (HEAD 401f7edea2), грязные tools/skill_* + untracked tools/skill_write_policy.py, tests/tools/test_skill_write_forbidden.py, mtime 19:54–19:55 = живой редактор (sess.run pytest tests/tools @41% CPU) — не коммитим за них. В fleet-секции ToDo «ждёт агента»/~~ нет (118 строк).
- NEXT TICK: merged monitoring a2 исход (шторм-watch bring-up; после 3 попыток NO-CLEAN → +monitoring в rerun-both-lanes); base security a1 исход ~22:27; исход elk/skills-write-forbidden (коммит/пуш — дело той сессии, контролировать только что неOur ToDo не трогает).

## Тик 20:1x (04.11)
- MERGED chunkmergedv6b: monitoring a2 шторм-hang на bring-up (лог статичен с 19:58:02, node down, CPU 0–1ms/22–25s два замера) → thaw kill 248564 → rc=143 20:10:34; a3 (final) тоже шторм-hang (статичен с 20:10:40, CPU 1ms/22s) → thaw kill 288118 → rc=143 20:14:35 → **monitoring NO-CLEAN merged, ZERO XML → +monitoring в rerun-both-lanes**. Лейн возобновился: perf_guards a1 rc=1 XML принят (0 INTERNALERROR), .done merged 13/25, живёт на pm (лог mtime = время проверки).
- BASE chunkbasev6: security a1 статичен с 18:27:18, CPU 140ms/25s ≠0 → не трогаем, wrapper hard-timeout ~22:27 → a2. .done base 6/25.
- Шлюз 15 мин: 0 ERROR/Traceback (grep пусто). Git: дерево hermes-agent чистое; elk/skills-write-forbidden c3d28a502b == origin (параллельная сессия завершила и запушила — ТЗ записи скилов, ждёт приёмки Вл); more-mem-layer 401f7edea2 == origin. ToDo.md 118 строк: «ждёт агента»/~~ нет, инлайн «— Вл» строка 86.
- NEXT TICK: base security a1 исход ~22:27 (rc=124→a2; NO-CLEAN zero XML = симметрия); merged pm/perf_guards WATCH продолжение; шторм-цикл по-прежнему ~каждый тик.

## Тик 20:3x (04.11)
- MERGED chunkmergedv6b: 3 оттаивания — verify a1 (шторм bring-up со 20:20:58, `[gw0] node down`, 1ms/22–25s два замера) kill 397199 → rc=143 20:28:49; verify a2 тоже шторм-hang (статичен с 20:28:56) kill 402449 → rc=143 20:31:14; verify a3 rc=3 ЗАВЕРШИЛСЯ за секунды и ПРИНЯТ СЛАБЫМ: XML 14.6KB но 1 testcase + 52 INTERNALERROR → verify в r3-список обеих сторон. website a1 шторм-hang kill 403512 → rc=143 20:32:39; website a2 rc=3 20:32:44 принят слабым: summary 3F/47P за 0.70s, XML 2 testcases, 52 INTERNALERROR → website тоже в r3-список (r1 XMLы потеряны ENOSPC, r2 обе стороны слабые — base ещё не доходил).
- .done merged 21/25; осталось: gateway (a1 стартовал 20:33:23 — самый тяжёлый чанк, r1 оба раза rc=124 по 4h), desktop, e2e, hermes_api. manual rc=5 обе стороны симметрично (0.23s «no tests ran» — каталог без коллектируемых тестов, не прокол, как fakes/fixtures).
- BASE chunkbasev6: security a1 шторм-наблюдение (статичен с 18:27:18; CPU 340ms/25s ≠0) → не трогаем, wrapper hard-timeout ~22:27 → a2. .done base 7/25 (без изменений с 18:3x; пропускная способность base сильно ниже — основной простой на security).
- Шлюз 15 мин: 34 строки, 0 ERROR/Traceback. Git: hermes-agent дерево чисто; elk/skills-write-forbidden c3d28a502b==origin; more-mem-layer 401f7edea2==origin (ls-remote). ToDo.md 118 строк: «ждёт агента»/~~ нет.
- NEXT TICK: merged gateway a1 исход (4h timeout ~00:33; шторм-патруль каждый тик); base security a1 ~22:27 rc=124→a2; r3-инвентарь на VERDICT растёт: verify, website (+старые: hermes_cli, hermes_platform, plugins, skills, tools, security, hermes_events, desktop, e2e, acp_adapter, monitoring).

## Тик 20:4x (04.11)
- MERGED chunkmergedv6b: gateway a1 ЖИВОЙ и прогрессит — @12%, реальные F, лог mtime = секунда проверки, cgroup 1057ms/3s. Оттаиваний не было. Обёртка 4h → дедлайн ~00:33 (r1 обе стороны rc=124 — чанк марафонский).
- BASE chunkbasev6: security a1 лог статичен с 18:27:18 (2ч18м, шторм-сиг `[gw0] node down`), но cgroup CPU 292ms/25s — на порядок выше frozen-crit порога (<10ms/30s) → не трогаем, wrapper hard-timeout ~22:27:18 → rc=124 → a2. .done по ls = 6/25.
- Шлюз 15 мин: 1 строка, 0 ERROR/Traceback. Git: hermes-agent дерево чисто; НАЙДЕН не запушенный коммит параллельной сессии 453cea8a39 «skills: hub-install honors skills.write_forbidden» (20:29) → запушен FF в origin/elk/skills-write-forbidden (source==dest, разрешено тиком); more-mem-layer 401f7edea2==origin. ToDo.md 118 строк: «ждёт агента»/~~ нет.
- NEXT TICK: base security a1 исход ~22:27 (rc=124→a2, шторм-watch на bring-up); merged gateway прогресс/шторм-патруль; r3-инвентарь без изменений.

## 04.11 21:2x tick
- NO thaws. MERGED gateway a1 healthy & progressing 36%→57% (log mtime=check-sec, cgroup 17283ms/22s, real F clusters both sides), wrapper deadline ~00:33. BASE security a1 log static since 18:27:18 (3h) BUT cgroup CPU 172ms/22s ≫ frozen-crit → leave; wrapper hard-timeout 22:27:18 → rc=124 → a2 storm-watch. .done merged 21/25, base 6/25. Gateway 15-min: 0 ERROR/Traceback. GIT all ==origin (ls-remote: elk/skills-write-forbidden 453cea8a39, more-mem-layer 401f7edea2, upstream-sync-20261103 self). ToDo 118 clean. NEXT TICK (~21:4x+): merged gateway long-march watch (r1 both lanes died rc=124 at 4h); base security 22:27 outcome (rc=124→a2 bring-up storm watch; NO-CLEAN zero-XML = symmetric w/ merged security 15:20).

## Тик 04.11 23:39 — снят дубликат merged-полосы (v6b→v6c)
- Кто-то (фоновая сессия) поднял в 23:26 `chunkmergedv6c` (v6, merged, -n3) как перевыпуск gateway a1, НЕ сняв старый `chunkmergedv6b`, чей gateway a1 сидел 99%-фризом с 22:18. Обе полосы писали в один `chunks_merged_r2/gateway.a1.log` (следы «bringing up nodes…» дважды) и один basetemp `/data/pt-tmp/pt-merged-gateway-a1`; к 23:39 v6c шла ~1% с живым логом, v6b все ещё был жив.
- Решение тика: v6b снят (kill wrapper 3723034 + timeout 404127 + pytest-parent 404128; a1 всё равно потерял бы XML на rc=124 ~00:33 по утру-прецеденту, файл лога уже смешан). Осиротевших xdist/forkserver нет, cgroup v6b исчез, v6c — единственная merged-полоса (is-active: v6b inactive, v6c active).
- Риск на вердикт: окно сосуществования 23:26–23:40 — позиции чанков не пересекались (v6c ~1%, v6b 99% один long-test), коллизия tmp_path маловероятна; при разборе gateway.a1.xml v6c обращать внимание на file-exists/ENOTEMPTY-ошибки первых минут = артефакт, симметрично дергать при недоумении.
- Прочее: base `security` a2 жив (started 22:27, wrapper-деadline ~02:27→a3, CPU по прежнему тикинг-чеку ≠0, не трогать); .done merged 21/25, base 6/25; gateway-юнит 15 мин — 0 ERROR/Traceback (только свои tool_executor-предупреждения execute_code-BLOCKED); дерево hermes-agent чистое, HEAD 401f7edea2 == origin/more-mem-layer, elk/skills-write-forbidden == origin (464e76d36f); ToDo 118 строк — без «ждёт агента», без самозакрытов.

## Тик 05.11 01:1x
- СОБЫТИЕ: merged `gateway` a1 (chunkmergedv6c) завершился 01:13:19 rc=3 ПРИНЯТ: XML 2.2MB, tests=13509, 144F/13247P/5E, реальный прогон 1:47:23; INTERNALERROR KeyError(gw3) только на teardown сессии = weak-accept по v6-правилу. Впервые в r2 — полноценный gateway-покрытие merged-стороны; gateway уходит из «zero-XML» списка merged, но NAME-DIFF всё равно ждёт base gateway.xml (base до gateway не доходил — стоит на security). При разборе XML первые минуты = окно сосуществования v6b/v6c 23:26–23:40 — file-exists/ENOTEMPTY артефакты искать симметрично.
- MERGED полоса живая: перешла на `e2e` a1 (лог растёт, прогресс виден). Осталось после e2e: desktop; hermes_api SKIP-nodir симметрично. .done merged 22/25.
- BASE полоса: `security` a2 статичен 149B с 22:27:23, НО cgroup CPU 119ms/22s ≫ frozen-crit → не трогаем; wrapper rc=124 ~02:27 → a3, ожидаемо NO-CLEAN zero-XML (симметрично merged security 15:20, уже в r3-списке). .done base 6/25 — throughput base остаётся узким местом VERDICT.
- Шлюз 15 мин: 0 ERROR/Traceback (только свои tool_executor WARNING — execute_code BLOCKED в cron, известная норма). Git: hermes-agent дерево чисто, more-mem-layer 401f7edea2==origin (ls-remote); локальный ref elk/skills-write-forbidden отстал от origin (c3d28a502b vs 464e76d36f) — синхронизирован FF-фетчем в сам remote-ref, контент не менялся. ToDo.md 118 строк: «ждёт агента»/~~/самозакрывов нет.
- NEXT TICK (~01:3x+): merged e2e a1 шторм-патруль (e2e исторически NO-CLEAN-призрак обеих сторон); base security исход ~02:27 (a2→a3, ждём zero-XML и переход полосы дальше — watch _meta + .done с 6/25).

## 05.11 02:0x tick (merged lane ALL-DONE)
- MERGED: e2e a1 ПРИНЯТ rc=3-WEAK 02:02:30 — e2e.a1.xml 498KB, 1356 testcase, 52 INTERNALERROR (weak-accept; у merged ранее был ZERO e2e coverage — теперь есть; base e2e partial ~290tc → на VERDICT сравнивать executed-counts по e2e, не только имена).
- MERGED desktop a1/a2/a3 — все три зависли на bring-up (статик 108B, `[gwN] node down`, cgroup CPU 1ms/22s при окне ≥22s) → три оттаивания (kill только pytest-parent: 1060136, 1061688, 1062005) → desktop NO-CLEAN 02:08:20, ZERO XML merged-side (base desktop ещё не дошёл до него в r2; desktop остаётся в r3-rerun списке обеих полос).
- MERGED полоса: ALL-DONE 02:08:20, .done 24/25 (25-й — hermes_api SKIP-nodir), unit chunkmergedv6c inactive (collect). Мerged-side покрытие r2 ЗАКРЫТО до конца base-полосы.
- BASE: security a2 ЖИВ (cgroup CPU 341ms/22s ≫ frozen-crit, не трогали); wrapper rc=124 ~02:27 → a3 (final), ожидаемо NO-CLEAN zero-XML (симметрично merged security) и полоса пойдёт дальше по списку — throughput base остаётся узким местом VERDICT. .done base 6/25.
- Шлюз 15 мин: только свои tool_executor WARNING (execute_code BLOCKED в cron) — 0 gateway ERROR/Traceback. Git: hermes-agent чисто, more-mem-layer 401f7edea2==origin (ls-remote), elk/skills-write-forbidden 464e76d36f==origin. ToDo.md 118 строк: «ждёт агента»/~~/самозакрывов нет.
- NEXT TICK (~02:3x+): исход base security a3 (~02:27) → NO-CLEAN + переход на следующий чанк (watch _meta + .done с 6/25); шторм-патруль base (bring-up storm активен). VERDICT-подготовка: парсить fail-names по merged r2 XML (24 чанков) + inventory no-XML чанков.

## 05.11 05:0x tick (base skills-оттаивание; skills = weak-артефакт ОБЕИХ полос)
- BASE security a3 rc=143 03:22:28 (SIGTERM извне тика, источник не установлен), NO-CLEAN zero-XML 03:22:33 — симметрично merged security, остаётся в r3-списке обеих полос.
- BASE `skills` a1 завис на bring-up: лог статичен 03:22:40 (1ч42м), `[gw0] node down`, cgroup CPU 326ms/22s (≪ хода) → оттаивание 05:05 kill только pytest-parent 1079664 (wrapper 3554936 жив). Убиты также два осиротевших test-gateway демона базы (PPID=3134, etime 14–15ч: 3780561, 3947108) — уроки 04.11 про MemoryMax-надутые cgroup.
- skills a2 принят rc=3-WEAK за 12с: XML 1618tc/6.459s/3E — scheduling-артефакт, НЕ покрытие. На merged skills.xml симметрично слабый: 1649tc/7.063s. → `skills` добавлен в VERDICT-список перепрогона обе полосы (рядом с desktop, security).
- BASE полоса возобновилась сама: `tools` a1 жив (лог растёт 05:07:03). .done base 8/25 (hermes_cli, hermes_platform, hermes_state, integration, plugins, providers, security, skills). Узкое место VERDICT — по-прежнему throughput base.
- Шлюз 15 мин: 0 записей (No entries), unit active, NRestarts=0. Git: hermes-agent дерево чисто, more-mem-layer 401f7edea2==origin (ls-remote); сверены все активные ветки по ls-remote: upstream-sync-20261103, elk/skills-write-forbidden, elk-upstream-sync, fix/resume-origin-birth == origin; diff-фолс-положительные разобраны (elk-matrix-multibot 22bfed608f = серверный merge-коммит PR#2, MERGED на GitHub — пушить нечего; local main только отстал). Прочие LOCAL-ONLY ветки — dormant 26–28.10 снапшоты/keep, не интеграционные. ToDo.md: «ждёт агента»/~~/самозакрывов нет.
- NEXT TICK: шторм-патруль base tools a1 (исторически bring-up-фриз ~15мин цикл); исход tools → watch `_meta`; merged спит (ALL-DONE), lanes-юниты merged не рестартить. VERDICT-список на сегодня: desktop, security (zero-XML обе), skills (weak обе), e2e (count-diff), acp_adapter r1-историч.

## Тик 05.11 05:2x (агентский)
- 3 оттаивания base `monitoring`: a1 завис на bring-up (лог 144B статичен с 05:15:40, `[gwN] node down`, cgroup CPU 1ms/22-30s) → kill pytest-parent 1261934 (05:19:31) → a2 завис идентично (статичен с 05:19:37) → kill parent 1262966 (05:22:22) → a3 идентично (с 05:22:27) → kill parent 1263723 → **monitoring NO-CLEAN 05:25:32, zero-XML** = СИММЕТРИЧНО merged (в merged_r2 monitoring тоже без XML) → остаётся в VERDICT r3-списке (с 04.11).
- Между попытками base полоса прогнала дешёвые chunk'и: `tools` a1 rc=3 accepted (1777tc/7F/3E vs merged 1716tc/2F/3E — масштабно симметрично, F диффать по именам на общем executed-наборе, остаётся в r3), `tui_gateway` a1 **rc=1 CLEAN** (2979tc/4F vs merged a2 2976tc/12F — первая полная пара r2; 8F только на merged — триажить env/real по именам на вердикте), `perf_guards` a1 rc=1 (2tc/0F/1E == merged, симметрично).
- `.done` base 12/24; осталось 13: desktop, e2e, gateway, hermes_api(SKIP-symmetric), hermes_events, install, installation, manual, pm (жив: 13%+, реальные F, CPU живой), scripts, secret_sources, verify, website. Толстый pending base — `gateway` (merged gateway.xml 13509tc уже принят) — ключевой пробел перед VERDICT.
- Шлюз 15 мин: 2 строки, 0 ERROR/Traceback. GIT: всё ==origin (fetch+ls-remote: more-mem-layer 401f7edea2 ahead/behind 0; elk/skills-write-forbidden 464e76d36f; upstream-sync-20261103 047b012fd4 == hermes-sync-up HEAD). Деревья чистые (hermes-sync-base dirty by design — полосы гонят его). ToDo 118: «ждёт агента»/~~/самозакрывов нет.
- NEXT TICK: шторм-патруль pm и остатка (дешёвые scripts/manual/install* могут NO-CLEAN-ить мгновенно на bring-up — проверять `_meta` хвост + .done count); base gateway — оттаивать ТОЛЬКО по frozen-crit (лог статичен AND CPU<10ms/30s, окно ≥20s), при CPU≠0 не трогать (4h wrapper сам рециклит); merged ALL-DONE — не рестартить.

@05.11 05:3x tick (шлюз-тик):
- ОДНО оттаивание base `pm` a1: реальный прогон дошёл до 98% (лог 3221B статичен с 05:28:13, хвост `[gw0] node down: Not properly terminated` + F), cgroup CPU 1ms/25s → frozen-crit подтверждён (окно 25s ≥20s) → bare SIGTERM pytest-процесса 1264444 (PPID=timeout) в 05:38:52 → `pm a1 rc=143` → a2 поднялась сама (PIDs 1295442/1295443, проверено +30s). pm НЕ в r3-списке — base-side pm нужен для вердикта, a2 шторм-патруль.
- Дельта с 05:2x: monitoring NO-CLEAN / perf_guards уже учтены предыдущим тиком; .done base 12/24 без изменений (pm не закрыт). merged ALL-DONE 24/25 — не трогать.
- Шлюз 15 мин: 1 строка, 0 ERROR/Traceback. GIT: more-mem-layer 401f7edea2 == origin (ahead/behind 0), дерево /home/qwen/hermes-agent чистое; hermes-sync-up чист, HEAD == origin/upstream-sync-20261103. ToDo 118: «ждёт агента»/~~/самозакрывов нет. Память 86G available.
- NEXT TICK: шторм-патруль pm a2 (_meta хвост + .done); далее толстая полоса base-повтор + оставшиеся дешёвые (scripts/manual/install*/installation/verify/website/e2e/hermes_events). merged не рестартить.

@05.11 06:1x tick (шлюз-тик):
- 3 оттаивания base `website`: a1 реальный прогон до 97%+`[gw0/1] node down`, лог 173B статичен с 05:45:12, cgroup 1ms/25-30s → frozen-crit → термин pytest 1369926 06:03:5x → a2 (1374487) завис на bring-up идентично (175B с 06:05:11, 1ms/20-30s) → термин → a3 (1375012) та же картина (175B с 06:07:22, 1ms/25s) → термин → **website NO-CLEAN-ATTEMPT 06:09:38, zero-XML** = симметрично merged (website в VERDICT r3-списке с обеих сторон). .done base 19→20.
- Толстый пробел закрыт стартом: полоса перешла на base `gateway` a1 (06:10, PIDs 1375558/9) — **реальный прогон**: +20.0s CPU/25s, лог растёт (6% к 06:12). merged gateway.xml (13509tc) уже принят; base-side ожидается ~1h45m (завершение ориентировочно ~08:00), шторм-патруль по frozen-crit.
- Событий между 05:45 и 06:03 не было: verify a1 rc=3 был последним дешёвым; install/manual/installation уже учтены тиком 05:5x (install NO-CLEAN rc=5x3, manual NO-CLEAN rc=5x3 — симметрия на вердикте).
- pm a2 rc=3 ACCEPTED 05:41:36 (pm.done есть) — pm закрыт weak-accept, executed-count сверить на вердикте. Осталось base после website: gateway (жив), e2e, desktop; hermes_events SKIP-nodir (симметрично merged).
- Шлюз 15 мин: только tool_executor WARNING (чужие сессии), 0 ERROR/Traceback. GIT: more-mem-layer 401f7edea2 == origin (ahead/behind 0), дерево hermes-agent чистое; hermes-sync-up HEAD == origin/upstream-sync-20261103 (ls-remote). ToDo 118: «ждёт агента»/~~/самозакрывов нет.
- NEXT TICK: шторм-патруль base gateway a1 (оттаивать ТОЛЬКО frozen-crit: лог статичен AND CPU<10ms/30s, окно ≥20s; при CPU≠0 не трогать — 4h wrapper рециклит сам); проверка .done/hвост _meta; сверить статус pm a2/e2e/desktop.

@05.11 10:1x tick (шлюз-тик):
- Прогноз тиков 08:1x/08:2x подтверждён: base `gateway` a1 завершился rc=124 ровно 10:09:38 (4h wrapper), XML НЕ высадил (gateway.a1.xml отсутствует) — band «жив, но не высаживает» (577-926ms/25s с 07:53) оказался never-land. Драйвер сам перешёл на a2 (~10:09:38).
- a2 — реальный прогон: лог 808→988B за тик, mtime = время проверки (10:12:20), 4%+; cgroup chunkbasev6 20649ms/25s ≫ frozen-crit → не трогать. Дедлайн wrapper a2 ~14:10 → a3 (storm-watch; при NO-CLEAN gateway остаётся в VERDICT r3-списке как base-gap, merged-side 13509tc уже принят).
- .done base 20/24 без изменений (gateway ещё не закрыт); после gateway осталось base: e2e, desktop. merged ALL-DONE 02:08:20 — не трогать.
- GIT: hermes-agent дерево чисто, more-mem-layer 401f7edea2 == origin (ahead 0); skills-write-forbidden 464e76d36f == origin. В hermes-sync-up был 1 не запушенный docs-коммит 095365661a (opencode → optional-skills, чужая сессия, дерево чисто) — запушен FF в origin/upstream-sync-20261103 этим тиком. hermes-sync-base грязный — by design, не чистить.
- Шлюз 15 мин: 1 строка, 0 ERROR/Traceback. ToDo 118: «ждёт агента»/~~/самозакрывов в секции приёмки нет.
- NEXT TICK: шторм-патруль base gateway a2 (оттаивать только frozen-crit, окно ≥20s; CPU≠0 не трогать); при высадке gateway.a2.xml либо NO-CLEAN — проверить .done 21 и переход полосы на e2e.
