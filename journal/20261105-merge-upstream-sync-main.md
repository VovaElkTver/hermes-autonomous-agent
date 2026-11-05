# Мерж-цепочка upstream-sync-20261103 (05.11, мандат Вл «мерж, с этим надо завязывать»)

## Исполнено
1. Пуш пачки: `more-mem-layer` @ `401f7edea2`, `upstream-sync-20261103` @ `498754291c` — оба в origin (проверено ls-remote).
2. Мерж №1: `main` (257435b3b2) ← `upstream-sync-20261103` --no-ff → `6307081868`. Донор выровнен на 6ab0de91d8 (+1863 коммита).
3. Проверка состава нашла: sync-ветка НЕ содержит causal/followups-v2/thread-doctrine (ответвилась от a81996241e до их коммитов) — это не потеря, а ожидаемое следствие топологии.
4. Мерж №2: `main` ← `more-mem-layer` --no-ff → `f4520799d2`: followups v2, causal stage a–d, matrix thread-visualizer default-off, terminal timeout fix.
5. Санити на собранной main: imports ок; test_controller_causal 31 passed; test_controller_followups 8 passed; 39 passed суммарно.
   - Collection errors в tests/plugins — только telegram-модули, ошибка среды venv, воспроизводится и до мержа.
6. Пуш `main` → origin: 257435b3b2..f4520799d2 (верифицировано ls-remote f4520799).
7. Чекаут основного репо возвращён на more-mem-layer — живой шлюз и тень-causal должны оставаться на проверенном коде; перевод прода на main = отдельный шаг приёмки.

## По пути починено
- skills-catalog: строка opencode указывала на bundled/ путь, а upstream регенерацией унёс skill в optional-skills/ → контракт-тест красный. Строку удалил, тесты docs-contract 7/7, коммит `095365661a` (вошёл в sync-ветку до мержа).

## Остаточный риск (классификация, не доказательство)
- Гейт-3 закрыт по пересечению 12 кусков (~1385 совпадающих зелёных); merged-only регрессий нет.
  Base-лана потеряла 4 тяжёлых куска (gateway/e2e/desktop/website) из-за смертей воркеров («node down», симметрично) — эти зоны сравнены классификацией «environment-red», прямого соответствия нет.
- fork_profile.py и skills-write-forbidden НЕ входят в main — они лежат ветками elk/autonomous-mode и elk/skills-write-forbidden (закладки, ждут ТЗ/команды).

## Статус лин
- chunkbasev6 active (добивает монстров), chunkmergedv6c finished (ALL-DONE 02:08).
