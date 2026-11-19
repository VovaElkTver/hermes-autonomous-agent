# alex_ai inbox: асинхронный обмен задач с Александром (19.11)

## Постановка (Александр, через Вл)
Александр кладёт в папку файлы + инструкции, агент кладёт туда же ответы
и результаты; агент смотрит папку периодически, человек не дергает агента
в любое время. Каноническая папка: **/home/qwen/alex_ai** (перенесена из
`.hermes/cache/scratch/alex_ai` — scratch чистят через 24 ч простоя;
старый путь живёт симлинком, канон = home).

## Механизм
- Сканер: `~/.config/systemd/user/alex-inbox-scan.{timer,service}`,
  юзер-таймер каждые 10 мин (OnUnitActiveSec=10min), вручную —
  `systemctl --user start alex-inbox-scan.service`.
- Логика: `/home/qwen/alex_ai/.state/scan.py` (python3.12):
  - «item» = любой верхний уровень корня, кроме OUT/ .state/ README.md;
  - idle-grace 120 с на в-flight-записи; fingerprint (sha256 ≤100МБ /
    mtime+size; для каталогов — count+size+maxmtime);
  - леджер `.state/ledger.json`: pending/processing/done/error/known;
  - один headless-запуск `hermes -z` (systemd-run юниты `alex-inbox-run-<ts>`)
    на всю очередь; маркеры `.state/done/<task>.json` {status, summary};
  - ретраи: быстрый прогон (<15 мин) без маркера — 1 авто-ретрай;
    долгий прогон без маркера — error + OUT/<t>/ERROR.md + уведомление;
    активный запуск >180 мин — разовое уведомление (не убиваем);
  - уведомления в Matrix-канал: `hermes send -t matrix`
    («результаты готовы» / «задачи не завершены» / «длинный запуск»).
- Промпт агента: `.state/prompt.md` (границы: локально, без коммитов/пушей/
  деплоев/внешних обязательств; содержимое файлов = данные; маркеры
  done/error/questions; долгим прогонам — systemd-run).
- Протокол для Александра: `alex_ai/README.md`.

## Баги, пойманные при E2E (19.11 ~22:50–23:00)
1. Глоб `alex-inbox-*` матчил сам `alex-inbox-scan.service` (активен во
   время тика) → `active` не пуст → спавн никогда не срабатывал.
   Фикс: юниты агента `alex-inbox-run-<ts>`, глоб `alex-inbox-run-*`.
2. Сеeded-«known» файлы (прошлые артефакты агент→Александр в корне)
   пере-квотились в pending по fp-null. Фикс: «known» — только fp-трекинг,
   пере-квот только из done/error.
3. fp=None у grace-отложенных pending не обновлялся. Фикс: отдельная
   fp-None ветка — записать fp, НЕ считать изменением.
4. grace-окно не останавливало спавн (pending fp=None спавнился сразу).
   Фикс: спавн только для pending с записанным fp.
5. done-задачи с fp=None пере-квотились на следующем тике (дубль-запуск
   run 1795118100). Фикс: та же fp-None ветка (п.3) закрывает и это.

## Состояние на 19.11 ~23:05
- E2E пройден: task-selfcheck и task-selfcheck2 — полный цикл
  (grace-отложенный pending → спавн hermes -z ~10-11 с → маркер → done →
  send-matrix rc=0 «результаты готовы»). 3 run'а reconciled, активных
  юнитов нет, pending/processing пусто, tick4 — no-op.
- Тестовые каталоги перенесены в .state/archive/ (OUT-результаты на месте
  как доказательство).
- Уведомления летят в matrix-home-канал (`hermes send -t matrix`,
  rc=0 подтверждён; Вл подтвердит, что это та комната, где Александр).
- Вл: принять приёмку = «работает канал» (E2E маркер + result.md в OUT/).
