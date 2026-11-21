
## Активация (S2.3 + noop)

Коммит в elk/thinking-enable-s0 ("S2.3 + noop"). Тесты 61 зелёные, ruff чистый. Активация — рестарт gw-cycle-2111e: роутинг retry, noop в ctl_step_done, колонка ctl_step.brief (reconciler мигрирует живую state.db при подъёме). Попутно: positional INSERT в add_step упал на новой колонке — переведён на именованные (тест поймал до прода).
