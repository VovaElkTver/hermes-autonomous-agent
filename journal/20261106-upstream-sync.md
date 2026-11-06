# upstream-sync-20261106 — дельта 1326 (main..upstream/main), гейты в работе

Дата: 06.11 · Ветка: `elk/upstream-sync-20261106` · Мандат: «делай» (Вл, 06.11)

## База

Вчерашняя цепочка: main = 6307081868 (донор 6ab0de91d8) + f4520799d2 (more-mem-layer).
Дельта против **main**: 1326 коммита (прошлый отчёт upstream-watch считал против
more-mem-layer — 3189, двойной счёт донора; формулировку watcher'а меняю на дельту от main).

## Ход слияния

1. `elk/upstream-sync-20261106` ← main; merge upstream/main @ ca4360692055.
   Конфликты: только 2 докованных справочника slash-commands (en + zh-Hans) —
  их сторона возродила write-approval месседжинг-подкоманды /skills, наша держала
   `/save` в CLI-only списке. Разрешено юнионом: структура+формулировки upstream,
   `/save` сохранён.
2. Порядок-итог: первая попытка была основана только на main (topic-gate туда не
   входил) — исправлено дослиянием more-mem-layer@eb04f3a4da; итоговый HEAD
   376a17d9f2 = main + upstream/main + more-mem-layer (v0.1+v0.2 садятся поверх
   свежего апстрима без отдельного бэкпорта).

## Что апстрим изменил в run_inbound.py (зона автоконфликта)

- блок admission вынесен в метод `_hm_admit_to_fifo()` + телеметрия busy-прихода
  (`stamp/settle_busy_arrival`);
- наш фикс 29.10 (`rescued_callback` вокруг `admit_message`) auto-merge перенёс
  внутрь нового метода корректно — проверено чтением hunk'а;
- остальное по файлу: бот↔бот идентичность/хопы, SessionActor переименование,
  relay attachment filenames.

## Гейты

- Gate-A (адресные): topic-gate 19, memory_tool, authority_busy plaintext/preempt —
  89 passed.
- tests/controller: 8 passed.
- Gate-B (tests/hermes_state): гоняется фоном, лог
  `~/.hermes/cache/scratch/hermes_state_gate2.log` — итог дописывается ниже.

## Статус

Запушено в origin, ls-remote сверен. Шлюз продолжает жить на
more-mem-layer@0c285095d1 (v0.2, перезапуск 08:58 прошёл — MainPID сменился,
инцидент PM-install-key закрыт самим фактом цикла). Перевод чекаута прода и
рестарт на sync-ветку = шаг приёмки Вл после Gate-B.
