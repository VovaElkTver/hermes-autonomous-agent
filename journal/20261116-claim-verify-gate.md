# 16.11 — claim-verify stop gate (код по corpus-GEPA)

##起因 (данные, не нарратив)
GEPA-прогон на 8 сутках диалогов (40 correction packets): false_success 11, semantic 9.
Сигналы: агент пишет «создал/настроил/перезапустил/смержил» без следа исполнения;
ссылается на файлы, которых нет на диске. Класс «самый дешёвый для кода»
(детерминированная проверка против LLM-классификаторов).

## Что сделано
- `agent/claim_verify.py`: детектор phantom-путей (regex токенов → `Path.exists()`,
  whitelist путей, тронутых tool-вызовами за ход) + детектор effect-claims
  (смержено/настроено/перезапущено/создано + EN, семь families) против реальных
  tool-команд того же хода. Nudge-текст по-русски.
- `agent/turn_stop_gates.py`: gate #6 после investigation-H4; одно напоминание
  за ход (маркер `_claim_verify_synthetic` в history текущего хода);
  конфиг `agent.claim_verify` (env-оверрайдов новых нет — ratchet HX002 прошёл),
  default ON для этого форка; exception-boundary с `exc_info=True`.
- `tests/test_claim_verify.py`: 17 тестов (phantom, claims, границы хода, URL/no-such-file
  exemption, wiring через monkeypatched run_turn).

## Проверки (фактические прогоны)
- `pytest tests/test_claim_verify.py tests/test_investigation_hooks.py` — 39 passed.
- `scripts/check --only health` — 0 blocking (после правок: убрать HERMES_* env,
  exc_info на catch-all, ruff FURB167 x5).
- Ruff на своих файлах — чисто (в checkout есть предсущ. грязь: handlers.py,
  src/claims/*, plugins/skill-gate — 22 ошибки вне моих файлов, не трогал).
- Replay по 40 пакетам: phantom-арм сработал бы на 22, effect-arm на 25 (верхняя
  граница: replay не даёт полного evidence-контекста — зафиксировано как ограничение).

## Ограничения
- Effect-arm проверяет НАЛИЧИЕ подходящей команды, не её exit-успех —
  это дополняет (не дублирует) verification_stop (тот проверяетrc=0 в tool ring buffer).
- Пути в чужих machines/других хостах (`/opt/...` на rtx5000) могут дать ложный phantom:
  whitelist ходовых мутаций смягчает, но remote-пути — открытый риск. Порог осознанно
  смещён в сторону ложных срабатываний (дешёвый nudge против ложного успеха).
- Не активирован в продакшене: гейт живёт в `elk/claim-verify-v1`, main не тронут.

## Артефакты
- git-ref `elk/claim-verify-v1`, tip `7793cd15610`, на origin (3 файла, +388).
- GEPA: `~/projects/gepa/analysis-16-11/` (turns.json, packets.jsonl, reflected,
  optimized_prompt.txt, val_audits.json, run_gepa_dialog.py) + raw-дайджест
  GEPA-16-11-RAW.md (отправлен в комнату).
- Ждёт вердикта Вл: мерж в main (активация = рестарт гейтвея с этим checkout).
- Не сделано (по кластерам GEPA): scope-guard (#3), authorization-классификатор (#4)
  — дороже, требуют отдельного design-pass.
