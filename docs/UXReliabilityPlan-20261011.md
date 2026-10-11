# План надёжности CLI, 2026-10-11

Статус: **R1 утверждён root и передан source-final; R2/R3 остаются предложениями**. Исполнитель: native GPT-6.1 Sol high `fleet_tools_lead`. Root владеет review/Git/push. Репозиторий: `C:/Users/User/.claude/skills/cli-agents`, исходный HEAD `bcf8cd8`. Это отдельный tool checkpoint, не Unity-изменение.

## Решение и исходные наблюдения

Нужны прозрачные быстрые запуск/остановка, primary-provider failures, реальные parent/child controls и правдивый terminal результат. Повторять существующую систему процессов/Job Object нельзя: она уже есть. Использовать существующие StateStore/Lock, reporting snapshots, TaskTurn и provider adapters.

Фактические текущие файлы — `neoxider_agents/*.py`, а не `core/*.py`. `docs/PERFORMANCE.md` уже документирует bounded baseline и partial tracking. `reporting.begin_snapshot()` до publish metadata и stop/result whole-workspace scan могут тратить 30 s на огромном Unity tree; `--owns` сейчас фильтрует выходной delta после сканирования. Предыдущий tools worker сообщил children/files без настоящих metadata/files — это failure приёмки worker, а не уже доказанная runtime ошибка core.

`agent.sh pending` в текущем глобальном state выдал большой исторический перечень и огромные changed_names; есть воспроизводимый дефект удобства вывода. Старый `mvp1011-aaa-team` правильно определяется stopped с launcher-stopped reason. Не менять его историю и не запускать заново автоматически.

## Волны, scope и acceptance

### R1: ускорить точный ownership snapshot

Предложенный writable scope: `neoxider_agents/reporting.py`, `neoxider_agents/lifecycle.py`, `tests/test_bounded_baseline.py`, `tests/test_ux_tracking.py`, `docs/PERFORMANCE.md`.

До реализации проверить настоящий call graph. При explicit `--owns` scoped snapshot перечисляет и хеширует только этот scope с ограниченным candidate enumeration; без owns сохраняет общий baseline и прежние completeness semantics. Baseline фиксирует scope, чтобы последующие diff/stop/result использовали ту же область, даже когда caller flags уже отсутствуют. Existing v2 baselines читаются. Вне scope нельзя объявлять added/deleted и нельзя скрывать partial. Не применять mtime reuse к обычным small-file delta: restored-timestamp edits должны обнаруживаться.

Acceptance: large synthetic unowned tree плюс narrow owned files — unowned байты не читаются; owned modification/deletion/addition, restored mtime, glob parent paths, outside-directory paths/linked ancestors, partial scan проверяются. Старый baseline без scope работает как раньше. Команды: `python -m unittest discover -s tests -p test_bounded_baseline.py`; `python -m unittest discover -s tests -p test_ux_tracking.py`. Ожидается pass без повышения бюджетов. Реальный Unity narrow read-only task отдельно измеряет время до visible metadata/start и stop; это не performance-claim до receipt.

### R2: primary error isolation и bounded status

Предложенный writable scope отдельного батча: `neoxider_agents/providers.py`, `neoxider_agents/runtime.py`, `neoxider_agents/views.py`, `tests/test_providers_core.py`, `tests/test_limit_reporting.py`, `tests/test_activity.py`, `README.md`.

Сначала воспроизвести primary 429/EndpointUnavailable без progress. `small=true agent=title` error не должен останавливать здоровый primary run. Primary failure должен приводить к явному limited/failed с factual reason/reset, а не бесконечному running. Не предполагать наличия metadata в каждой provider log строке. Сохраняются bounded retries и existing LIMIT_HIT markers. Уменьшить только вывод historical stop details в pending/list; полный result/diff остаётся доступен. Ошибки provider/session/report не превращаются в fabricated success.

Acceptance: title-only429 + successful primary; primary429 без text output; primary endpoint unavailable; mixed transient/retry; malformed metadata; stdout close. Exact tests определяются после reproduction. `python -m unittest discover -s tests -p test_providers_core.py`, `... test_limit_reporting.py`, `... test_activity.py`. Existing Windows process/lifetime guarantees не ослаблять.

### R3: реальные nested ownership/control и delivery evidence

Предложенный writable scope: только после отдельной карты call graph `neoxider_agents/lifecycle.py`, `fan.py`, `ux_tracking.py`, `tests/test_agent_control.py`, `tests/test_process_core.py`, `ORCHESTRATOR.md`. Эта волна не стартует параллельно R1, поскольку lifecycle общий.

Использовать реальный `-P` parent, strict owns и tracked launcher/wait. Не создавать manager-only устный «дерево исполнителей». Сначала проверить, что send/cancel/restart/status/GUI уже делают по существующим contracts. Parent terminal/failure propagation и launcher-reset recovery требуют regression; orphan delta вычисляется только explicit stop/result/diff, не под global ownership lock. Native launcher lifetime cancellation сохраняется, отключать Job Object для сохранения фоновых workers запрещено.

Acceptance: fake-provider manager создаёт настоящие children; verified metadata parent+PIDs/generations; disjoint owns разрешены, overlap отвергается; queued send доставляется в порядке; cancel parent/child и launcher hard death имеют честные terminal outcomes и no surviving owned processes. Existing tests и Windows lifecycle sampler; отдельно real free-provider child tasks после coding/tool probe. CLI/GUI статус и command result review, затем root whole-diff и отдельный tool push.

## Риски и границы

- Scoped tracking доказывает assigned files, не whole-workspace compliance; root git diff по всему repo обязателен.
- Exact-file и recursive glob ownership нельзя тихо смешивать. Forward/backslash normalization, Unicode/Windows drive paths, case, symlinks и missing/new paths нужны в проверках.
- Изменение snapshots нельзя публиковать с параллельно работающими old launchers. Нынешние stopped task receipts остаются читаемыми.
- Нулевая цена и успешный OK probe не доказывают tool correctness. OpenRouter-key не попадает в stdout/docs.
- Полный fleet/tools goal не завершён этим планом. R1 получил root exact approval; R2/R3 ещё ждут отдельного решения. Paid models, config/key changes и новые сервисы не входят в scope.

## Критерий передачи

CHANGED: настоящий file list. VERIFY OUTPUT: commands/return codes/observations. NOT DONE: live checks и другие волны. CONCERNS: сохранённые ограничения. QUESTIONS: только consequential contract ambiguities. Root получает actual diff и результаты; commit/push исполнитель не делает.

## R1 — фактическая передача source-final

Изменены только пять утверждённых файлов: `neoxider_agents/reporting.py`, `lifecycle.py`, `tests/test_bounded_baseline.py`, `test_ux_tracking.py`, `docs/PERFORMANCE.md`. Native исполнитель сам Git не выполняет. Root review/push ещё не принят этим документом.

- `python -m unittest discover -s tests -p test_bounded_baseline.py`: 28 tests, 12.083s, OK, skip1 (недоступно реальное создание Windows symlink); независимая проверка linked ancestor без OS privileges проходит.
- `python -m unittest discover -s tests -p test_ux_tracking.py`: 52 tests, 1.130s, OK.
- `python tests/test_ux_tracking.py --prove-defects`: exit0, 43/43 planted defects пойманы. Обновлены устаревшие anchors ранее существовавших hash/stat/git-status mutation proofs; ownership final-filter proof теперь проверяет старый unscoped baseline, новый scoped baseline проверяется отдельно. Assertions не ослаблялись. Manifest: `D:/Temp/agents-ux/tracking/proofs/manifest.json`.
- `git diff --check`: exit0. Actual Unity workspace read-only scope `Docs/Tasks/FleetBriefs20261011/*.md`: 3files, baseline0.339656s, delta0.221038s, freeze0.240613s, partial=false, zero changes. Receipt: `D:/Temp/agents-ux/r1-20261011/unity-owned-docs-readonly.json`. Это конкретный narrow-scope measurement, не GUI parity или большой общий benchmark.

Остаются R2/R3, actual free-provider write/tool probe, настоящее вложенное делегирование и GUI/control/lifetime acceptance. Успех R1 их не заменяет.
