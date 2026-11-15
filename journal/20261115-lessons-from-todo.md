# Уроки, хранившиеся в ToDo.md до 15.11 (перенесены оттуда: ToDo — только намерения)

Урок item1 (инцидент 02.11, PATH-замечание Вл): `hermes chat --oneshot -Q` воркера
исполняет ход В ПРОЦЕССЕ ШЛЮЗА (gateway-hosted turn) — env-зависимости ядра возить
только именованными переменными через client_env/BEHAVIOUR_ENV, не через окружение
процесса. PATH-канал тулчейна (TOOL_ENV, client_env_passthrough) — best-effort для
интерактивных тулов: conda/uv/venv'ы тусуют PATH как хотят, на нём ничего не строить;
тесты не должны зависеть от окружения запуска (герметизировать monkeypatch'ем
`_HERMES_BIN_DIR`, см. tests/gateway/test_session_client_env.py).

Урок инцидента 03.11 (разбор по ленте 00:07–00:11; «гробил проект выводами»): все четыре
ошибки — вывод раньше опоры на источник. Код-механизмы (правила в промпте недостаточны,
прецедент session.py boot-identity):
1. **Медиа-происхождение кодом** (`1fcf646d12`): каждый media-оборот открывается строкой
   `[Media origin (resolved by gateway code...): platform=…, chat=…]` — платформа приходит
   фактом, vision отвечает за контент (лечит угадывание «Telegram vs Matrix» по пикселям).
2. **Пины свидетельств** (`e6e4a62223`): gateway/platforms/media_pin.py — цитированные медиа
   переживают age-sweep (rolling 24ч, кэп 7д), housekeeping чистит реестр.
3. **Рождение сессии O(1)** (`7191bbe8ec`): `**Session started:** …` в Current Session Context
   из created_at (cache-stable) — «уже был /new?» не реконструируется из нарратива.
4. **Гейт деклараций** (`933f251aa0`): финальный текст с success-словами без артефакта проверки
   (тесты N/N, хеш, путь, tool run) получает маркер `◌ Декларация без артефакта проверки`.
   Off-switch: HERMES_DECLARATION_GATE=0.
