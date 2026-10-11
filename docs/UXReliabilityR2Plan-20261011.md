# R2 — bounded health, компактные результаты и границы файлов, 2026-10-11

Статус: **подход R2-H принят root после чтения полного diff, независимых 14/14 проверок diagnostics за 4.079 s и совпадения всех девяти исходных хешей. Публикация R2-H ожидается. R2-O/J остаются планами, их исходники не изменены; паритет с native не принят**. Native lead завершил F05; R1 опубликован root, удалённый SHA `b71c1d3afda13ddaae668bcc1dd5aad2d081696a` подтверждён. R1 доказывает ограниченные исправления scoped hashing и ранней идентификации. Новые модельные пробы и волна CLI пока не разрешены.

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

Только `neoxider_agents/reporting.py`, `tests/test_bounded_baseline.py`; sourcewave сериализована сR2-O. `Path.is_junction` whereavailable, Python3.8 stdlib mountpoint reparse-tag fallback; не запрещать произвольные cloud reparse points. Guard дляGit ancestors иfallback descent, relevant excludedlinkedsubtree partial/unknown; positive ownedfile/normalcloudtag иnegative realjunction/outsideworkspace byte-read tests. Scoped hash budgets/oldv2/mtime detection остаются. Actual OS-temp resolvedtargets проверить передcleanup, link удалить какlink, sourceQA originals сохранить.

## Передача и публикация

Накаждуюволну: root exactscope approval → controlled reproduction → narrowimplementation → namedmeaningfultests/mutation → actualdiff/sourcefinal. Новыхactive CLI launchers приtoolpatchнет; смешениеold/newcore запрещено. Root owns Git/NeoXider/no coauthors/push/remote equality. SourceF05/Unity work не входит вtoolwave; R2успех не закрывает UI/editor/game/runtime/network/mobile MVP.

## R2-H — текущая source-final передача root

Изменены только approved9code/test/docfiles и этотplanstatus; old UXReliabilityPlan.md содержит прежний отдельный authorized documentation delta, не sourceR2-O/J. Новая модельнаяCLIволна не запускалась. ProjectC# не затронуты.

Первичный self-review/root review обнаружил daemon-reader failure branches. Они заменены **nonblocking pipe drain**: existing Windowskernel/_declare PeekNamedPipe + os.read; POSIXselect+os.read. Никаких readerthreads/service/новогоframework. Owned active tree сохраняется доfinally, stdoutзакрывается наread/setup/wait/containment failures. VerifiedPID/generation fallback cleanup и bounded ожиданиесобственных потомков; diagnosticstdin=os.devnull устранил подтверждённый Windows inherited-temp-stdin lock race. Reason error/Unknown сохраняется, даже если fallback cleanup уже освободил tree. `process.py/windows.py` не менялись.

FINAL tests: `python -m unittest discover -s tests -p test_diagnostics_bounds.py`14/14Pass4.264s; `...test_providers_core.py`78/78Pass1.841s; `...test_core_contracts.py`11/11Pass0.598s. Positive+negative: per-vs-whole deadlines, opaque hung resolver/FS/network/provider, real trusted provider-info withstuckCLIversion descendant, inheritedstdout grandchild, outputcap/malformedJSON, setupread/wait/containment/descendant-inventory failure cleanup, partiallocal inventories иbytecap. Original compatibility assertions сохранены.

Реальный FINAL `agent.py doctor --json`: exit0,6.031989s,2126bytes; Codex/Gemini/Kimi/OpenCode stateok, Claudeknownnotloggedin, quotaabsenceне0%. Rawtext содержит actual env15.000s/10.000s/cleanup≤0.4s/helper. Earlier realrun Codextimeout remainedUnknown, healthyothersretained; этотслойнебылскрыт currentPASS. Helperprocesses послеsmoke не остались поnativeprocessscan. ExistingPOSIX arbitrarydetached-descendant limitation containment остаётся, онане объявлена устранённой.

Постоянные receipts: `D:/Temp/agents-ux/r2-health-20261011/doctor-smoke-final.json` и девять хешей в `source-final.json`; `git diff --check` — 0. Root прочёл полный фактический diff, независимо проверил 14/14 diagnostics за 4.079 s и подтвердил совпадение всех девяти хешей. Подход R2-H принят, исходники заморожены, публикация ожидается. R2-O/J, паритет с native, качество AI-провайдеров и успешное исполнение 15 команд остаются незавершёнными. Текущие количества проверок: **14 diagnostics / 78 providers / 11 compatibility**; прежние 13/77 были промежуточными числами.
