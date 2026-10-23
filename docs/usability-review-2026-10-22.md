# End-to-end usability review — 2026-10-22

Cortex accomplished simple reasoning, a contextual correction and a real app
launch. It is **not yet a consistently useful general personal assistant**.
API reads and worker execution now work in cases where final interpretation,
approval handling or synthesis still prevents a useful answer. Test-suite
success is not a substitute for those remaining live gates.

## Method and boundaries

Twelve text requests (ten original cases and two repair retests) used the normal
authenticated `/api/control/message` path, production task database, legitimate
ASUS credentials, existing provider configuration and actual native PyQt app.
No model responses, graphs, execution results or successful plans were injected.
Voice was mocked. No account writes, new scopes, paid providers or quota overrides
were used. Sensitive data-sharing and execution approvals were not automatically
approved. The native administrative dashboard was not used as a Workspace substitute.

Evidence directory:
`C:\Users\abhin\Documents\Codex\2026-10-15\referenced-chatgpt-conversation-this-is-an\work\usability-review-20261022`.
It contains the prewritten request manifest, owned task summaries, sanitized
provider metrics, real integration provenance, graph evidence and regression XML.
Private integration contents are omitted from the report and result summaries.

## Requests and actual outcomes

Times below are observed submission-to-terminal times, including provider waits
and approval waits. Acknowledgements were 1.51–3.40 seconds. API task status
`COMPLETED` alone is not treated as proof the user's goal was achieved.

| Request | Intent/capability and authorization | Execution and verification | Final answer/usefulness | Time |
|---|---|---|---|---:|
| Divide a 45-minute study session across three topics after reserving nine minutes | Correct reasoning; no capability needed | Calculation checked: `(45-9)/3=12` | Correct, natural answer | 5.44s |
| “Actually make the session an hour, keeping that same quiz reservation” | Correct correction and reference resolution | Preserved three topics and nine-minute quiz: `17` each | Correct contextual answer | 2.06s |
| Bring up Windows Calculator on ASUS | Real model selected `app.launch`; permitted | Actual ASUS launch receipt succeeded | Grounded launch confirmation; independent native Calculator inspection not completed | 7.12s |
| Calculator scientific mode and square root of 144 | Model proposed a compound plan; real `ui.observe` | Cloud-sharing approval expired; no UI input dispatched | Truthful denial; requested result not achieved | 74.63s |
| Copy a harmless synthetic text file and verify it | Correct `fs.copy` proposal; execution approval required | Cancelled through a fresh authenticated Workspace action while awaiting approval; no copy dispatched or output file created | Cancellation preserved; copy goal uncompleted | 144.53s including human wait |
| Current ASUS processor usage | Wrong selection: battery-only `device.status` | Battery receipt real; CPU reading not obtained | Truthfully admitted missing CPU measurement, but failed user's goal despite success status | 5.18s |
| Newest unread Gmail subject and sender | Correct read-only `email.search`, default `primary` account resolved | Actual Google Gmail search returned one ID; subsequent interpretation required cloud-sharing approval | Approval expired; no message headers or useful final answer obtained | 100.46s |
| Next default Calendar event | Selected falsely advertised `calendar.get_next` | Rejected as NOT_ADVERTISED; no Google read | Genuine capability catalog defect, fixed below | 3.95s |
| Wait 35 seconds, then report completion | Correct harmless `ui.wait`; real ASUS receipts | Two bounded waits completed; provider delays continued; cancelled via normal control API | Truthful cancellation, not a completed 35-second goal | 71.59s |
| CPU repair retest, same wording | Model responded from supplied context | Returned `12.4%`; **no fresh telemetry capability receipt** was produced | Better answer, but this evidence does not establish fresh dedicated sampling or prove description change caused improvement | 2.33s |
| Calendar repair retest, same wording | Real model selected `calendar.list`; default `primary` resolved | Actual Google Calendar read returned one event; cloud interpretation denied by existing privacy policy | Service execution repaired; conversational acceptance still blocked | 30.32s |
| Independent workers analyze retrieval quizzes and explaining aloud, then compare | Real model selected `agent.orchestrate`; real four-worker graph admitted | All four model workers completed, independent overlap 1.318527s, dependent ordering correct | Frozen conflict detector rejected generic claim-key collisions before synthesis; no verified unified answer | 41.77s |

Native progress/result rendering is **unverified per case**. The normal endpoint,
durable task state and structured Workspace API were real; those are not a native
rendering pass. The first reasoning request was later deliberately used to
reproduce a stale-cancel defect: its owning request was incorrectly relabelled
cancelled while its successful AgentTurn/result remained recorded. That historical
test artifact was not rewritten to conceal the defect. Its initial completion time
is used above, not the later cancellation timestamp.

## Repairs

1. `hub/api_control.py`: reject cancellation of terminal durable requests, goals
   and execution records with HTTP 409 before invoking cancellation or rewriting
   state. A real stale cancel returned 200 and corrupted a completed review task
   before repair. After restart the same control against another completed review
   task returned 409 and preserved its state. Nine behavioral checks cover terminal
   requests/goals/executions and verify no cancellation/lease action occurs.
2. `hub/capability_registry.py`: describe battery/AC power separately from CPU,
   RAM and disk usage. Their identifying information now survives the existing
   compact catalog's description prefix. This changes metadata, not phrase routing.
   The CPU retest's lack of a fresh receipt remains a validation limitation.
3. `hub/capability_registry.py`: retain `calendar.get_current`, `calendar.get_next`
   and `calendar.get_availability` as inspectable unavailable legacy declarations,
   excluding them from actionable catalogs. None has an execution handler. Three
   behavioral checks verify inspection remains possible, validation rejects them,
   actionable lists omit them and the real `calendar.list` contract stays intact.
   No replacement phrase handler, time-window shortcut or fabricated result was added.

Modified production files: only `hub/api_control.py`, `hub/capability_registry.py`.
Modified tests: `tests/test_gui_controls.py`.
Added tests: `tests/test_usability_repair.py`.
Documentation: this report and `docs/usability-frozen-findings-2026-10-22.md`.
Review runners and synthetic fixtures are separate evidence, not production routes.
Existing unrelated uncommitted work was retained; no commit/push performed.

## Provider failures versus Cortex defects

Across the twelve requests, the existing bounded router made 44 provider attempts:
Groq GPT-OSS 21 (14 successful results), Groq Qwen 16 (10 successful results),
Gemini seven (no successful result). Thirteen Groq failures retained sanitized
HTTP metadata: two 400 `tool_use_failed`, three 413 `rate_limit_exceeded`, eight
429 `rate_limit_exceeded`. Gemini failure status bodies were not present in this
run's sanitized evidence; prior 503 history is not assigned to these seven attempts.

Some GPT-OSS 429 responses explicitly identified TPM, limit 8,000, with requested
tokens exceeding remaining capacity. Qwen error quantities included limit 7,000
and requests above that bound. Their scope was not retained; HTTP 413 is not taken
as proof of model context-window exhaustion. Frozen routing's existing 413 behavior
was not altered. No cooldown was cleared, quota bypassed, or paid fallback added.

Largest measured wire payload: **30,943 bytes**: system 23,781, capability catalog
9,980, state 6,035, tool schema 3,916; output allowance 1,024 tokens. The 7,736-token
wire estimate is byte-based, not a tokenizer measurement or provider quota fact.
Wire bytes and the planner's separate input limit are different quantities.
Catalog/session overhead and tight free-tier admission budgets materially impair
latency. External quotas do not explain the Calendar placeholder defect or A14's
false conflict classification.

## Reliability, interface and security

- Normal Hub, authenticated ASUS Agent and PyQt app started; ASUS advertised 42
  capabilities. Native Overview showed Hub connected and ASUS online.
- Workspace navigation failed with `SendInput sent 0 of 1 events; GetLastError=87`.
  Later Computer Use was **stopped by the user with physical Escape**. No further
  Computer Use was attempted. Native Workspace cards, graph, refresh, cancellation
  button and reconnect rendering are therefore **UNVERIFIED**.
- Actual loopback socket inspection matched agent PID 42840 and desktop PID 38464
  on IPC port 58711 with an established connection. This proves TCP connectivity,
  not live microphone, speech recognition, speech output or end-to-end voice.
- Active request cancellation propagated to owning request and AgentTurn. The file
  approval cancellation and real wait cancellation were API controls, not native clicks.
  Already-dispatched bounded waits can finish; cancellation does not imply reversal.
- A normal restart retained all 18 then-existing owned terminal task rows, IDs,
  statuses, timestamps and context hashes unchanged. This proves terminal durability;
  interrupted side-effect recovery/no replay is not freshly established here.
- Disconnect: all 42 ASUS capability candidates became unavailable/non-executable
  with DEVICE_OFFLINE. Reconnect with existing credentials and fresh advertisement:
  42 available, 34 executable, eight requiring approval. Native projection unverified.
- Authenticated real rejection probes: forged message source 403, executable
  Workspace content 422, unowned Workspace control 403, invalid credential 401.
- No sensitive approval was automatically granted. Gmail sharing expired; Calendar
  data sharing was policy-denied. No external writes or scope changes occurred.
- A cancelled execution approval remained as a stale pending DB row. Its active
  future had been removed; cancellation did not dispatch the copy. Only this owned
  synthetic fixture's orphan approval was explicitly denied during cleanup. The
  frozen approval cleanup limitation is documented separately.
- Native Overview counted about 20 older active tasks despite no corresponding
  review work. They were not blindly relabelled, deleted or replayed. This remains
  a misleading legacy-history/activity limitation.

## Automated validation

Initial focused run omitted the expected `CORTEX_HUB_DEVICE_ID=asus`: 56 passed,
two failed from fixture device mismatch. No assertions were weakened. Correctly
configured rerun: **58 passed**, 17.88s. After final repairs: **103 focused passed**,
13.90s. An intermediate full suite passed 944 tests; the final suite includes the
three additional Calendar availability checks.

Final serial consolidated regression:

```text
passed = 947
failed = 0
skipped = 0
xfailed = 0
duration = 176.32 seconds
baseline retained = 935 / 935
new behavioral checks = 12
frozen source hashes = 11 / 11 match
```

No test processes overlapped. A14 implementation sources were not modified.

## Remaining limitations, ranked

1. Model admission/payload costs cause ordinary tasks to incur retries and long
   delays. Fit real requests to verified existing capacity without dropping user
   requirements or weakening safety; keep provider failures distinct from task bugs.
2. A14's generic claim-key collision rule blocks synthesis even after workers
   succeed. Review the subject/predicate contract in an explicitly scoped frozen-code
   repair; do not remove conflict checking or manufacture a successful graph.
3. Sensitive integration interpretation and compound UI require real human privacy
   approvals or a legitimately eligible local inference path. API reads alone do
   not complete conversational acceptance. Calendar currently reaches hard policy denial.
4. Native Workspace interaction/rendering is not accepted; Computer Use was stopped.
5. Execution-approval waits/stale approval cleanup and legacy active-task display
   can confuse progress. Do not mass-cancel old tasks as a display fix.
6. Fresh CPU receipt, completed file verification, completed compound computer use,
   live voice, Android and interrupted side-effect recovery remain unverified here.

Recommended next priority: resolve the documented frozen inference/claim-contract
blockers in one explicitly bounded repair, then repeat diverse ordinary-use checks
with available provider capacity and real human approvals. Do not start a new phase
or add integrations while basic completed-task usefulness remains inconsistent.

```ini
A11_LIVE_ACCEPTANCE = BLOCKED
PRE_A11_COMPOUND_VALIDATION = OPEN
A12_FOUNDATION = COMPLETE
A13 = COMPLETE
A14_IMPLEMENTATION = FROZEN
A14_LIVE_ACCEPTANCE = OPEN
A12_2_GENERATIVE_WORKSPACE = DEFERRED
GENERAL_END_TO_END_RELIABILITY = NOT_ESTABLISHED
```
