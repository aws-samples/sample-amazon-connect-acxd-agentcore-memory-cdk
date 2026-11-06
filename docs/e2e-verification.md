# End-to-end live verification

Deployed stack, ACXD assets and new Amazon Connect Customer test flows, verified live on
2026-10-08/09 in a test account in eu-central-1 (one Amazon Connect Customer instance, one
ACXD workspace). Chat tests used a synthetic Customer Profile; the voice test used a tester's
own profile, resolved by caller number.

Identifiers in this public copy are redacted: account `111122223333`, API `abcdef1234`,
memory `acxdmem_memory-AbCdEf1234`. Contacts are named C1-C6 (chat) and V1-V3 (voice);
their contact ids, conversation ids, memory record ids and raw log captures are kept with
the authors and are not published. The tools that produced the evidence are in `e2e/` and
`scripts/verify_live_security.py`.

> **Scope of this evidence.** Everything below was recorded against the stack as deployed
> on 2026-10-08/09, **before** the TLS change (enhanced security policy
> `SecurityPolicy_TLS13_1_2_PFS_PQ_2025_09` with `STRICT` endpoint access mode), the JSON
> Lambda log format, the alarms, and the fixes from the code review
> ([`security-posture.md`](security-posture.md) sections 3 and 3a). The deployed API still
> used the regional default `TLS_1_0`, so the security run below has 22 checks, not the 24
> the script runs today. The current code was then verified from a clean clone: see
> [Final verification of the published code](#final-verification-of-the-published-code).

## Final verification of the published code

On 2026-10-09 the published commit was cloned into an empty directory and deployed twice
into the same test account, following only the README and
[`deployment-guide.md`](deployment-guide.md) (new virtual environment, no cached build
output, every earlier `acxdmem` resource deleted first). The first run found documentation
gaps, which were fixed before the second run; both runs passed the checks below, and both
were torn down with the guide's teardown section.

| Check | Result (second run) |
|---|---|
| `make install`, `make test` | Pass: 214 pytest, fetch-step and ACXD asset tests, `check_public` 0 findings |
| Preflight, deploy, ACXD `provision --apply --deploy`, writer redeploy with the real application id | Pass. Live build status keyword: `BUILT` |
| `scripts/verify_live_security.py` | **Pass, 24/24**, incl. `SecurityPolicy_TLS13_1_2_PFS_PQ_2025_09` and endpoint access mode `STRICT` |
| TLS probe (OpenSSL 3 `s_client`) | TLS 1.0 and 1.1 rejected (no cipher negotiated); TLS 1.2 and 1.3 accepted. ACXD's own calls arrived with `tlsVersion` `TLSv1.3` in the access log |
| Chat: contact 1 states a fact, contact 2 (about 4 minutes later) recalls it | **Pass**: "Based on your preferences, request an aisle seat and a vegetarian meal!" Read `hasMemory=True records=4` |
| No profile | Pass: `ReadSkipped` and `WriteSkipped` with `SkipReason=malformed_profile_id`, nothing enqueued |
| Memory variable after exit (`acxdmemProbeMemory`) | `""` on every contact |
| Metrics with JSON Lambda logs | Pass: `ACXDMemory` `ReadHit`, `ReadRecords`, `ReadSkipped`, `WriteAccepted`, `WriteSkipped`, `WriteSucceeded`, `TurnsWritten`, `AuthorizerDeny` emitted; application log lines are JSON with `requestId` |
| Alarms | Pass: all 10 present (incl. `<prefix>-write-queue-age`, 1800 s), no actions, all `OK` after the run; DLQ empty |
| Teardown | Pass: no `acxdmem` stack, function, API, queue, table, alarm, log group, secret, memory, IAM role or ACXD asset left (read-only sweep afterwards) |

Not re-run in this verification: voice, escalation, idle timeout and the Error branch (the
earlier results below stand for them).

## Result summary

| Check | Result |
|---|---|
| Deploy (preflight, `acxdmem-writer`, `acxdmem-api`, ACXD `provision --apply --deploy`) | Pass, after bugs B1-B2 below |
| Security of the deployed resources | Pass, 22/22 checks (`scripts/verify_live_security.py`, version before the two TLS checks); 24/24 on the published code (see above) |
| Chat: contact 1 states a fact, contact 2 recalls it | **Pass** |
| No profileId: skip, no memory | Pass |
| Duplicate exit call: dedup | Pass |
| Exit and Escalate reach the write and clear `memoryContext` | Pass |
| Idle chat timeout and Error reach the write | **No**, as accepted in design §7 (kept for v1). `memoryContext` is empty on both branches |
| G14: no memory in contact attributes or contact record | Pass, 6/6 contacts |
| Voice contact | Pass: write on exit, recall on the next call |
| Task 8 assumptions 1-5 | All hold, no fallback needed |
| Task 7 follow-ups | Timestamp format confirmed; cold start fixed (B3); stripping not exercised |

## Security (deployed state)

`python3 scripts/verify_live_security.py --api-id <api-id> --profile <aws-profile>`, output
in full (ids redacted):

```
[PASS] found 6 acxdmem functions
[PASS] acxdmem-authorizer-read: no Function URL
[PASS] acxdmem-authorizer-read: statement … principal {'Service': 'apigateway.amazonaws.com'} SourceArn arn:aws:execute-api:eu-central-1:111122223333:abcdef1234/authorizers/<read-auth>
[PASS] acxdmem-authorizer-write: no Function URL
[PASS] acxdmem-authorizer-write: statement … SourceArn …:abcdef1234/authorizers/<write-auth>
[PASS] acxdmem-fetch: no Function URL          (no resource policy)
[PASS] acxdmem-ingest: no Function URL
[PASS] acxdmem-ingest: statement … SourceArn …:abcdef1234/v1/POST/memory/write
[PASS] acxdmem-read: no Function URL
[PASS] acxdmem-read: statement … SourceArn …:abcdef1234/v1/POST/memory/read
[PASS] acxdmem-writer: no Function URL         (no resource policy)
[PASS] POST /memory/read: authorization CUSTOM <read-auth>
[PASS] POST /memory/write: authorization CUSTOM <write-auth>
[PASS] one authorizer per route
[PASS] stage throttling 50.0 rps / burst 100
[PASS] data trace off
[PASS] /memory/read without secret header -> 401 (want 401)
[PASS] /memory/read with a wrong secret -> 403 (want 403)
[PASS] /memory/read with the write route's secret -> 403 (want 403)
[PASS] /memory/write without secret header -> 401 (want 401)
[PASS] /memory/write with a wrong secret -> 403 (want 403)
[PASS] /memory/write with the read route's secret -> 403 (want 403)
all checks passed
```

Not exercised: the authorizer's own route-mismatch deny (needs a request for one route
evaluated by the other route's authorizer, which API Gateway never sends; unit-tested). The
writer's API key belongs to a new workspace-scoped `read-only` programmatic user
(`acxdmem-reader`, created by `assets/acxd/scripts/create-reader-key.mjs`, key straight into
Secrets Manager).

## Identity in the test flows (T9)

`e2e/connect_flow.py` creates `acxdmem-e2e-inbound` (the shipped shape) and
`acxdmem-e2e-probe` (same, plus a test-only copy of the returned
`$.AgenticCX.ContextVariables.acxdmemMemoryContext` into contact attribute
`acxdmemProbeMemory`, to prove it comes back empty). Both branch on `$.Channel`:

- voice: Customer Profiles lookup on `_phone = $.CustomerEndpoint.Address` (caller number);
- chat: lookup on `_email = $.Attributes.acxdmemVerifiedEmail`. The test driver
  (`e2e/chat.mjs`) plays the trusted backend that verified the customer and then calls
  `StartChatContact` with IAM. A production chat must get this value from an authenticated
  source (server-side start or a JWT-secured widget), never from an unauthenticated client.
- `nlx_userId` is only ever `$.Customer.ProfileId`; no profile, or more than one, takes the
  block without context variables.

## Chat: write and recall

| # | Contact = conversationId | Flow, branch | What happened |
|---|---|---|---|
| C1 | C1 | probe, Default | Customer: "I always need a vegetarian meal on flights and I prefer an aisle seat", then "goodbye". Read 200 (empty), exit write 202 |
| C2 | C2 | probe, Default | Customer: "What seat and meal should I request for me?" Agent: **"Based on your preferences, request an aisle seat and a vegetarian meal when you book your flight to Lisbon."** Read 200, 240 bytes |

C1 through the writer (CloudWatch, `acxdmem-writer`, `acxdmem-fetch`):

```
22:56:27.94Z  ACXD DataRequestsReturned acxdmemWrite 202 (enqueued, DelaySeconds 120)
22:58:31Z     fetch 200, 5 messages -> requeue phase=confirm delay=30s
22:59:02Z     wrote conversation=C1 eventId=0000001791500187977#… turns=5
23:00:06Z     records extracted (about 64 s after CreateEvent)
```

AgentCore Memory `acxdmem_memory-AbCdEf1234`, actor = the synthetic profile, session = C1: one
event, `eventTimestamp` 2026-10-08T22:56:27.977Z (= last message), 5 turns
(ASSISTANT/USER alternating, greeting first). Records after C1:

| Namespace | Text |
|---|---|
| semantic | The user always needs a vegetarian meal on flights. |
| semantic | The user prefers an aisle seat on flights. |
| recap | The customer prefers vegetarian meals on flights. |
| recap | The customer prefers aisle seats on flights. |

Dedup rows (`acxdmem-dedup`): `acxd#C1` with `lastWatermark 1791500187977`, segment
`acxd#C1#1791500187977` `status written, attempt 1`. C2 was written the same way
(`0000001791500588105#…`, 5 turns).

**Feedback loop (D2), one sample:** in C2 the agent restated the stored preferences. The
extraction for C2 added one record ("The user is booking a flight to Lisbon next month.") and
did not re-extract the restated aisle/vegetarian facts. Consistent with the `appendToPrompt`
override working (n=1, not a tuning result). Stripping (`enableStripping`) stayed off.

## No profileId

C3, flow `acxdmem-e2e-inbound` without
`acxdmemVerifiedEmail`, so the lookup errors and the block runs without `nlx_userId`. Customer
said "I always need a vegetarian meal". Read: `ReadSkipped SkipReason=malformed_profile_id`,
`200 {memoryContext:"", hasMemory:false}`. Write: `WriteSkipped
SkipReason=malformed_profile_id`, `202 {accepted:false}`, nothing enqueued. No memory actor
was created for it.

Observation: without `nlx_userId`, `{System.userId}` is not empty; ACXD fills in its own
value, which fails `is_valid_profile_id`. The skip reason is therefore `malformed_profile_id`,
not `no_profile_id`; the behaviour is the same.

## Duplicate exit call

Two concurrent `POST /memory/write` for C1 (`{profileId, conversationId: C1}`) at
23:17:00Z, with the write secret: both `202 {accepted:true}`. Both messages went through
fetch and confirm, then:

```
skip conversation=C1 reason=no_new_messages     (x2, WriteSkipped)
```

`ListEvents` for the session still returns 1 event. The conversation row's `lastWatermark`
filter caught both copies before a segment claim. The segment-claim race itself (two copies
with new messages) is covered by unit tests only.

## Terminal branches and `memoryContext`

| # | Contact | Branch (`acxdmemBranch`) | Write | `acxdmemProbeMemory` |
|---|---|---|---|---|
| C1 | C1 | Default | Yes (exit sub-flow, `ApplicationExit type terminate reason completed`) | `""` |
| C4 | C4 | Escalation ("I want to speak to a human agent") | Yes, `ApplicationExit type escalate reason escalation`, written `0000001791501154066#…` (3 turns) | `""` |
| C5 | C5 | IdleChatTimeout, fired 61.5 s after the last bot message | **No**: no ACXD node ran after the timeout, no write request | `""` |
| C6 | C6 | Error (flow `acxdmem-e2e-error`, unknown alias) | **No**: no ACXD conversation exists | `""` |

On C1, C2 and the voice exits, the ACXD logs show the clear twice: `StateValuesEjected
acxdmemMemoryContext ***` on the sub-flow's "Return to caller" redirect, then again on the
Exit application node.

Idle timeout parameter: Connect validates `IdleChatTimeoutSeconds` on the Agentic CX block
as an **integer** (a string is rejected with "Invalid Action property value"). Unknown keys,
including the spike's `InputTimeLimitSeconds`, are accepted and ignored, which is why the
spike's idle branch never fired. Not in public docs.

The idle and error gap is the one design §7 accepts: a conversation that ends by idle,
error or hang-up is not written. We expect a customer closing the chat to behave the same
but did not test it in this run. Earlier memory is unaffected and is still read next time.
The gap is kept for v1.

## G14: memory never in the contact

`python3 e2e/check_contact_memory.py` searched `DescribeContact` and `GetContactAttributes` of C1-C6
for the variable name and for every memory record text of the profile (8 needles):
6/6 PASS. The probe attribute was `""` on every branch.

The instance has no CTR stream configured (`list-instance-storage-configs
CONTACT_TRACE_RECORDS` is empty), so the contact record from `DescribeContact` and the
contact attributes (which the CTR carries) were checked instead of a streamed CTR.

## Voice

The instance's existing number was associated with another flow, so a new
test number was claimed and associated with `acxdmem-e2e-inbound`. The
caller's own Customer Profile was resolved by caller number (`_phone`). Engine
`AMAZON_AGENTIC_VOICE`. The ACXD logs of V1-V3 carry `isVoiceChannel: true`.

| # | Contact | What happened |
|---|---|---|
| V1 | V1 | Read 200 (empty, 2710 ms cold). Caller stated preferences and said goodbye: exit sub-flow, write 202, `ApplicationExit terminate completed`, clear logged. Written `0000001791524849942#…` (5 turns); records extracted 07:51:07 CEST |
| V2 | V2 | Read at 07:50:48 CEST, 19 s **before** extraction finished, so empty. Test timing, not a defect (exit → +120 s → +30 s → about 60 s extraction ≈ 4 min). Caller hung up: no write (expected) |
| V3 | V3 | Read 200, 328 bytes (hasMemory), 1471 ms. Agent: **"Based on your preferences, I can arrange a vegetarian …"**. Caller hung up before an exit: no write (expected) |

Voice observations: ASR turned "an aisle seat" into "an aleseed" (V1) and "meal" into "news"
(V3). Extraction then kept only the vegetarian meal as a clean fact and recorded the rest as
unclear in the recap. Hang-up ends without a write, as design §7 expected.

## Task 8 assumptions

| # | Assumption | Live result |
|---|---|---|
| 1 | Exit sub-flow returns via `{System.lastFlow:NLX.System}` | **Holds.** Chat and voice: `Redirection "Write memory, then exit"` → sub-flow write → `Redirection "Return to caller"` → the caller's wired Continuation (Exit application or Escalate). No inline fallback needed |
| 2 | Runtime terminal is `terminate` though the model enum says `end` | **Holds.** `CreateFlow`/`UpdateFlow` accepted `terminate`; runtime logged `NodeTraversal`/`ApplicationExit type terminate reason completed` |
| 3 | `RedirectConfig {type: flow, flowId}` with a static and a dynamic target | **Holds.** Both redirects ran. `GetFlow` returns the form as written (`{type: "flow", flowId: "{System.lastFlow:NLX.System}"}`); no intent form is visible through the SDK |
| 4 | ACXD accepts `additionalProperties: false` in a `requestSchema` | **Holds.** Create accepted it, `GetDataRequest` returns it, and every live request passed the API Gateway model that rejects extra fields (200/202, `validationError "-"`), so ACXD sent only the declared fields |
| 5 | Voice | **Holds**, see Voice |

Also confirmed: state modifications on a Redirect node run (the sub-flow clear is logged on
the redirect node), and `memoryContext` cleared on the terminal is empty in
`$.AgenticCX.ContextVariables` when the terminal is reached through the sub-flow return.

## Task 7 follow-ups

- **`GetConversation` timestamps / `CreateEvent` timestamp:** message `timestamp` is an ISO
  8601 UTC string with milliseconds (`2026-10-08T22:56:27.977Z`, JS type `string`, from
  `e2e/conversation.mjs`). The writer parsed it; watermark `1791500187977` (epoch ms)
  and `eventTimestamp` 2026-10-08T22:56:27.977Z both equal the last message.
- **Indexing:** `GetConversation` returned 200 at the first fetch (+120 s) and an unchanged
  result at the +30 s confirm for all four written conversations (C1, C2, C4, V1); no 404
  re-queue was needed. Exit to `CreateEvent` about 150 s; records about 60 s later.
- **Read cold start vs. the 4 s Lambda / 5 s ACXD budget (bug B3):** first deploy (256 MB):
  ACXD `responseTime` for `acxdmemRead` 4454 ms (C1) and 4078 ms (C2) with authorizer and
  read both cold (authorizer ~1.9 s, read ~2.4 s incl. 0.73 s init), 0.5 s from the timeout
  path. After the fix (1024 MB, client and secret prefetched at init): 2323 ms double cold
  (C3), 1471 ms and 2710 ms on the voice calls after idle periods (V3, V1), 264-345 ms warm. Write route: 3852 ms cold
  before, 2167 ms after, 80-95 ms warm.
- **Stripping threshold:** not exercised (`enableStripping` off, as designed). See the D2
  sample above.

## Bugs found and fixed

| # | Bug | Fix |
|---|---|---|
| B1 | `provision_memory.sh` failed on the first run: `CreateMemory` "Please provide a role with a valid trust policy" right after creating the role (IAM propagation) | Retry up to 5 times, 10 s apart, only on that error |
| B2 | `preflight.py` read `cdk.json` only, so it could not check values passed with `cdk deploy -c` | `-c key=value` overrides, same as the CDK CLI; unit-tested |
| B3 | Double cold start of the read route took 4.1-4.5 s against the 5 s ACXD timeout | Customer-facing Lambdas at 1024 MB; read, ingest and authorizer create their client (authorizer: also load the secret) during init; CDK assertion and unit test |
| B4 | The "Store memory" greeting said "Hi, welcome back" to first-time customers (the read succeeds with `hasMemory:false`) | Neutral greeting on both paths |
| B5 | Spike idle parameter `InputTimeLimitSeconds` is silently ignored | Test flows use `IdleChatTimeoutSeconds` (integer); documented in `assets/acxd/README.md` |

New tooling: `assets/acxd/scripts/create-reader-key.mjs` (read-only programmatic user + key
into Secrets Manager, with `--delete`), `scripts/verify_live_security.py`, `e2e/`
(`connect_flow.py`, `chat.mjs`, `voice_call.py`, `conversation.mjs`, `acxd_logs.mjs`,
`check_contact_memory.py`).

## Not verified

- On the published code (TLS policy, JSON logs, alarms, code-review fixes): voice,
  escalation, idle timeout and the Error branch were not re-run; chat recall, no-profile,
  security, metrics and alarms were (see the final verification above).
- Voice greeting latency as heard by the caller (only ACXD-side times are logged).
- Segment-claim race with two copies that both carry new messages (unit tests only).
- Stripping and its 8-word threshold (`enableStripping` never on).
- A streamed CTR (no CTR stream on the instance); the contact record was checked instead.
- Re-entry (second Agentic CX block in the same contact, spike S10) was not re-run.
- Load: concurrency, SDK 429 handling and the DLQ path were not driven live.
