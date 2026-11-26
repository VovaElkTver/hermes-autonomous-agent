# Решения §5 thinking-enable (Вл, 26.11 19:45 MSK): A/A/A

- Пункт 1 (реестр cognitive): A — frozenset в коде (статус-кво, паддаун-тест замораживает).
- Пункт 2 (verify_on_stop/background_review): A — auto (CLI+desktop), worker флаг unverified, matrix off (фактически уже в живом конфиге — подтверждение статус-кво).
- Пункт 3 (машинный предикат): A — обязателен для всех новых контрактов.
  Исполнено этим же вечером: guard draft->ready активирован, генераторы带着 предикатами
  (ctl_enqueue required arg; /for — полнота шагов; followups — факт доставки),
  HERMES_TASK_ID в окружении предиката. Git-ref elk/thinking-enable-s2 (fe266f2d974).
