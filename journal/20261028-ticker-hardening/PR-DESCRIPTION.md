# PR (draft) → VovaElkTver/hermes-autonomous-agent

## Заголовок
`claims-harness: внешняя оракульская проверка аналитики (популяция + предикаты + замороженный оракул с бюджетом)`

## Связанные директивы
- Архитектор, 20.10: «важнее реально пошаговый анализ, а не я_взял_10_из_100».
- «грех Hermes: сам себя оптимизирует и считает скрипт успешным по отсутствию кода ошибки, а не по качеству».
- Донор схемы: `github.com/gepa-ai/terrarium` (EvalServer = единая точка бюджета/оценки).

## Что делает
Выводит оценку аналитической работы агента **вне кандидата**:
1. **Популяция** перечисляется харнесом, не агентом; покрытие — по trace сессии.
2. **Предикаты**: утверждение агента прогоняется по ВСЕЙ популяции; возвращается
   распределение контрпримеров (каждый — своей строкой).
3. **Оракул вне кандидата + бюджет на сервере**: `eval_fn` заморожена/не видна
   исполнителю; единая точка `FrozenOracle.evaluate()` с счётчиком (choke point).

## Состав
- `docs/claims-harness-spec.md` — спека.
- `src/claims/` — ядро: `core.py` (Population/Trace/Claim/ClaimHarness/report),
  `oracle.py` (FrozenOracle + BudgetExhausted), `xlsx_io.py` (stdlib xlsx r/w).
- `src/claims/workclasses/` — два класса работ: `xlsx_sums.py`, `donor.py`.
- `examples/xlsx_sums/demo.py`, `examples/donor_coverage/demo.py` — демо с поимкой
  внедрённых контрпримеров.
- `tests/test_claims.py` — 13 тестов (stdlib unittest).

## Как проверить
```
python3 -m unittest discover -s tests -v
python3 examples/xlsx_sums/demo.py
python3 examples/donor_coverage/demo.py
```

## Ограничения соблюдены
- Не внедрены DSPy/terrarium-рантаймы (только донор-схема).
- Только stdlib (+ то, что в venv); xlsx — zipfile+xml.
- Каждый контрпример — отдельная строка отчёта (нет «в основном»).

## Статус
Draft. Этап 1. След. шаг: врезка предикатов в `ctl_review`/`ctl_artifact_revision`,
затем этап 2 — GEPA за замороженным харнесом.

## Push / PR
Бранч `claims-harness-stage1` перебазован на `main` fork (single commit — история
клона была несвязной). Draft PR #61: https://github.com/VovaElkTver/hermes-autonomous-agent/pull/61
(README клона перенесён в docs/claims-harness.md, корневой README fork не тронут).
