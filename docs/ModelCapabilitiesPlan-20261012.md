# Каталог моделей, режимы мышления и проверка запуска

Статус: ROOT APPROVED. Источник: прямое поручение владельца от 2026-10-12 после запуска 15 команд OpenCode.

Решено добавить команды `models` и `probe`. Каталог показывает сведения, реально объявленные CLI: полный ID, провайдер, возможности, лимиты, стоимость и варианты мышления. Наличие в каталоге не считается доступностью или наличием квоты. Неизвестные режимы остаются неизвестными; общий список provider.json не выдаётся за подтверждение конкретной модели.

Проверка запуска использует выбранные engine/model/effort без подмены, ограниченный таймаут и существующий отслеживаемый harness. По умолчанию это короткий chat-only запрос `Reply OK`, без доступа к инструментам и файлам проекта. Результат различает успешный ответ, rate-limit/auth/model error, таймаут и отсутствие ответа. Режим, переданный CLI, и режим, сообщённый самим провайдером, описываются отдельно.

GUI сохраняет прежний `/api/models` контракт и получает дополнительные сведения по модели для выпадающего списка режимов. Реальная проверка запуска только по явному нажатию/CLI команде, без платных фоновых вызовов. Каталог и сообщения не раскрывают конфиги авторизации, ключи или reasoning-текст.

Scope: новый `neoxider_agents/model_catalog.py`, CLI routing, ограниченное GUI API/выбор режимов, meaningful offline tests, README/SKILL/AGENTS/GEMINI usage. Provider runner, lifecycle, active state и уже запущенные 15 jobs не менять и не перезапускать.

Волны: сначала текущий CLI format и нормализация; затем команды/GUI; затем offline regression + реальные OpenCode catalog/probe проверки; полный diff и push. Использовать stdlib и существующие native executable/process guards, без новых зависимостей.

Проверка: ANSI/multiple verbose JSON records, malformed/oversized/timeout catalogue, неизвестная модель/режим, отсутствие секретов, точное forwarding выбранного variant, no silent fallback, JSON/text CLI, GUI backward compatibility; реальный catalogue двух free моделей и bounded probe. Существующие CLI/provider/GUI tests остаются гейтами.

Риски: каталог не доказывает доступность/квоту; free endpoints могут не публиковать variants; успешный chat-only probe не подтверждает tool execution. Эти границы показываются в результате явно.

Дополнение ROOT APPROVED после live-проверки: OpenCode free tier отклонил обе пробы custom chat-only агента с 403 FreeTierError, тогда как обычная задача Muse завершилась успешно. Это отдельный `route-rejected`, не вывод об исчерпании квоты/доступности модели. Добавить явно выбираемый `probe --standard` для проверки обычного OpenCode в пустой временной папке: инструменты доступны, проверяется только короткий ответ. Автоматически менять профиль при отказе запрещено. Вывод ошибки ограничивается message, без responseHeaders/responseBody; JSON stdout остаётся чистым. Runtime/lifecycle/provider runner не меняются.

Интерфейс также обновляет три штатных completion scripts (PowerShell/Bash/Zsh) с командами models/probe и новыми флагами. В SKILL уточняется уже действующий plugin-root POSIX path; исполняемые provider/runtime файлы остаются без изменений.
