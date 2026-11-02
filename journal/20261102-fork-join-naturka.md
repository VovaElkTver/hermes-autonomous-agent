# item2 (stage003): форк/слияние натуркой (2026-11-02)

**НАМЕРЕНИЕ (дословно, из тред-циаты Вл).** «new прошел» — по согласованному
пункту 2, воспроизведённому агентом в цитируемом сообщении: «пункт 2 плана:
форк/слияние натуркой (fork-стамп, branch_id, snapshot+overlay уже в коде по
stage003; проверяем живьём)».

**ГРАНИЦА НАМЕРЕНИЯ.** Проверка живьём на рабочем state.db через реальные пути
кода (инструмент `session_fork`/`session_join`, `SessionDB`, read-path
`get_messages_as_conversation`), без смены семантики slash-команд. Правка
допускается только как устранение найденного живьём дефекта, минимально, с
регрессией. Чужие active-ветки (наследие 30.10) не трогать.

## РАУНД 1 — РЕЗУЛЬТАТ

**Что прошло натурку (зелёное).**
- Состояние live state.db: 14 веток в `session_branches`, 12 из них закрыты
  слиянием `note` — note-merge работает в проде с 31.10, включая 02.11.
- Полный цикл на текущей сессии-родителе (`20261102_235306_b4e717ce`):
  1. FORK (`tools.session_fork_tool.session_fork`): ветка `b8734d6ce`, ребёнок
     `…-fbaf26df93`, base=последняя live-строка, name→`meta_json.title` — ок.
  2. OVERLAY (`get_messages_as_conversation(ребёнок)`): контекст ребёнка =
     префикс родителя ровно до base (32 строки, построчное совпасть сверено)
     + 2 свои строки; родитель продолжает дописываться параллельно — ок.
  3. JOIN strategy=`note`: см. пункт про 12 веток выше.
  4. JOIN strategy=`append`: **провал** — см. корень.

**Корень (append-merge мёртв в натурке).** Гвард чистоты хвоста родителя в
`hermes_state_branches._move_branch_rows_to_parent_sync` проверял
`_compressed_summary IS NOT NULL`. Фактический домен колонки —
`INTEGER NOT NULL DEFAULT 0`, писатель (`append_messages_batch`) пишет строгое
`1/0`, NULL не встречается никогда (проверено на live: 33871×0, 93×1).
Следствие: ЛЮБАЯ строка родителя после base помечается «загрязнённой» —
append-merge невозможно на живом родителе, т.е. всегда, кроме тестов, где
родитель после fork не дописывал ни одной строки. 17 тестов
`test_session_fork_branches` и сетка stage001 46/46 это не ловили ровно из-за
этого молчания родителя: тест с компакцией вставлял summary-строку
(она flagged верно), а «обычная жизнь хвоста» не моделировалась никогда.

**Фикс (1 предикат + 1 регресс-тест).**
- `hermes_state_branches.py`: `COALESCE(_compressed_summary, 0) NOT IN (0, '')`
  вместо `IS NOT NULL` (значение, а не наличие; наследует корректность и для
  legacy NULL, и для summary-text в колонке).
- `tests/test_session_fork_branches.py::TestAppendMove::test_append_moves_rows_after_parent_lives_on`:
  родитель дописывает обычные строки между fork и join → append разрешён,
  строки перенесены в хвост, порядок сохранён.

**Приёмка (натурка + тесты).**
- Повторный append-join `b8734d6ce` на ИСПРАВЛЕННОМ коде против live state.db:
  `ok`, status=`joined`, strategy=`append`, 2 строки перенесены в родителя ровно
  один раз (дублей 0), ребёнок отстыкован (0 строк), watermark родителя
  освобождён (`min_active_base_rowid=None`).
- Тесты: `test_session_fork_branches` 18/18 (было 17 + новый регресс);
  `test_fork_join_handlers + fork_explore + fork_explore_verbs + forkb_sticky`
  80/80; `branch_prefix + branch_name_ui + resume_console_lineage_guard +
  investigation_hooks` 72/72. Итого 170 зелёных.

**Границы / остаток.**
- Worktree-код исправлен; ПРОЦЕСС шлюза держит старый предикат в памяти до
  рестарта (chat-путь `/join --append` живёт в шлюзе). Рестарт — отдельное
  событие, не в этом тёрне.
- 2 унаследованных active-ветки 30.10 (`b7d7f3a42` «test», `b8e4c564f` «test2»,
  дети 122/423 строки) пинят watermark своих родителей; discard — за Вл.
- `note`-merge живьём не прогонялся заново (LLM-компрессор, 12 подтверждений в
  проде с 31.10) — не дублирую без нужды.
- CI на чистом HEAD не гонял; локальный прогон.
