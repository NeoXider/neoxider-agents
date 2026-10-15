# Release verification — 2026-10-15 (source gate принят, исторический выпуск опубликован)

Короткий итоговый отчёт проверки восстановления паритета. Статус:
**source gate ACCEPTED** — Root-авторитет
`C:/AI/outputs/parity-final-recovery-20261015/root_source_gate.json`
(`SOURCE_GATE_ACCEPTED`, 2026-10-15T07:35:52Z, SHA256 файла
`ba196679de21c3f67292758991ed04b7c4de1bf81e995fa7c9ffecb60b6c8c3b`):
35 модулей, 1313 уникальных тестов, 5 skips, 0 failures/errors — после ОДНОЙ
исходной teardown-ошибки хранилища. Root исправил ТОЛЬКО
`tests/test_ux_storage.py` (bounded exact-owner drain процессов, исключая
in-process test runner); затронутые 31 тест перезапущены за 46.142 с — PASS.
Исходный первый полный прогон и неудачная промежуточная fixture-попытка
сохранены; это НЕ заявляется как один чистый прогон 1313. Продакшн-код не
менялся; source-final манифест
(`c7488f46a24555d5942e7c6245063155e1c1c4c59e56349e6d502bd694035def`)
изменился только этим тест-файлом. SDK source
`a730dc79f75a814e17d3780cdedc95d33b6868bb937edd0032f721e6ee6df679`, test
source `6e836d18a2c072b9e97abab473ce25ffce65205c72b28c7cdec715223af3dce9`;
SDK 63 пройден внутри полного прогона. Пакеты scope-compliance от
doc/source-воркеров НЕ приняты (нарушение read-only-Git ограничения); Root
независимо принял фактические source/diffs/tests.

Историческая публикация подтверждена Root-квитанцией
`C:/AI/outputs/parity-final-recovery-20261015/RELEASE_RECEIPT.json`:
статус `PUBLISHED_VERIFIED_CLEANED`, 2026-10-15T08:11:15 UTC.
[Коммит `828c3b7136851bdc23ffaf8911ad992fdda896ea`](https://github.com/NeoXider/neoxider-agents/commit/828c3b7136851bdc23ffaf8911ad992fdda896ea)
опубликован и установлен; `remote_head` и `installed_head` совпали по квитанции.
Удалены только собственные recovery-worktree и task-branch; unrelated
`.opencode/` и `docs/ProviderProbe20261011.md` сохранены. Это результат того
выпуска по датированной квитанции, не новая проверка текущего remote или runtime.
Новый native worker-тест не проводился. При исторической документационной
подготовке source gate Git (включая status/diff), вызовы провайдеров,
делегирование, source-тесты, Unity и изменения конфигов не выполнялись.

База: worktree от remote `main 9b74239`, включает опубликованные parity-фичи
`2dd31e0`. [NATIVE_PARITY_REPORT](NATIVE_PARITY_REPORT.md) — исторический
снимок, для принятого source заменён этим отчётом. Старые неудачи —
исторические, ссылки сохранены, не стёрты.

## 1. Фактическое сравнение native vs CLI (одна задача, не бенчмарк)

Источник: `C:/AI/outputs/neoxider-native-comparison-20261014/REPORT.md`
(русский исходник) + `evidence.json`. Объём: ровно один native-агент и одна
задача OpenCode SpaceBunny на одну работу — статический аудит README
`UP_I1_Sea_Shadow_Angler`, 9 разрешённых файлов, формула наград 1/6/2, одна
общая medium-находка (приватный путь квитанции в публичной документации),
принудительное прерывание и продолжение в той же личности без изменения
отчётов (хеш native `19e610cc…` и хеш CLI `556874b4…` — РАЗНЫЕ хеши, каждый
совпал со своим историческим ожидаемым дайджестом, а не друг с другом).
При повторной сверке сошлись 8 из 9 исходных входов (один дрейф provenance-дока).

| Операция | Native (внутри чата) | Neoxider Agents / OpenCode |
|---|---|---|
| Запуск | один вызов spawn, пустая история | именованная CLI-задача + каталог/рантайм/модель, отслеживаемый дескриптор |
| Сообщение | `send_message`, подтверждение в отчёте | `send` → `queued` за 336 мс, доставлено после текущего хода, подтверждено |
| Наблюдение | статус агента, финальное уведомление | `peek`, `result --json`, `diff --stat` |
| Прерывание | `interrupt_agent` → running | `stop` → stopped/130, сессия сохранена |
| Возобновление | `followup_task` на том же агенте | `restart`, та же сессия `ses_ec9a7bdbaffe00Q1SQgpM22Xmu` |
| Диф/атрибуция | встроенный diff не проверялся | `diff --stat` от базовой линии старта, замороженный `.changes.json` |
| Дисциплина области | 9 чтений строго по заданию | 9 + 3 extra чтения staged-файлов (не строго-областное одобрение) |

Вывод только об удобстве управления обёрткой, не о качестве моделей:
результат совпал (1/6/2, та же находка, то же восстановление). Это ровно один
кейс, не бенчмарк: скорость (фаза 1 CLI 136.824 с, замера native нет) и
стоимость (обёртка сообщила $0 — не подтверждение биллинга) не ранжируются.
Восстановление покрывало read-only работу, не внешнюю транзакцию.

## 2. Текущее удобство SDK/CLI (реализованный контракт)

| Область | Реализовано | Исход |
|---|---|---|
| Launch | типизированный `spawn`, приватный prompt-файл, ownership binding, опциональный retained Codex/OpenCode profile (`isolate_runtime`, только с явным `--credential-env-keys`) | Контракт реализован; живой маршрут требует явного провайдера/модели |
| Messages | стабильные caller-UUID, durable receipt journal (`accepted`/`delivered`/`acknowledged`); дедуп same-UUID, unknown attempts fail closed в reviewed resume | Исторические receipts: 56 PASS receipts, 4 PASS state (переоткрываются любым later state change) |
| Control | generation tracking, `--pending-only` drain, `send --flush`, reviewed `restart --reviewed` с recheck report-SHA | Legacy ручной restart/flush сохраняет replay-риск |
| Recovery | opt-in reviewed resume: retained report SHA, recapture, session/generation/source/profile/effect checks | Исторически 18 PASS; отказ на stale/incomplete evidence, unknown attempts, unresolved effects |
| Resources | выход — быстрый reap; lingering settled owners identity-checked ≤ 300 с от observed settlement; сессии/профили/результаты retained | Завершённые worker-процессы ушли по точным PID, наблюдённым Root; сессии сохранены |
| Context | durable parent events, named consumer replay (прогресс только после успеха callback); observation timeout оставляет workers | Исторически 71 PASS receipt; `wait_any` timeout не останавливает |
| Limits | типизированные failure/cost/verification evidence; quota отделена от auth/TLS/network/OOM; exit zero ≠ QA | Hard billing caps не поддерживаются; hosted-chat injection не поддерживается; auth-файл не копируется автоматически; CAS writeback нет |

## 3. Актуальные gate-свидетельства vs исторические

| Gate | Историческое (сохранено, не стёрто) | Актуальное 2026-10-15 |
|---|---|---|
| Enforced scope | Аудит отклонил baseline (`69d87c07…`, запись вне AAP при 19 passing tests); V3 concurrent spawn-after-close + untracked `LocalFree` text найдены | **Root gate exit 0: 37 passed + 31 subtests passed, 23.54 с** (`security/root-enforcement.log`, JUnit `security/root-enforcement-junit.xml`), включает native child/escape, cleanup faults и **loopback-контроль**. Источник/дифф прочитаны Root (read 37). Фикстура: `C:/AI/outputs/neoxider-parity-v3-astra-20261014/suite-fixture.exe`. Этот gate заменяет старые формулировки «network untested» — **только для этого случая** |
| Profile text ownership | Указатель `LocalFree` терялся (`repro-old-bug.log` → `BUG_REPRODUCED`, `native_pending: []`) | Исправление активно в коде: `windows_enforcement.py` (`create_profile`): `owner=None` отклоняется до OS-вызова, профиль+SID трекаются сразу, успех+`LocalFree`-fault → `retain_release("profile-sid-text-free")` + retry ровно один раз, conversion-fault → checked `release_resources(..., raise_errors=False)`, исходная ошибка авторитетна. 4 новых теста (`ProfileTextReleaseTests`): `verify-text-release.log` 4 passed. Pre-fix/post-fix repro: `repro-old-bug.py`, `verify-postfix-repro.log` → `FIXED`. Полный дифф: `security/fix.diff` |
| Public enforcement | false, fail closed | **Остаётся false / fail closed.** Detached native helper — не общий sandbox; обычные провайдеры — declaration/diff only; outside-AAP escape baseline отклонён |
| SDK admission | Исторические: forged foreign-TaskHandle bypass, RLock pickle, shared prompt filename, recursion/zero-timeout, отклонённый промежуточный pump-start gap | **ПРИНЯТ в source gate.** Reviewed startup admission (детерминированный pump-start, pre-owner-publication, cleanup failed-start) + retained failure resources + PID-reuse тесты; SDK 63 PASS внутри полного прогона. Старая негативная история сохранена, это не текущее состояние |
| SDK промежуточные запуски (не приёмка) | — | `sdk/positive-orchestrator.log`: 57/58 (1 isolated rerun `adopt…first turn` deadline — причина НЕ установлена, rerun flakiness НЕ доказывает), `sdk/positive-parent.log`: 11 OK, `sdk/positive-parent-core.log`: import-ошибка harness (`test_core_parity_integration`), `sdk/negative-before.log`: известный race до фикса, `sdk/positive-adopt-rerun.log`: 1 OK. Это рабочие логи, не acceptance; старые полные SDK fail-логи и harness-ошибки сохранены |
| Текущий реальный SDK-прогон | — | Новый независимый SDK OpenCode Muse-прогон `C:/AI/outputs/parity-final-recovery-20261015/sdk-live-proof.json`: state `done`, exit 0, SHA результата совпал с входом `c679af20556cb9cc9d0c35982d6dd27fcd982833835cfe978cf904e5a45adb4a`, zero child resources, close failed/pending все пустые; точные owner 10772 / provider 2176 ушли (проверено Root). Сессия `ses_ec193c869ffes41PASFzAqxeq5` retained. Реальный текущий прогон, НЕ фикстура и НЕ гарантия квоты. Старые реальные read/write/messaging/stop/recovery OpenCode и 2 изолированных Codex-свидетельства сохранены с их историческими source/module-привязками |
| Live provider маршруты | 2026-10-14: SpaceBunny (UUID `41b5106e…` delivered + explicit ack, stop `0`/`130`, recovery `c8dc54c8…` → `waiting`) и Muse Spark 1.3 (`done`, exit 0); два конкурентных Codex с attach/send/ack/dedupe + 14 callbacks | Сохранены как proof тех exact cases; source-binding старше и раскрыт. Новое: fresh Muse free probe `parity-route-20261015` — `done`, exit 0, tool-read сигнатура, текущий cwd. **Только этот случай, не universal quota** |
| Runtime isolation | 26-test docs check (25 pass, 1 symlink-skip) — fixture-only; live isolation freeze dir retained | **BOUNDED component acceptance** per Root `root_isolation_acceptance.json` (`BOUNDED_RUNTIME_ISOLATION_ACCEPTED`, rechecked 2026-10-15, 0 new provider calls, no auth files read): исторический Codex-парный прогон 2026-10-14 — два реальных профиля, каждый initial + same-session resume `done`/exit 0, distinct `CODEX_HOME`, реальные хеши файлов проверены, ack/dedupe, 14 callbacks, billing unknown. Текущий `runtime_profiles.py` отличается ТОЛЬКО CRLF (нормализованный LF SHA `50db5d382b82c436c0270f9c8ca09f7ddf5286ccbc944c73244e54e4b76d8cc2` совпал с историческим; raw current `9532b858cd7e024ad7390c31cfb8a20dc5521b551df70edc945abe7405c98abf`). Это НЕ свежий whole-current-SDK isolated-pair rerun и НЕ общий OS sandbox; текущий обычный OpenCode live-прогон нового SDK — отдельная проверка |
| Fleet GUI | Fixture reconnect QA PASS (synthetic) | Root CLI 41 PASS (55.115 с), GUI 191 PASS, Node chat/tree + Fleet 12 групп PASS, fixture browser reload/reselect ровно 1 event (`fleet-replay.png`); `ROOT_GUI_VERIFICATION.md` фиксирует узкий default viewport и server-disconnect traces — без гарантий responsive/visible-terminal-window |
| Интеграция/публикация | Parity-фичи `2dd31e0` опубликованы; source gate ACCEPTED | **PUBLISHED_VERIFIED_CLEANED** по Root `C:/AI/outputs/parity-final-recovery-20261015/RELEASE_RECEIPT.json`, 2026-10-15T08:11:15 UTC: коммит `828c3b7136851bdc23ffaf8911ad992fdda896ea` опубликован/установлен, remote SHA проверен, собственные recovery-worktree/task-branch удалены. Датированная приёмка того выпуска |

Ограничения явно: никаких caps/hosted-chat/auto-auth-copy/auto-CAS; `win32u.dll`
может грузиться — не escape; `0xC0000142` для ordinary/`noWindow` детей;
оценки каталога и reported usage — не биллинг.

## 4. Статусы фаз (исторический PLAN.md § Phase status на 2026-10-15)

Фазы 1–5, 7 — bounded acceptance в source gate (см. PLAN.md); runtime
isolation — bounded component acceptance (историческая Codex-пара 2026-10-14 +
текущий CRLF-only компонент + 26 fixture-тестов, см. строку выше);
GUI — fixture QA (см.
строку выше); фаза 9: source gate ACCEPTED, исторический выпуск
`828c3b7136851bdc23ffaf8911ad992fdda896ea` опубликован/установлен, remote SHA
проверен и собственный cleanup завершён по Root-квитанции 08:11:15 UTC.
Исторические pending-статусы PLAN superseded этой квитанцией. Кодовых воркеров
того выпуска не осталось.

## 5. Ссылки на свидетельства

- `C:/AI/outputs/parity-final-recovery-20261015/security/root-enforcement.log`
  (37 passed, 31 subtests, 23.54 s, exit 0)
- `C:/AI/outputs/parity-final-recovery-20261015/security/root-enforcement-junit.xml`
- `C:/AI/outputs/parity-final-recovery-20261015/security/EVIDENCE.md`
  (CHANGED / VERIFY OUTPUT / NOT DONE / CONCERNS / QUESTIONS)
- `C:/AI/outputs/parity-final-recovery-20261015/security/fix.diff`
- `C:/AI/outputs/parity-final-recovery-20261015/security/verify-text-release.log`
  (4 passed), `verify-enforcement.log` (9 passed, 18 subtests)
- `C:/AI/outputs/parity-final-recovery-20261015/security/repro-old-bug.log`,
  `repro-old-bug.py`, `verify-postfix-repro.log`
- `C:/AI/outputs/parity-final-recovery-20261015/sdk/` (`orchestrator.before.py`,
  `source-sha-before.txt`, `positive-*.log`, `negative-before.log`)
- `C:/AI/outputs/neoxider-native-comparison-20261014/REPORT.md`, `evidence.json`
- Исторические: `C:/AI/outputs/neoxider-parity-core-20261014/`,
  `C:/AI/outputs/neoxider-parity-final-gates-20261014/`,
  `C:/AI/outputs/neoxider-parity-*-20261014/`,
  `C:/AI/outputs/neoxider-parity-v3-astra-20261014/suite-fixture.exe`
- Оригинальный PLAN: `C:/AI/work/parity-evidence-20261014/PLAN.md`;
  текущий durable PLAN: `PLAN.md` в корне worktree
- Root-авторитет source gate:
  `C:/AI/outputs/parity-final-recovery-20261015/root_source_gate.json`
  (SHA256 `ba196679…b60b6c8c3b`)
- Root-авторитет bounded isolation:
  `C:/AI/outputs/parity-final-recovery-20261015/root_isolation_acceptance.json`
  (`BOUNDED_RUNTIME_ISOLATION_ACCEPTED`; source gate `ba196679…` неизменён)
- Текущий реальный SDK-прогон:
  `C:/AI/outputs/parity-final-recovery-20261015/sdk-live-proof.json`
- Авторитет завершённой исторической публикации/cleanup (Root, 08:11:15 UTC):
  `C:/AI/outputs/parity-final-recovery-20261015/RELEASE_RECEIPT.json`

## 6. Завершённый исторический выпуск и пределы проверки

Root-квитанция `RELEASE_RECEIPT.json` от 08:11:15 UTC подтверждает commit, push,
проверку remote SHA, установку и cleanup собственного recovery-worktree/task-branch
для выпуска `828c3b7136851bdc23ffaf8911ad992fdda896ea`. Native fixture suite,
датированный живой SDK-прогон и ограниченная браузерная проверка описаны выше;
сверка квитанции не является новым runtime-прогоном. Общий sandbox
провайдеров, другие Windows/ABI, полная responsive-QA и поведение окон пользовательского
терминального хоста не подтверждены и не заявляются.

## CHANGED

Историческая документационная подготовка source gate затронула только шесть документов: `PLAN.md`, `SKILL.md`,
`docs/RELEASE_VERIFICATION_20261015.md` (финал source scope),
`docs/NATIVE_PARITY_REPORT.md` (исторический снимок),
`docs/ENFORCED_SCOPE.md`, `docs/ORCHESTRATOR_API.md`. Исходники, тесты,
конфиги, Unity, провайдер-вызовы не тронуты. Git-команды не выполнялись
вообще (включая status/diff); ложное утверждение о выполненном NoGit-действии
из старых черновиков удалено.

## VERIFY OUTPUT

Проверены локальные ссылки (все — на файлы в репо или абсолютные evidence-пути
выше), Markdown-таблицы и Python/PowerShell примеры (сигнатуры по
`ORCHESTRATOR_API.md`, против провайдера не запускались). Линк-чек: `SKILL.md`
→ `docs/ORCHESTRATOR_API.md`, `docs/CORE_INTEGRATION.md`, `docs/FLEET_UI.md`,
`docs/NATIVE_PARITY_REPORT.md` существуют; финальный отчёт линкуется из
`NATIVE_PARITY_REPORT.md`, `ENFORCED_SCOPE.md`, `SKILL.md`, `PLAN.md`.
Python-блоки `NATIVE_PARITY_REPORT.md`/`ORCHESTRATOR_API.md` распарсены AST
(синтаксис OK, выполнение против провайдера не проводилось).

## NOT DONE

См. раздел 6. Source scope принят, историческая публикация и cleanup завершены
по Root `RELEASE_RECEIPT.json`. Перечисленные ограничения runtime, sandbox,
биллинга и GUI-проверок сохраняются; новых проверок эти сведения не заявляют.

## CONCERNS

- `release_resources` retry — 2 bounded passes; стойкий `LocalFree`-fault
  остаётся pending by design (retryable, never lost).
- `owner=None` теперь hard-reject; будущие callers обязаны передавать owner
  (единственный callsite уже передаёт).
- Промежуточная pump-start ошибка исправлена; текущая версия принята Root после проверок выше.

## QUESTIONS

Нет блокирующих. Допущение: отказ от ownerless-создания до OS-вызова — intended
«unsupported» handling, т.к. единственный callsite всегда даёт owner.
