# R2 — bounded health, компактные результаты и границы файлов, 2026-10-11

Статус: **R2-H опубликован root в main `8375d78cfdf1a8866a77cecb457e6a358a7935c1`, push 0 и local/remote SHA совпадают. Подход R2-J принят root после полного diff, независимых 37 тестов и проверки обоих исходных хешей; source-final заморожен, публикация ожидается. R2-O и паритет с native не приняты**. Независимые проверки R2-H: 14 diagnostics / 78 providers / 11 compatibility — все PASS, девять хешей совпали. Свежий ограниченный doctor под нагрузкой завершился exit 0 с четырьмя Unknown/error и OpenCode ok; здоровье всех провайдеров не заявлено. Простая Apodex free/high Reply OK проба и отдельная tool/file проба завершены; подтверждена только базовая работа tools/файлов. Новых CLI запусков без отдельного root решения не будет.

## Фактические failures

- Текущий root doctor exec38779 несколько минут не выдал stdout. Root проверил PID/parent/creation и остановил только doctor Python33716 (pwsh36248, conhost72400), result exit1/output0. Другие providers/Unity не остановлены. Первый doctor ранее в этой сессии прошёл — currentfailure его не заменяет.
- Root pending получил155572tokens/758lines historical junk; у lead scoped pending unread failures, child STOPPED печатает raw partial/tool JSON. Saved `D:/Git/_Fork/VampireDota/Docs/QA/2026-10-11-mvp/fleet-pending-{lead,manager}.txt`.
- F02 fresh start11:03:24/новая generation после первогоrun, rawmeta осталсяrunning при мёртвомowner; effectiveStopped выводит прежний stopped_at11:01:35 и duration чужого lifecycle. Изолированное старое metadata/stop-report надо различить reproduction, не предполагать одну причину.
- Controlled OS-temp junction: `is_symlink=false`, `is_junction=true`, Git и walk hash прочли outside-workspace test-only file. Actual receipt `D:/Temp/agents-ux/r1-20261011/junction-review.json`.
- Пятнадцать настоящих teamIDs зарегистрированы;14watchdog124 и1effectiveStopped, assignedCLIoutputs0. Root/title/primary logs показывают real reads/tools, context85–126k и Novita429. Это не сравнительный benchmark и не доказанная единая причина latency. Все активные code launchers завершены; никаких слепых повторов15.

## Actual call graph и выявленные неограниченные области

`neoxider_agents/diagnostics.py:doctor` делает `ThreadPoolExecutor.map` в context manager: нет per-provider/whole-doctor deadline, slow filesystem/resolve/network action может задержать всё; timeout Future без безопасного termination недостаточен — context shutdown/join всё равно ждёт.

`providers.py:BaseProvider.doctor→executable/which_native→capture` имеет8s только внутри subprocess.run. PATH/native shim filesystem lookup и plugin doctor не ограничены этим timeout. Direct child capture не доказывает закрытие grandchildren/pipe.

`providers/claude/provider.py:_usage` до network usage читает рекурсивно все недавние transcript bytes без общего time/file/byte budget. HTTPS timeout5s не ограничивает эту localscan. `providers/codex/provider.py:_limits` перечисляет/сортирует все session paths поmtime, затем читает tail8files: tail bounded, enumeration/sort нет. Это concretecandidate causes, **не утверждение, что именно они повесили конкретный rootcall**.

`views.py:pending` печатает full `stop_block` для каждогоStopped; `reporting.make_stop_block` использует old stopped_at/partial/changed_names, не имеет outputcap. `lifecycle.run/execute` не обнуляет все terminal metadata приfreshrun; snapshot generation и stop report должны согласоваться.

## R2-H — жёстко ограниченный doctor/health

Root-approved exactsource scope: `neoxider_agents/diagnostics.py`, `neoxider_agents/providers.py`, `providers/claude/provider.py`, `providers/codex/provider.py`, новые `tests/test_diagnostics_bounds.py`, existing `tests/test_providers_core.py`, `docs/PERFORMANCE.md`, `README.md`. Дополнительный rootgrant `tests/test_core_contracts.py` только two doctor fixtures дляmock новойbounded-probe границы; все originalshape/state/reset/deep assertions сохраняются. Root принимает один narrow batch, не все флотские файлы.

Решение: provider-info probes выполнять в контролируемых helper processes через **existing process tree/Windows Job Object implementation**, ограничивать per-provider иwholecall wall-time. Не вводить daemon/supervisor/service/framework и не менять lifetime рабочихагентов. Defaults proposed15s whole/10s perprovider; допустимое env tuning явно печатается. Приdeadline/ошибке вернуть bounded partial JSON, state timeout/error, availability/limits **Unknown**, не False/notinstalled/ok и не выдуманный reset. Остальные здоровые engines сохраняют результат.

Предел включает executable resolution/local scans/DNS/network/child CLI. Безопасное убийство только diagnostic helper tree поPIDgeneration; CLI snapshot/read commands здоровых tasks не затрагиваются. Provider plugins получают cooperative budgets localscan (time/files/bytes) как оптимизацию, но жёсткая supervisor граница нужна даже для stuck filesystem. Cloud/proxy credentials не печатать; transcriptestimate partial/Unknown не вызывать «остатком квоты».

Acceptance: fake provider neverreturns; stuck resolver; filesystem scan budget; delayed network/read; helper leavesstdout grandchild; одинslow+другойhealthy; malformedJSON/provider error. Actual command завершается вdeclareddeadline+tightcleanupmargin, no ownedhelperchildren/no visiblewindows. Existing provider/nativeentry tests; новые positive+negative tests и targeted planteddeadlinefailure. `doctor --json` безAI-модельного вызова отдельно; opt-in bounded real toolprobe идёт только послеbasichealth и не требует15одновременных endpoints.

## R2-O — output bounds и generation-correct state

Proposed exactscope отдельной волны: `neoxider_agents/views.py`, `reporting.py`, `lifecycle.py`, `cli.py` (толькоесли существующийverbose не проходитoptions), `ux_tracking.py` (толькодляboundedresult presentation), existing `tests/test_agent_control.py`, `test_ux_tracking.py`, `test_limit_reporting.py`, `README.md`. Не менять simulation/provider routing.

Default pending — одна компактная строка/task, bounded items/totalchars и countomitted; подробности только явный `--verbose`/last/result ссылка. Current status/raw result сtail0 не долженdefault печатать неограниченныйtranscript/changed_names. Полный artifact и machineJSON остаётся доступен через явныйfull/export механизм; backwardcompatibility API mode фиксируетсяrootдоimplementation. Source/file/partial/limit warnings не теряются приcompact display.

Fresh generation атомарно обнуляет stopped_by/stopped_at/partial_result/old files+changes/exit; .stop cache принимаетсятолькодляэтогопоколения или очищаетсяпоexistingnewrun протоколу. Legacy stop artifacts остаются читаемыми. Оба boundedstate reads и explicit orphanstop/result/diff остаются отдельными — нельзя возвращать orphandelta scan подglobalownershiplock. Pending не долженконтролировать/рестартоватьagents.

Acceptance: stale stoppedmetadata+newowner→harddeath показываетnewgeneration; sameactor fresh/restart/send originalbaseline contracts сохраняются; default pending на200historicaltasks/гигантскихchangednames/partialjson имеетboundedoutput иomittedcount, detailedexport faithful; no secrets; strict exit semantics не прячутся. Отдельно фиксируется наблюдаемое PowerShellexec exit1 приcore strict3 — выяснить launcher propagation source, не молча нормализоватьfailure в0.

## R2-J — junction boundary (root exactscope уже принят)

Разрешённый scope: только `neoxider_agents/reporting.py`, `tests/test_bounded_baseline.py` и статус/решение в этом документе. До начала проверены живые процессы: CLI AI launchers завершены, совпадение в текущем process scan относится только к собственной проверяющей PowerShell-команде. R2-O не реализуется параллельно.

Принятое решение: единая проверка директории сохраняет существующий запрет symlink, использует `Path.is_junction()` при наличии и Windows `lstat().st_reparse_tag == IO_REPARSE_TAG_MOUNT_POINT` как совместимый с Python 3.8 fallback. Другие reparse tags, включая облачные файлы, не запрещаются. Git-кандидат с linked ancestor не хешируется; fallback walk исключает relevant linked directory до спуска. Ошибка проверки директории или исключённый relevant link помечает snapshot partial с неизвестным числом непрочитанных файлов. Не выдавать такой пустой результат за полную проверку без изменений.

Волны: сначала контролируемые реальные junction-тесты на текущем коде с независимой проверкой разрешённого пути при попытке `Path.open`; затем минимальный production guard обеих веток и тест fallback без нового API; затем полный bounded baseline и существующие tracking/control проверки. Положительные проверки читают обычный owned file, отрицательные обнаруживают любое открытие файла через junction вне workspace. При наличии junction и не относящемся к нему owns snapshot остаётся полным. Partial-baseline и current-partial предупреждения сохраняются.

Проверка: `python -m unittest discover -s tests -p test_bounded_baseline.py`, `test_ux_tracking.py`, `test_agent_control.py`, реальные Windows junction fixtures в обоих маршрутах, before/after результаты и `git diff --check`. Fixtures создаются только в собственном OS-temp дереве; перед cleanup проверяются абсолютные lexical/resolved пути. Junction снимается как link до удаления собственного временного дерева; чужие каталоги не удаляются. Исходный receipt `D:/Temp/agents-ux/r1-20261011/junction-review.json` не заменяется.

Риски и границы: это исправление обхода заранее существующего directory junction, без глобального запрета reparse points, новых сервисов или изменений маршрутизации. Сохранены owns, v2 baseline, follow-up, хеши при восстановленном mtime и прежние budgets. Атомарная защита от конкурентной подмены файловой системы между проверкой и open не вводится; такое более широкое усиление потребовало бы отдельного решения. Source-final и фактический diff принимает root до новой CLI-волны.

## Передача и публикация

Накаждуюволну: root exactscope approval → controlled reproduction → narrowimplementation → namedmeaningfultests/mutation → actualdiff/sourcefinal. Новыхactive CLI launchers приtoolpatchнет; смешениеold/newcore запрещено. Root owns Git/NeoXider/no coauthors/push/remote equality. SourceF05/Unity work не входит вtoolwave; R2успех не закрывает UI/editor/game/runtime/network/mobile MVP.

## R2-H — текущая source-final передача root

Изменены только approved9code/test/docfiles и этотplanstatus; old UXReliabilityPlan.md содержит прежний отдельный authorized documentation delta, не sourceR2-O/J. Новая модельнаяCLIволна не запускалась. ProjectC# не затронуты.

Первичный self-review/root review обнаружил daemon-reader failure branches. Они заменены **nonblocking pipe drain**: existing Windowskernel/_declare PeekNamedPipe + os.read; POSIXselect+os.read. Никаких readerthreads/service/новогоframework. Owned active tree сохраняется доfinally, stdoutзакрывается наread/setup/wait/containment failures. VerifiedPID/generation fallback cleanup и bounded ожиданиесобственных потомков; diagnosticstdin=os.devnull устранил подтверждённый Windows inherited-temp-stdin lock race. Reason error/Unknown сохраняется, даже если fallback cleanup уже освободил tree. `process.py/windows.py` не менялись.

FINAL tests: `python -m unittest discover -s tests -p test_diagnostics_bounds.py`14/14Pass4.264s; `...test_providers_core.py`78/78Pass1.841s; `...test_core_contracts.py`11/11Pass0.598s. Positive+negative: per-vs-whole deadlines, opaque hung resolver/FS/network/provider, real trusted provider-info withstuckCLIversion descendant, inheritedstdout grandchild, outputcap/malformedJSON, setupread/wait/containment/descendant-inventory failure cleanup, partiallocal inventories иbytecap. Original compatibility assertions сохранены.

Реальный FINAL `agent.py doctor --json`: exit0,6.031989s,2126bytes; Codex/Gemini/Kimi/OpenCode stateok, Claudeknownnotloggedin, quotaabsenceне0%. Rawtext содержит actual env15.000s/10.000s/cleanup≤0.4s/helper. Earlier realrun Codextimeout remainedUnknown, healthyothersretained; этотслойнебылскрыт currentPASS. Helperprocesses послеsmoke не остались поnativeprocessscan. ExistingPOSIX arbitrarydetached-descendant limitation containment остаётся, онане объявлена устранённой.

Постоянные receipts: `D:/Temp/agents-ux/r2-health-20261011/doctor-smoke-final.json` и девять хешей в `source-final.json`; `git diff --check` — 0. Root прочёл полный фактический diff, независимо проверил 14/14 diagnostics за 4.079 s и подтвердил совпадение всех девяти хешей. R2-H опубликован в main `8375d78cfdf1a8866a77cecb457e6a358a7935c1`; последующая проверка под нагрузкой сохранила четыре Unknown/error, поэтому полного здоровья не заявлено. Паритет с native, качество AI-провайдеров и успешное исполнение 15 команд остаются незавершёнными. Текущие количества проверок: **14 diagnostics / 78 providers / 11 compatibility**; прежние 13/77 были промежуточными числами.

## R2-J — итоговые проверяемые результаты и source-final

Изменены только разрешённые `reporting.py`, `test_bounded_baseline.py` и этот план. Production guard сохраняет symlink-проверку, использует `Path.is_junction` и mountpoint tag fallback, исключает relevant junction в Git ancestors и walk descent. Ошибка доступа оставляет partial/unknown; обычный удалённый родитель (`FileNotFoundError`) сохраняет достоверное удаление. Облачный tag не объявляется junction.

До production-правки новая выборка `python -m unittest discover -s tests -p test_bounded_baseline.py -k junction -v` получила **6 tests / 5 failures / 1 pass**, exit 1, 0.221 s. Четыре реальные Windows fixtures (Git/walk × доступный/отключённый новый API) зафиксировали попытку открыть `linked/secret.txt` вне workspace независимым resolved-path guard; пятая ошибка показала ложную достоверность delta. Unowned junction positive case уже проходил. При самопроверке первоначального patch отдельный новый case удаления normal owned directory воспроизвёл FAIL; точная обработка отсутствующего parent устранила эту регрессию.

На окончательном source: `python -m unittest discover -s tests -p test_bounded_baseline.py` — **37 tests OK, 36 passed / 1 skipped**, 14.498 s, exit 0. Skip — прежний symlink WinError1314; ни один junction case не пропущен. Все четыре реальные junction routes: `escaped=[]`, открыты только `owned/input.txt` и `normal/nested.txt`, их SHA совпадают с независимым ожидаемым значением; snapshot partial и not_tracked неизвестен. Cleanup проверил lexical/resolved targets, снял junction как link и сохранил его controlled target. Новый API отключён на Python 3.13 для проверки fallback; реальный Python 3.8 interpreter здесь не запускался. Cloud-tag positive — синтетическая подмена только metadata, не проверка реального облачного провайдера.

`python -m unittest discover -s tests -p test_ux_tracking.py` — **52/52 PASS**, 0.998 s. `python -m unittest discover -s tests -p test_agent_control.py` — **27/27 PASS**, 265.413 s, exit 0; это изолированные локальные fixture helpers, не AI launchers. Tracking suite предшествовала заключительному FileNotFoundError уточнению; control suite работала во время него. Окончательный source независимо проверен root полным baseline: **37 tests OK / 1 existing skip**, 14.024 s, оба SHA до/после одинаковы. Scoped post-scan показал оставшийся fixture legacy Bash PID66172, parent43204, creation13:13:33, command ORIGINAL с work `D:/Temp/agents-ux/legacy-tests/control-2em7x9hf/work`. Поэтому завершение всех helpers не заявлено; root уведомлён, J scope не расширяется до control lifecycle patch.

Исходники заморожены. `reporting.py`: SHA256 `94408b30aa161b0bc8c340a464a7da9ccae62a8ffa71f97c09611f8cc0438972`; `tests/test_bounded_baseline.py`: `4212c3773e056ece915fc2517da12f58590afb010fa06f276dce0579fbd358b3`. Итоговый manifest/receipt по запросу root: `D:/Temp/agents-ux/r2-junction-20261011/source-final.json`. Root прочёл оба production изменения и девять новых cases, принял подход; Git/push выполняет root отдельно. R2-O, TOCTOU между check/open и native parity не закрыты.

Свежий bounded inventory root после source-freeze: настоящий `doctor --json` завершился exit 0 за 8.3039148 s; OpenCode1.18.27 state ok, Codex ok с limits null/Unknown, Gemini/Kimi ok, Claude не logged in с partial local usage. Receipts `D:/Temp/agents-ux/r2-junction-20261011/root-doctor.json` и `root-doctor.stderr.txt`. Первое перенаправление не запустило doctor из-за отсутствующей temp-директории; последующий реальный вызов отдельный. Это текущая доступность CLI, не остаток квоты или качество модели; повторный doctor без изменения состояния не нужен.

Повторная проверка разрешённого residual fixture PID66172 перед возможным exact Stop-Process не нашла этот PID, его отсутствовавшего parent43204 или children. Последующий scoped scan по control fixture command также пуст. Stop-Process не выполнялся; причина завершения неизвестна. Исторический leftover сохраняется как concern контрольного lifecycle, текущих fixture helpers на этой проверке не найдено.

## R2-E — Codex recoverable retry не должен завершать CODE task

Статус: **план и controlled regression; production source scope подтверждается root до записи**. Все AI launchers завершены. J/H исходники не меняются. Единственный текущий native reliability lead исполняет уже назначенную задачу, новых native нет; крайняя необходимость — CLI CODE заблокирован подтверждённой классификацией recoverable event.

Факт: task mvp1011-codex-asset-skill закончил provider через43s на JSON `type=error`, message `Reconnecting... 2/5 (stream disconnected before completion: tls handshake eof)`. Это начало собственного повторения CLI, не терминальное исчерпание пяти попыток. Short probe был OK; account quota38% used не исчерпана. TLS/network сами этим исправлением не устраняются.

### Реальный Core2 call graph и нужный exact scope

`providers/codex/provider.py:30–59` строит прямой `codex exec ... --json`. `neoxider_agents/runtime.py:134` вызывает Provider.command, затем Popen через existing spawn. JSON проходит `neoxider_agents/output.py:291–292`: любой Codex error/turn.failed вызывает `_error` (218–222), который выставляет provider_error и печатает AGENT_PROVIDER_ERROR. Runtime210–211 немедленно убивает собственное tree с126. Bash `providers/codex/provider.sh` содержит аналогичную логику, но **не находится в текущем agent.ps1/Core2 launch path**. Исправить только .sh недостаточно.

Предлагаемый scope для root: `neoxider_agents/output.py` только Codex event branch и маленький приватный classifier; `providers/codex/provider.sh` только аналогичный Codex parser для legacy parity, если root включит; новый `tests/test_codex_retry_signals.py`; статус/этот amendment в `docs/UXReliabilityR2Plan-20261011.md`. Existing `tests/test_providers_core.py` читается/запускается для compatibility, его assertions не меняются. Не менять Runtime/lifecycle/OpenCode/shared routing, provider settings/credentials, H/J source или другие test files.

### Узкое решение

Только Codex `type=error` с точным retry-status prefix Reconnecting... n/m, валидными конечными positive attempt/count и явно recoverable transport body становится диагностикой; не выставляет provider_error и не печатает fatal marker. Учитывать последний retry n=m как ещё исполняемую попытку: успешный final после неё допустим. `turn.failed` всегда terminal. Без retry prefix обычный TLS error не становится безопасным. Auth/quota/429/context/model errors остаются fatal даже если обёрнуты retry-status строкой; проверить permanent markers до исключения. Unknown shapes оставляют прежнее fatal поведение, а не молчаливый skip.

Диагностика остаётся в raw/activity/log. Existing watchdog whole/silence/control и финальный process exit code сохраняются. Если CLI после recoverable сообщений завершился nonzero, это error/nonzero, не success и не выдуманная quota/reset; terminal error event остаётся fatal. Это не общий signals framework и не разрешение игнорировать все provider errors. Legacy single-quoted embedded Python не должен получить apostrophe, иначе Bash script сломается.

### Проверка до/после

Новый `tests/test_codex_retry_signals.py` использует существующий OutputFilter и реальный `runtime.Turn` с controlled Python child (engine=codex), isolated D:/Temp fixture Store. Child пишет exact retry JSON, ждёт дольше watchdog poll, потом пишет normal agent_message RECOVERED_OK и exit0. BEFORE должен дать126/не дойти до controlled receipt; AFTER —0, final answer intact, без AGENT_PROVIDER_ERROR. Не использовать AI/network для этого regression.

Negative cases: quota/auth/context error; wrapped permanent marker; malformed retry counters/body; terminal turn.failed; retry followed by exhausted nonzero exit; retry then stuck helper → existing watchdog124 и owned process cleanup. Existing test_providers_core сохраняет inline sessions, large answers, real fatal markers и остальные engines. Если legacy .sh разрешён, проверить именно его embedded parser теми же streams, без фактического Codex API.

Команды: `python -m unittest discover -s tests -p test_codex_retry_signals.py`; `python -m unittest discover -s tests -p test_providers_core.py`; Bash syntax check только изменённого provider.sh при разрешении; scoped `git diff --check`, hashes/source-final и root полный diff. Реальный CLI CODE повтор только после root source acceptance. Не лечить TLS/proxy/квоту этим parser fix и не менять R2-O pending/state work.

Root уточнил окончательный boundary R2-E перед production patch: только 0 < attempt < max диагностический, attempt=max (5/5) остаётся fatal по существующему контракту. Это явный выбор root заменяет предложенное выше допущение последней retry-попытки. Scope принят: только `neoxider_agents/output.py` Codex branch/local classifier, новый `tests/test_codex_retry_signals.py` и этот план. Bash legacy provider.sh не меняется, defect там остаётся documented concern. Existing test_providers_core.py только запускается, не редактируется.

Controlled BEFORE реально выполнен на текущем Core2: новый suite8 cases,6 failures,2.012 s, exit1. Delayed recover child получил126 вместо0 и не дошёл до RECOVERED_OK; retry+hang получил126 вместоwatchdog124; retry+nonzero получил126 вместо7; terminal flow не дождался настоящего turn.failed. Permanent quota/unknown/other-engine cases сохранились. Source Python direct OutputFilter подтверждён, поэтому .sh-only patch не исправил бы текущий путь. Затем тестовый boundary приведён к явному root5/5 решению; owned fixture pipe cleanup добавлен в тест без изменения Runtime.

### R2-E — окончательная передача source-final

Изменены только разрешённые `neoxider_agents/output.py`, новый `tests/test_codex_retry_signals.py` и этот план. Root согласовал 0<attempt<max для exact observed TLS reconnect notice;5/5 остаётся fatal. Любые другие shapes/transport messages, quota/auth/context и turn.failed сохраняют прежний fatal путь. Diagnostic попадает в raw/activity/log, без AGENT_PROVIDER_ERROR и без подавления последующего terminal/nonzero результата. Runtime/lifecycle/OpenCode/providers и H/J файлы не изменены. Bash legacy parser дефект остаётся отдельным concern.

Последний `python -m unittest discover -s tests -p test_codex_retry_signals.py`: **8/8 PASS**,3.144s, exit0. Controlled live Turn действительно дождался receipt+RECOVERED_OK после retry; hang сохранилwatchdog124; exhausted nonzero сохранил7 безquota; quota/terminalerror остановились126. Boundary feedcases проверили1/5,2/5 positive и0/5,5/5,6/5,2/0/permanentwrapped/unknown negatives; OpenCode fatal unchanged. Последний `python -m unittest discover -s tests -p test_providers_core.py`: **78/78 PASS**,1.805s, exit0; файл не изменён.

Первый patch не имел importre, tests выявилиNameError; import добавлен и повторныеfocused tests прошли. Не скрывать этот промежуточный failure или выдавать его за source-final. Scoped diffcheck exit0. J source reporting94408…/baseline test4212… unchanged. AI/network не вызывались regression; source заморожен для полного root review, реальная CODE task возобновляется только после принятия. TLS/proxy/квота не исправлены этим изменением. Receipt `D:/Temp/agents-ux/r2-codex-retry-20261011/source-final.json`.

### Исправление boundary по первичному исходнику Codex0.160.0

Root сверил и native lead независимо прочёл [responses_retry.rs тега rust-v0.160.0](https://raw.githubusercontent.com/openai/codex/rust-v0.160.0/codex-rs/core/src/responses_retry.rs). notify_stream_error для retry_count/max_retries вызывается до sleep и возврата Ok, включая retry_count=max_retries. После исчерпания возможно переключение WebSocket→HTTPS со сбросом счётчика. Поэтому прежнее root5/5fatal указание ошибочно и заменено: **0<attempt<=max** диагностический для exact TLS retry notice. Исчерпание подтверждается terminal turn.failed/эксплицитной terminal ошибкой или nonzero process exit, не одним счётчиком. Wall watchdog остаётся прежним и не продлевается.

До изменения production comparison добавляется настоящий delayed5/5→RECOVERED_OK test; прежний код должен получить126. Дополнительный negative test передаёт turn.failed с тем же exact2/5 body и требуетfatal. Invalid6/5/permanentwrap/unknown shapes остаютсяfatal. Production scope по дополнительному grant только один comparison `<`→`<=`; шаблон сообщения не расширяется, legacy .sh не меняется. Ожидаемый окончательный test suite9, фактические counts будут записаны после запуска.

### R2-E — окончательный boundary и результаты после первичного уточнения

Последний `python -m unittest discover -s tests -p test_codex_retry_signals.py -v`: **9/9 PASS**, 4.019 s, exit 0. Новый настоящий child с last notice5/5 действительно дошёл до RECOVERED_OK и controlled receipt; до `<=` получил126, отдельный BEFORE1case FAIL0.249s. Дополнительный negative turn.failed с тем же exact2/5 body осталсяfatal. Existing compatibility `test_providers_core.py`: **78/78 PASS**, 1.641 s, exit 0. Публичная CLI retry semantics подтверждена primary source URL выше; wall watchdog неизменен, бессрочный retry не разрешён.

Окончательный source-final заменяет промежуточный8case manifest; предыдущий JSON сохранён отдельно как source-final-eight-case.json. Новые hashes и counts в `D:/Temp/agents-ux/r2-codex-retry-20261011/source-final.json`. Source снова заморожен для root diff/independent tests. Старое5/5fatal правило отмечено ошибочным; Runtime/OpenCode/legacy parser и H/J не менялись. Это восстановление правильного client retry protocol, не устранение TLS/network failure и не доказательство успешного CODE delivery.
