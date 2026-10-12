# Проверка каталога моделей и запуска

Проверено 2026-10-12 на Windows, OpenCode 1.18.34, Python 3.14. Добавлены `models`, `probe`, сведения о моделях в GUI и completion для PowerShell/Bash/Zsh. Provider runners, runtime/lifecycle и активные задачи не изменялись; GUI-сервис не перезапускался.

Полный повторный offline-прогон: **929 tests, 0 failures, 0 errors, 3 skipped**, 497.629 s. Новые focused проверки: **13/13**, плюс Node `tests/test_models.js` PASS. Отдельные provider/core/GUI проверки ранее: 78/78, 11/11, 172/172. Первый общий прогон (926 tests) завершился 7 failures/1 error: среди наблюдённых отказов были timing/process thresholds и отсутствие буквального plugin-root пути в SKILL. Runtime не правился; действующий plugin-root путь уточнён в документации, полный повтор выполнен без ослабления тестов. Полные локальные отчёты сохранены вне репозитория: `D:/Temp/neoxider-models-20261012/all-tests-repeat.json` и `all-tests-repeat-errors.txt`.

| Модель | Объявленные режимы | Реальная стандартная проба |
|---|---|---|
| `opencode/muse-spark-1.3-contributor-free` | minimal, low, medium, high, xhigh | `Reply OK`, forwarded xhigh, exit 0, 13.402 s |
| `opencode/space-bunny-free` | low, medium, high, xhigh, max | `Reply OK`, forwarded max, exit 0, 13.953 s |

Обе модели объявляют reasoning/toolcall и нулевые input/output цены. Это сведения каталога, не остаток квоты и не оценка качества. Эффективный режим мышления провайдером не сообщён; результат оставляет его неизвестным.

Custom chat-only профиль получил `403 FreeTierError` для обеих моделей; стандартные независимые пробы прошли. Ошибка классифицируется как `route-rejected`, без автоматической смены профиля/модели. `--standard` выбирается явно, использует пустую временную папку и допускает инструменты; в этих пробах проверялся только ответ. Доступ к инструментам, квота и качество кода этими ответами не подтверждены.

GUI: Python проверяет сохранение списка ID и явный fallback на unknown metadata; Node проверяет режимы конкретной модели, пометки unverified и игнорирование устаревшего ответа при смене провайдера. Живой браузерный visual QA не выполнялся. Исходники и Git diff проверены; проверка whitespace PASS.
