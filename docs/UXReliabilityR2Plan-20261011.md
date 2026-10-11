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
