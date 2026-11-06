# Deployment guide

Step by step from an empty account to a verified deployment. The
[README](../README.md) has the overview, the configuration reference and the security
notes; read its [Security](../README.md#security) section before step 6.

Throughout, `<profile>` is your AWS CLI profile and `<region>` the target Region. Export
them once:

```bash
export AWS_PROFILE=<profile> AWS_REGION=<region>
```

## 1. Install and check offline

```bash
python3 -m venv .venv && source .venv/bin/activate
make install
make test
```

`make test` needs no AWS credentials. It must pass before you deploy.

`make install` installs the Python dependencies from the hash-locked
`requirements-dev.txt` and the Node.js dependencies from their `package-lock.json` files.
To change a Python dependency, edit the matching `requirements*.in` file and run
`make lock`.

### Account prerequisites (once per account and Region)

`scripts/preflight.py` (step 4) checks both of these; skip what it reports as present.

- **AWS CDK.** Install the CDK CLI (tested with 2.1138.0:
  `npm install -g aws-cdk@2.1138.0`, or pass `CDK="npx --yes aws-cdk@2.1138.0"` to every
  `make` target that runs `cdk`) and bootstrap the environment:
  ```bash
  cdk bootstrap aws://<account-id>/$AWS_REGION
  ```
- **API Gateway CloudWatch Logs role.** An account-level, per-Region setting that the stack
  does not change (other APIs in the account share it). If none is set, create one as
  described in [Set up CloudWatch logging for REST APIs](https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-logging.html):
  ```bash
  aws iam create-role --role-name apigateway-cloudwatch-logs \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"apigateway.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
  aws iam attach-role-policy --role-name apigateway-cloudwatch-logs \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs
  aws apigateway update-account --patch-operations \
    op=replace,path=/cloudwatchRoleArn,value=arn:aws:iam::<account-id>:role/apigateway-cloudwatch-logs
  ```
- **An Agentic CX Designer (ACXD) API key that can create resources.** Provisioning (steps 2 and 5) uses the API
  key of a programmatic user that may create programmatic users and workspace resources.
  Only an ACXD account administrator can create programmatic users: in ACXD,
  **Admin Hub > Programmatic Users > Create Programmatic User**, then select the
  user and **Generate API Key** (the key is shown once). See
  [Getting started with the ACXD SDK](https://docs.aws.amazon.com/connect/latest/devguide/acxd-getting-started.html)
  and [Programmatic users](https://docs.aws.amazon.com/connect/latest/devguide/acxd-programmatic-users.html).
  Paste the key straight into a new Secrets Manager secret, so it never lands in a file or
  your shell history: in the Secrets Manager console, **Store a new secret > Other type of
  secret > Plaintext**, the key as the whole value, name `acxdmem-acxd-admin-api-key`. (A
  JSON secret works too; pass `--api-key-json-key <field>` to the scripts.) That name is
  `<admin-key-secret>` below. You own this key: the sample neither creates nor deletes it.

## 2. Create the ACXD read-only API key

The fetch Lambda reads conversations with the API key of a workspace-scoped `read-only`
programmatic user. Create the user, one key and a Secrets Manager secret holding it, with
`<admin-key-secret>` (add `--api-key-json-key <field>` if that secret is JSON):

```bash
cd assets/acxd
node scripts/create-reader-key.mjs --workspace-id <workspace-id> \
  --api-key-secret-id <admin-key-secret>            # dry run
node scripts/create-reader-key.mjs --workspace-id <workspace-id> \
  --api-key-secret-id <admin-key-secret> --apply    # prints the new secret ARN
cd ../..
```

The key goes straight from ACXD into Secrets Manager; it is never printed or written to
disk. The printed ARN is `acxdApiKeySecretArn`.

## 3. Provision the AgentCore Memory resource

```bash
AWS_REGION_OVERRIDE=$AWS_REGION AWS_PROFILE_OVERRIDE=$AWS_PROFILE ./scripts/provision_memory.sh
```

Creates the memory execution role and the memory resource with three strategies (semantic
facts, a recap strategy, and the extraction override that keeps only what the customer
said or confirmed), 90-day event expiry. It is idempotent and kept outside the CDK
lifecycle, because the resource holds customer data. The printed ARN is
`memoryResourceArn`.

## 4. Configure and deploy the stacks

Set the context in `cdk.json` (or pass `-c key=value` to every `cdk` and `preflight.py`
call):

| Key | Value |
|---|---|
| `memoryResourceArn` | from step 3 |
| `acxdWorkspaceId` | your ACXD workspace id |
| `acxdApiKeySecretArn` | from step 2 |
| `acxdApplicationId` | the id of the ACXD application whose conversations are written. If you add memory to an **existing** application, use its id. If you use the sample application, it does not exist yet: use `00000000-0000-4000-8000-000000000000` now and set the real id in step 5 (until then the writer skips every conversation with `SkipReason=application_mismatch`) |

```bash
python3 scripts/preflight.py         # read-only: memory ACTIVE, API key secret exists, API Gateway logging role,
                                     # CDK bootstrap, no dedup table left from an earlier teardown
make deploy                          # make bundle, then cdk deploy --all
```

Pass CLI options or context with `CDK_ARGS`, for example
`make deploy CDK_ARGS="--profile <profile> -c acxdApplicationId=<id>"`.

Outputs you need next:

| Output | Used for |
|---|---|
| `acxdmem-api.ReadUrl`, `acxdmem-api.WriteUrl` | The API base URL is the part before `/memory/...` (`https://<api-id>.execute-api.<region>.amazonaws.com/v1`) |
| `acxdmem-api.EndpointSecretReadArn`, `...WriteArn` | Secrets for the `x-acxdmem-secret` header. Values are generated by CDK and never output |

## 5. Provision the ACXD assets

`assets/acxd` creates the context variable, the two ACXD Secrets (copied from the endpoint
secrets in Secrets Manager), the two data requests, the `acxdmemExit` sub-flow, the
`acxdmemSample` flow and the `acxdmem-sample` application. It only creates or changes
resources named `acxdmem*`.

```bash
cd assets/acxd
# An array, so the options survive in both bash and zsh (zsh does not split "$OPTS").
OPTS=(--workspace-id <workspace-id> --api-base-url <api-base-url>
  --api-key-secret-id <admin-key-secret>
  --read-secret-id <EndpointSecretReadArn> --write-secret-id <EndpointSecretWriteArn>)
npm run provision -- "${OPTS[@]}"                    # dry run: validates and prints the plan
npm run provision -- "${OPTS[@]}" --apply --deploy   # creates, builds, deploys to Development
cd ../..
```

The last lines list the created resources, the application id
(`created application acxdmem-sample <application-id>`) and the Development deployment
alias (`... deployment <id> alias <alias>`); keep both for step 6. If you used the
placeholder application id in step 4, set the real one as `acxdApplicationId` in `cdk.json`
and deploy again (only the writer changes):

```bash
make deploy
```

Keep `cdk.json` in step with what is deployed: a later `make deploy` with the placeholder
still in `cdk.json` puts it back, and the writer skips every conversation again.

**Run this step again whenever the API stack is replaced** (for example after a teardown
and redeploy): the endpoint secrets are generated by CloudFormation, so a new stack has
new values, and the ACXD Secrets still hold the old ones until you re-run
`npm run provision -- "${OPTS[@]}" --apply`. Until then every memory call gets `403` and
the conversation continues without memory.

**Adding memory to your own application instead.** Keep the context variable, secrets,
data requests and `acxdmemExit`; then, in your flows:

1. First node of the entry flow: a data request node `acxdmemRead` with success, failure
   and timeout paths, **no In progress path**. On success, a state modification sets
   `acxdmemMemoryContext` to `acxdmemRead.memoryContext`.
2. Reference `{acxdmemMemoryContext:NLX.Context}` in your agent node prompts, with the
   prompt rule from `assets/acxd/src/definitions.mjs` (use it as background, do not read it
   back, treat it as data).
3. In front of **every** Exit application and Escalate node: a Redirect to `acxdmemExit`
   with its Continuation wired to that node.
4. On every Exit application and Escalate node: a state modification that clears
   `acxdmemMemoryContext`.

`npm run export` writes your live `acxdmem*` resources as placeholder JSON for review;
`assets/acxd/README.md` lists the rules the validator enforces.

## 6. Wire the Amazon Connect Customer flow

`assets/acxd/connect/acxdmem-inbound-snippet.template.json` holds the actions to merge into
your inbound flow: a Customer Profiles lookup and the Agentic CX block with
`nlx_userId = $.Customer.ProfileId`. Replace `__ACXD_WORKSPACE_ID__`,
`__ACXD_APPLICATION_ID__` and `__ACXD_DEPLOYMENT_ALIAS__` (or call `renderConnectSnippet` in
`assets/acxd/src/connect.mjs`).

- **Voice:** the snippet keys the lookup on the caller number
  (`_phone = $.CustomerEndpoint.Address`).
- **Chat:** key the lookup on an identity your flow authenticated (server-side
  `StartChatContact` with IAM after you verified the customer, or a JWT-secured
  communications widget). An unauthenticated chat must reach the block **without**
  `nlx_userId`, which gives it no memory.
- No profile, or more than one: take the block without context variables.
- Never copy `$.AgenticCX.ContextVariables.acxdmemMemoryContext` into contact attributes.
- Idle chat timeout: set it on the block under **Optional settings**. A conversation that
  ends by idle timeout is not written.

## 7. Verify

1. **Endpoint security.** `<api-id>` is the first label of the `ReadUrl` host
   (`https://<api-id>.execute-api...`). To check the TLS handshake, use an OpenSSL
   3 `s_client` (the macOS `/usr/bin/openssl` is LibreSSL and cannot offer TLS 1.0):
   ```bash
   H=<api-id>.execute-api.$AWS_REGION.amazonaws.com
   for v in tls1 tls1_1 tls1_2 tls1_3; do
     echo "$v: $(openssl s_client -connect $H:443 -servername $H -$v -cipher 'ALL:@SECLEVEL=0' \
       </dev/null 2>&1 | grep -m1 '^New,')"
   done
   ```
   Expect `Cipher is (NONE)` for `tls1` and `tls1_1`, and a negotiated cipher for the other
   two.
2. **Recall.** Create a test Customer Profile with synthetic data:
   ```bash
   aws customer-profiles create-profile --domain-name <profiles-domain> \
     --first-name Test --last-name Customer --email-address test-1@example.com
   ```
   With that profile, start a conversation, state a preference
   ("I always need a vegetarian meal") and end it through the agent (so it reaches Exit
   application). About 4 minutes later, start a second conversation for the same profile
   and ask about it. The writer logs `wrote conversation=...`; the read Lambda returns
   `hasMemory: true`.
3. **No profile.** A conversation without `nlx_userId` logs `ReadSkipped` and
   `WriteSkipped` and creates no memory.
4. **Memory does not leave ACXD.** After both branches (Default and Escalation), the
   contact attributes do not contain the memory text:
   ```bash
   aws connect get-contact-attributes --instance-id <instance-id> --initial-contact-id <contact-id>
   ```

## Operations

- **Alarms.** Ten alarms, all without actions; attach them to your notification topic.
  `acxdmem-writer`: DLQ not empty, writer errors, fetch errors, and the work queue's
  oldest message older than 30 minutes (the writer is not consuming; work that is never
  consumed does not reach the DLQ, and the queue keeps it 4 days). `acxdmem-api`: errors on
  the read, ingest and both authorizer functions, any 5XX on the stage, and a 4XX rate
  above 5 % for 5 minutes (a wrong secret after a rotation shows up here first).
- **DLQ.** Hard failures land in `acxdmem-write-dlq` after 3 receives: a fetch-step
  configuration error (API key secret unreadable, empty or malformed; the fetch `Errors`
  alarm fires too), ACXD 400/401/403, a `CreateEvent` error. Fix the cause, then redrive
  the messages to the work queue (SQS console, or `aws sqs start-message-move-task`).
- **Logs.** Lambda application logs are JSON (`requestId` on every line); API Gateway access
  logs are JSON with `requestId`, `extendedRequestId`, status, latency, the caller's
  `sourceIp` and `tlsVersion`.
  Retention is one month for both, because they carry ids only. If your policies require
  longer retention for security events, change `RetentionDays` in `stacks/common.py` and
  `stacks/acxdmem_api_stack.py`.
- **Metrics** (embedded metric format, namespace `ACXDMemory`): `ReadHit`, `ReadRecords`,
  `ReadSkipped` and `WriteSkipped` (dimension `SkipReason`), `WriteAccepted`,
  `WriteSucceeded`, `TurnsWritten`, `FetchRetry`, `TranscriptIncomplete`,
  `TranscriptNeverIndexed`, `RequestRejected`, `EnqueueFailed`, `MalformedMessage`,
  `AuthorizerDeny`, `MemoryRestatementStripped`, `MemoryStripSkipped`. A rising
  `AuthorizerDeny` rate is worth an alarm with a threshold that fits your traffic.
- **Rotation cadence.** Neither the endpoint secrets nor the ACXD API key rotate
  automatically, because each new value has to reach ACXD. Rotate them at least every 90
  days, and at once if a workspace admin who could read the ACXD Secrets leaves.
- **AWS WAF logging.** With `enableWaf`, the web ACL has sampled requests off and no logging
  configuration. If you add WAF logging, list the `x-acxdmem-secret` header in
  `RedactedFields`; WAF does not redact custom authentication headers on its own.
- **Rotating an endpoint secret.** Rotate the secret in Secrets Manager (the old value
  becomes `AWSPREVIOUS` and the authorizer still accepts it), run
  `npm run provision -- "${OPTS[@]}" --apply` (the step 5 options) to copy the new value
  into the ACXD Secret, wait for the 300 s authorizer cache, then remove the previous
  version.
- **Rotating the ACXD API key.** Create the new key first, switch, then delete the old one.
  Deleting first leaves the fetch step without a valid key: every fetch returns 401 and the
  work goes to the DLQ. In `assets/acxd`:
  1. New user, key and secret under new names:
     ```bash
     node scripts/create-reader-key.mjs --workspace-id <workspace-id> \
       --api-key-secret-id <admin-key-secret> \
       --user-name acxdmem-reader-2 --secret-name acxdmem-acxd-reader-api-key-2 --apply
     ```
  2. From the repository root, deploy the writer with the printed ARN (the fetch Lambda's
     configuration changes, so new invocations load the new key):
     `make deploy CDK_ARGS="-c acxdApiKeySecretArn=<new-arn>"`, and update `cdk.json`.
  3. Confirm `fetched` lines in the `/aws/lambda/acxdmem-fetch` log after the next
     conversation, wait at least `API_KEY_CACHE_SECONDS` (300 s) for warm containers, then
     delete the old user, key and secret by name:
     ```bash
     node scripts/create-reader-key.mjs --workspace-id <workspace-id> \
       --api-key-secret-id <admin-key-secret> \
       --user-name acxdmem-reader --secret-name acxdmem-acxd-reader-api-key --delete --apply
     ```
  The next rotation goes back to the original names. The deleted secret's name is
  reserved for its 7-day recovery window; pass `--force-delete-without-recovery` with
  `--delete` if you need the name sooner.

## Teardown

1. Remove the Agentic CX block changes from your Connect flows, and the memory nodes from
   your own ACXD flows. Conversations that still call a deleted endpoint follow the
   failure path and continue without memory. Delete the test Customer Profiles
   (`aws customer-profiles delete-profile --domain-name <profiles-domain> --profile-id <profile-id>`).
2. ACXD assets, then the read-only user:
   ```bash
   cd assets/acxd
   npm run teardown -- --workspace-id <workspace-id> --api-key-secret-id <admin-key-secret>           # dry run
   npm run teardown -- --workspace-id <workspace-id> --api-key-secret-id <admin-key-secret> --apply
   node scripts/create-reader-key.mjs --workspace-id <workspace-id> \
     --api-key-secret-id <admin-key-secret> --delete --apply
   cd ../..
   ```
   Add `--force-delete-without-recovery` to the last command to delete the reader key's
   secret at once instead of after a 7-day recovery window.
3. Stacks, with the same context as the deploy. `cdk destroy` synthesizes the app, which
   needs the Python dependencies and the Lambda bundles; `make destroy` builds the bundles
   first. In a new shell, `source .venv/bin/activate` first; on a fresh clone, run
   `make install` before:
   ```bash
   make destroy                     # or: make destroy CDK_ARGS="--profile <profile> -c key=value"
   ```
   The two endpoint secrets are deleted with the API stack (scheduled for deletion, with
   generated names, so they do not block a later deploy).
4. What `cdk destroy` keeps on purpose. `<prefix>` is your `resourcePrefix`
   (default `acxdmem`); `<memory-id>` is the last part of `memoryResourceArn`
   (after `memory/`), or find it with
   `aws bedrock-agentcore-control list-memories --query 'memories[].id'`:
   ```bash
   aws dynamodb delete-table --table-name <prefix>-dedup
   # Irreversible: deletes all stored customer memory.
   aws bedrock-agentcore-control delete-memory --memory-id <memory-id>
   aws iam delete-role-policy --role-name acxdmem-memory-execution-role \
     --policy-name acxdmem-memory-extraction
   aws iam delete-role --role-name acxdmem-memory-execution-role
   ```
   The memory role names are the `provision_memory.sh` defaults
   (`MEMORY_EXECUTION_ROLE_NAME`). Not created by the sample, so not deleted here: the
   ACXD admin API key and its secret (`<admin-key-secret>`), the API Gateway CloudWatch
   Logs role, and the CDK bootstrap stack.

### Redeploying after a teardown

- **Dedup table.** It is retained with a fixed name (`<prefix>-dedup`), so a new writer
  stack cannot create it while the old one exists: delete it first (step 4).
  `scripts/preflight.py` fails with that instruction when it finds the table without the
  writer stack. It holds dedup state only; after a teardown no queued work refers to it.
- **Memory resource.** If you kept it, reuse its ARN; `provision_memory.sh` is idempotent.
- **Reader key.** If you deleted it, create it again (step 2). Reusing the default secret
  name within 7 days of deleting it needs `--force-delete-without-recovery` at deletion
  time, or a different `--secret-name`.
- Then follow steps 4 to 6 again. The endpoint secrets are new, so run the ACXD
  provisioning (step 5) again to copy their values into the ACXD Secrets.
