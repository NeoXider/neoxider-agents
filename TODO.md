# TODO

Только открытые задачи. История выполненных изменений находится в
[`CHANGELOG.md`](CHANGELOG.md); архитектурные идеи и исследования — в
[`docs/IDEAS.md`](docs/IDEAS.md).

## Интерфейс

- Отображать unified diff в чате как diff-представление с файлами и строками `+`/`-`,
  а не как обычный блок кода.
- После пользовательского тестирования пересмотреть навигацию по активным задачам:
  компактная сводка запущенных задач или отдельная панель фоновых задач.

## Совместимость и дистрибуция

- Прогнать полный macOS gate на реальной машине: Bash 4+, zsh installer, GUI, все
  provider adapters и запуск видимого Terminal для `terminal=true`.
- Проверить чистую установку и обновление Claude Code plugin в изолированном профиле,
  где отсутствует `~/.claude/skills/neoxider-agents`.
- Добавить CI-матрицу для Git Bash/Windows PowerShell 5.1/PowerShell 7, Linux Bash и
  macOS с Homebrew Bash; strict plugin validation уже должна быть частью release gate.

## Интеграции

- Прогнать реальный Unity PlayMode benchmark через OpenAI-compatible bridge; wire-format
  и standalone live smoke не заменяют проверку клиентского раннера Unity.
- Повторно проверить нестабильные OpenCode routes после обновления CLI/provider catalog;
  отделять сбой внешнего сервиса от регрессии wrapper-а воспроизводимым raw-CLI canary.
