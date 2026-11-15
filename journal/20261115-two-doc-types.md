# 2026.11.15 — два типа доков: для агентов (в инсталл/апдейт) и для людей

ТЗ Вл: «надо два типа доков. для агентов (включить их в инсталл и апдэйт) и
для людей».

## Исполнено (ветка elk/agent-human-docs, коммит 66561562d2, запушена в origin)

- **Для агентов — `docs/agents/`** (EN, самодостаточные):
  - `README.md` — индекс, порядок чтения, модель доставки;
  - `operating.md` — дисциплина: лента как единственный свидетель,
    controller-команды (таблица 6 команд + /for), событийная модель времени
    (events, не таймеры; cron запрещён как планировщик агентных шагов),
    fleet-правила (билет на выход, DEPLOY-NOTE, switch_release,
    survivor-верификация rescue run-once), report model, remote writes
    только в VovaElkTver;
  - `node-ops.md` — install/update/restart/rollback/wedged-boot протокол
    (env-переопределения инсталлтеров, forensics `hermes update`,
    drain-aware циклы, switch_release, reanimation).
- **Для людей — `docs/humans.md`** (RU): установка (one-liner, env,
  post-install чек-лист), `hermes update` (что делает, что сохраняется,
  forensics, откат), рестарт гейтвея, controller-команды (таблица),
  где живое состояние (RELEASES.md), роутинг на preface/cautionary/
  reanimation.
- **Доставка «в инсталл и апдэйт»**: доки живут в чек-ауте
  `~/.hermes/hermes-agent` → инсталл (клон), апдэйт (ff-pull), zip-fallback
  (GitHub archive вешает docs/, сохраняется только venv/.git/.env/node_modules).
  Обнаруживаемость несут указатели в скиллах `autonomous-ai-agents`
  (install-fork-agent, controller-protocol) — скиллы синхронизируются в
  `~/.hermes/skills/` при инстале и апдейте. Проверено: sync_skills на
  чистом HERMES_HOME скопировал 49 скиллов, указатели на месте.
- Заодно: снят устаревший post-rename pitfall в install-fork-agent
  (оба инсталлера нормализуют пре-именованный origin URL — проверено по
  fork-install.sh/ps1).

## Параллельный сеанс — тот же ТЗ

Пока исполнялась задача, параллельный сеанс (общий чек-аут) создал ветку
`elk/lifecycle-docs` @ 5e98e10a90 (15.11 12:49): «docs: lifecycle contracts
for agents and humans (claims П1/П3б/П5)» — `docs/controller-livability.md`
(RU, для людей) + `skills/autonomous-ai-agents/fork-ops/SKILL.md` (агентный
раунбук контроллера-живучести). Это «доки потом» из мандата controller-claims
(П3б: доки потом). Скоуп их доков — контроллер-живучесть (инцидент/механизмы),
скоуп моих — общий ops + install/update. Пересечение умеренное.

Мой первый коммит случайно лег поверх их ветки (чек-аут переключили на
elk/lifecycle-docs между моими проверками); отстранено: ветка
elk/lifecycle-docs возвращена на 5e98e10a90, моя работа — на
elk/agent-human-docs от main (без их файлов), общий чек-аут возвращён на
main @ 50e4e8f1da.

## Состояние на конец записи

- `origin/elk/agent-human-docs` = 66561562d2 (запушена, НЕ слита в main).
- `elk/lifecycle-docs` не тронута после 5e98e10a90, в origin не пушена
  (свой пуш — за их сеансом).
- Мерж в main — решение Вл: (а) мержить только мою ветку, (б) мержить обе
  (скоупы не конфликтуют: разные файлы), (в) дождаться завершения
  параллельного сеанса и слить вместе.
- Замечание к main (вне этой задачи): `scripts/check` на main @ 50e4e8f1da
  красноват по agents-md (root AGENTS.md 14016 > 12000 cap; 6 цепей > 30k)
  и ruff (52 findings) — предсуществующее, не от этой ветки.
