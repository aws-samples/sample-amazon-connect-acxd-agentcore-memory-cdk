# ACXD assets

The Agentic CX Designer (ACXD) side of the sample, as code. Design: [`docs/design.md`](../../docs/design.md) sections 3 to 6.

| Asset | Name | Purpose |
|---|---|---|
| Context variable | `acxdmemMemoryContext` | Memory text for the agent prompt. Sensitive, Allow frontend updates off |
| ACXD Secrets | `acxdmemReadSecret`, `acxdmemWriteSecret` | Per-route values for the `x-acxdmem-secret` header |
| Data request | `acxdmemRead` | `POST /memory/read` `{profileId}` → `{memoryContext, hasMemory}` |
| Data request | `acxdmemWrite` | `POST /memory/write` `{profileId, conversationId}` → `{accepted}`. IDs only (Option B) |
| Flow | `acxdmemExit` | Exit sub-flow: runs `acxdmemWrite`, returns to the caller |
| Flow | `acxdmemSample` | Sample entry flow: read, agent node, exit through `acxdmemExit` |
| Application | `acxdmem-sample` | Attaches both flows |
| Connect snippet | `connect/acxdmem-inbound-snippet.template.json` | Customer Profiles lookup, then the Agentic CX block with `nlx_userId = $.Customer.ProfileId` |

`src/definitions.mjs` is the source of truth. `exported/` is the same content rendered with placeholders (`__ACXDMEM_API_BASE_URL__`, `__FROM_AWS_SECRETS_MANAGER__`), so you can read or diff it without running anything. No real ids, URLs or secrets are committed; a test enforces that.

## How it fits together

```
acxdmemSample
  start → Data request acxdmemRead {profileId: {System.userId}}
            success → Basic "Store memory": set acxdmemMemoryContext = acxdmemRead.memoryContext
            failure / timeout → Basic "No memory"
        → Generative Journey (prompt references {acxdmemMemoryContext:NLX.Context})
            exit 0 conversationEnd, timeout, failure → Redirect acxdmemExit ─continuation→ Exit application
            exit 1 humanHandoff                     → Redirect acxdmemExit ─continuation→ Escalate
          Exit application and Escalate clear acxdmemMemoryContext

acxdmemExit
  start → Data request acxdmemWrite {profileId: {System.userId}, conversationId: {System.conversationId}}
            success / failure / timeout → Redirect {System.lastFlow:NLX.System}, clears acxdmemMemoryContext
```

The wired Continuation on the calling Redirect node is the return address; the sub-flow's redirect to `{System.lastFlow:NLX.System}` lands there. To add memory to your own application, attach `acxdmemExit`, put a Redirect to it with its Continuation wired in front of every Exit application and Escalate node, and add the `acxdmemRead` node plus the state modification at the start of your entry flow.

Rules the validator enforces:

- Neither memory node has an In progress path.
- Success, failure and timeout all continue, so a memory outage never strands the customer.
- Send context is off on both requests.
- Request models are closed (`additionalProperties: false`), so only `profileId` and `conversationId` are sent.
- The `x-acxdmem-secret` header is a sensitive reference to an ACXD Secret, never a literal.
- The exit sub-flow clears `acxdmemMemoryContext` on its return, and every Exit application and Escalate node clears it again, so the memory text is not handed back to Connect.
- The Connect snippet takes `nlx_userId` only from `$.Customer.ProfileId` and never copies `$.AgenticCX.ContextVariables` into the contact (threat model T9, T10).

## Commands

Node.js 20 or later. Install with `npm ci` in this directory.

| Command | Network | What it does |
|---|---|---|
| `npm run verify` | none | Type-checks against the SDK types, runs the tests, checks `exported/` for drift |
| `npm run render` | none | Re-renders `exported/` after a change to `src/definitions.mjs` |
| `npm run provision -- <options>` | ACXD, Secrets Manager | Dry run unless `--apply`; `--deploy` also builds and deploys to Development |
| `npm run export -- <options>` | ACXD (read-only) | Writes the live `acxdmem*` resources as placeholder JSON to `build/export/` |
| `npm run teardown -- <options>` | ACXD | Dry run unless `--apply`; deletes only the `acxdmem*` resources |

Provisioning options:

```
--workspace-id <uuid>          ACXD workspace
--api-base-url https://...     CDK output: API Gateway stage URL
--dev-api-base-url https://... optional, Development environment URL (defaults to --api-base-url)
--api-key-secret-id <id>       Secrets Manager secret holding the ACXD API key
--api-key-json-key <key>       optional, if that secret is a JSON object
--read-secret-id <id>          Secrets Manager secret for the read route (CDK output)
--write-secret-id <id>         Secrets Manager secret for the write route (CDK output)
--region eu-central-1          default
```

The ACXD API key is read from Secrets Manager with your AWS credentials, never from an environment variable or argument. Provisioning needs a programmatic user that can create resources; the writer Lambda uses a separate `read-only` user (design.md section 3.2, T6). The script prints every resource it creates; record them for teardown.

After provisioning, apply the Connect snippet: replace `__ACXD_WORKSPACE_ID__`, `__ACXD_APPLICATION_ID__` and `__ACXD_DEPLOYMENT_ALIAS__` (or call `renderConnectSnippet` in `src/connect.mjs`) and merge the actions into your inbound flow. The lookup key is the caller's phone number (`_phone` = `$.CustomerEndpoint.Address`), which Connect provides on voice. For chat, key the lookup on an identity the flow has authenticated; an unauthenticated chat must take the no-memory path (design.md section 5, T9). Do not copy `$.AgenticCX.ContextVariables.acxdmemMemoryContext` into contact attributes.
