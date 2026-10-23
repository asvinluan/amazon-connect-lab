# Amazon Connect reference lab

A small, runnable Amazon Connect contact centre defined entirely in Terraform, with a Python Lambda on the voice path, a credential-broker API behind API Gateway for web chat, CloudWatch alarms tied to failures this build can actually produce, and a GitHub Actions pipeline that checks every push and pull request, and plans when an AWS role is configured.

> **This is lab work.** It is built in my own AWS account to learn and demonstrate the services, not extracted from any employer or client system. There is no client data, no proprietary configuration and no real customer information anywhere in this repository. Seed data uses fictional numbers.

Built by Anthony Vinluan — Melbourne, Australia.

\---

## Why this repository exists

I have twenty-two years in telecom and contact centres, most of it on premises-based and hosted platforms. This repository is where I rebuild the same problems on AWS: routing, caller identification, integration, and knowing something broke before a customer tells you.

It is deliberately small. Every file here is one I can explain line by line, which is worth more than a large repository I would have to re-read.

\---

## Architecture

```
  VOICE PATH                                WEB CHAT PATH
  ──────────                                ─────────────

  Caller                                    Browser
    │                                          │
    │ PSTN                                     │ HTTPS  POST /chat
    ▼                                          ▼
  ┌─────────────────────┐                  ┌──────────────────┐
  │  Phone number       │                  │  API Gateway     │
  │  (claimed manually) │                  │  HTTP API        │
  └─────────┬───────────┘                  │  throttled 10rps │
            │                              └────────┬─────────┘
            ▼                                       │
  ┌─────────────────────┐                           ▼
  │  Contact flow       │                  ┌──────────────────┐
  │  lab-main-flow      │                  │  chat\_broker     │
  │                     │                  │  Lambda          │
  │  set logging        │                  │                  │
  │  set voice          │                  │  holds the IAM   │
  │  invoke Lambda ─────┼───┐              │  role, calls     │
  │  set attributes     │   │              │  StartChatContact│
  │  greet by name      │   │              └────────┬─────────┘
  │  set working queue  │   │                       │
  │  transfer to queue  │   │              ParticipantToken only
  └─────────┬───────────┘   │                       │
            │               │                       ▼
            ▼               │                  Browser talks to
  ┌─────────────────────┐   │                  connectparticipant
  │  lab-queue-general  │   │                  with that token
  │  max\_contacts 5     │   │                  (no AWS credentials
  └─────────┬───────────┘   │                   ever reach the page)
            │               │
            ▼               ▼
  ┌─────────────────────┐ ┌──────────────────┐
  │  lab-rp-general     │ │  caller\_lookup   │
  │  routing profile    │ │  Lambda          │
  │  priority 1 delay 0 │ │  5s timeout      │
  └─────────┬───────────┘ └────────┬─────────┘
            │                      │
            ▼                      ▼
         Agent CCP          ┌──────────────┐
                            │  DynamoDB    │
                            │  customers   │
                            └──────────────┘

  OBSERVABILITY  ── CloudWatch ──────────────────────────────────
    ContactFlowErrors · ContactFlowFatalErrors ·
    MisconfiguredPhoneNumbers · Lambda p99 duration · Lambda errors
                              │
                              ▼
                          SNS topic → email
```

**API Gateway is not on the voice path.** Connect invokes a flow Lambda directly — that is what the `aws\_connect\_lambda\_function\_association` resource is for. API Gateway exists because `StartChatContact` is a SigV4-signed IAM call that a browser cannot make without holding AWS credentials. The Lambda behind it is a credential broker: it holds the role, calls Connect, and hands back only a short-lived participant token.

Getting that distinction wrong is common, so it is called out in the comments of both `apigateway.tf` and `lambda/chat\_broker/handler.py`.

\---

## What's in here

|Path|What it is|
|-|-|
|`terraform/main.tf`|Connect instance, hours of operation, queue, routing profile, contact flow|
|`terraform/lambda.tf`|Both Lambda functions, their IAM roles, scoped to one action and one resource each|
|`terraform/apigateway.tf`|HTTP API, throttling, access logs, the Lambda invoke permission|
|`terraform/observability.tf`|Five alarms, an SNS topic and a dashboard|
|`terraform/data\_store.tf`|DynamoDB table and fictional seed data|
|`terraform/flows/main.json.tftpl`|The contact flow as a template, with ARNs injected at apply time|
|`lambda/caller\_lookup/`|Invoked by the flow. Timeout budget, graceful degradation|
|`lambda/chat\_broker/`|Behind API Gateway. Returns a token, never credentials|
|`lambda/salesforce\_lookup/`|Looks the caller up in a Salesforce Contact (REST, OAuth 2.0). Invoked by `lab-salesforce-flow`|
|`terraform/salesforce.tf`|That Lambda, its log group and a role scoped to one action on one secret|
|`terraform/salesforce\_flow.tf`, `terraform/flows/salesforce.json.tftpl`|Second contact flow that invokes the Salesforce lookup, copies the name into contact attributes and greets the caller|
|`lambda/recording\_router/`|Reacts to a file landing in S3 (event-driven). Idempotent writes, retries, dead-letter queue|
|`terraform/events.tf`|The encrypted bucket, DynamoDB table, S3 trigger, dead-letter queue and its alarm|
|`lambda/ai\_assist/`|Guarded generative answer: Bedrock Converse with a numbered guardrail version, a grounded prompt, safe fallback|
|`terraform/ai\_assist.tf`|The guardrail and its version, the Lambda, model invocation logging, three log-based metrics and alarms|
|`evals/` and `scripts/run\_ai\_eval.py`|15 prompts with expected outcomes, and the runner that scores the deployed function|
|`knowledge/`|Five fictional help documents for the Amazon Q in Connect knowledge base (built by hand in the console, not Terraform)|
|`tests/`|Unit tests pinning the behaviour that keeps the Lambdas safe (7 `caller_lookup`, 20 Salesforce lookup, 15 recording router, 32 AI assist and its evaluation set) and 18 structural checks on the contact flow JSON|
|`scripts/audit\_flow.py`|Fails the build on dangling branches or broken references in flow JSON|
|`scripts/export\_flows.sh`|Exports flows to disk and reports unpublished designer edits|
|`.github/workflows/`|Terraform checks and plan; Python lint and test|

\---

## Five things in here worth a look

**1. The Lambda timeout budget is the design, not an afterthought.**
Connect abandons a flow Lambda invocation at **8 seconds** and takes the Error branch, regardless of the function's own timeout. So `caller\_lookup` sets an explicit 2-second connect and read timeout on its DynamoDB client with a single retry — boto3's defaults would allow roughly 60 seconds of connect time plus retries and guarantee the Error branch. A Terraform variable validation block refuses any `lambda\_timeout\_seconds` of 8 or more.

**2. An unknown caller is not an error.**
The handler never raises for an expected condition. A caller with no record, a withheld caller ID, a DynamoDB timeout and a throttle each return a usable response with a `status` field. Raising would send the flow down its Error branch, and the Error branch is for "something broke", not "we looked and there was nobody". There is a test for each case.

**3. Everything returned to Connect is a string.**
Contact attributes are string-valued, flat, and capped at 32 KB. The handler coerces on the way out and checks the encoded size; `test\_caller\_lookup.py` asserts it on every response, because a refactor that returns a number is the kind of change that looks harmless and breaks a flow in production.

**4. The alarms map to real failures.**
`MisconfiguredPhoneNumbers` counts calls that failed because the number has no flow attached — what a caller hears as *"the number you have called is unavailable."* I hit exactly that failure building a larger lab, and nothing in the Connect console surfaces it. The Lambda alarm is on **p99 duration** rather than the `Errors` metric, because errors tell you it already broke and p99 tells you it is about to.

**5. Git is the flow version control, because Connect's isn't.**
`CreateContactFlowVersion` is documented as supporting only flows of type `CAMPAIGN`, so there is no API version history for ordinary flows and no rollback to "version 3". The repository holding exported flow JSON *is* the rollback mechanism. `scripts/export\_flows.sh` pulls flows to disk and additionally diffs published content against the `$SAVED` alias, which catches edits somebody left unpublished in the designer — a silent failure that takes a flow down with nothing in the console to explain it.

\---

## Salesforce caller lookup

A second take on the caller lookup: instead of a DynamoDB table, the Lambda asks a **Salesforce Developer Edition** org who is calling. It is a separate function (`lambda/salesforce_lookup/`). It is invoked by its own flow, `lab-salesforce-flow` (`terraform/salesforce_flow.tf`), which copies the name into contact attributes, greets the caller by name and queues them. `lab-main-flow` and the voice path above are unchanged. The function can also be tested on its own from the Lambda console.

```text
event (caller number) -> Lambda -> Secrets Manager (client id, secret, org URL)
                                -> Salesforce /services/oauth2/token   (client-credentials flow)
                                -> Salesforce /services/data/v63.0/query?q=SELECT ... FROM Contact WHERE Phone ...
                       <- flat map of strings: status, callerName, company, reason
```

**What it does and why**

* **OAuth 2.0 client-credentials flow** with an External Client App and a Run-As user, so no person has to log in for a phone call. The token is cached for ten minutes and refreshed once if Salesforce answers 401.
* **The `+` in the phone number is sent as `%2B`.** In a URL query string a raw `+` means "space", so an unencoded E.164 number matches nobody — and fails quietly, which is worse than failing loudly. A test pins this.
* **A 4-second total budget, never more than 2 seconds per call.** Connect abandons a flow Lambda at 8 seconds. Every outbound call gets what is left of the budget, and the Lambda timeout is the hard backstop.
* **Failures degrade, they do not raise.** Bad credentials, a timeout, an outage and a malformed secret each return `status` plus a `reason` code (`token_http_400`, `query_timeout`, `secret_invalid_json` ...), and the caller is still greeted as "there". An unknown number is `not_found`, not an error.
* **The client secret never leaves AWS or the log.** It lives in Secrets Manager, is read with a role that can call one action on one secret, is only ever sent to a host ending in `salesforce.com`, and is asserted absent from logs and responses by a test.
* **Standard library only.** `urllib` for the HTTP, `boto3` from the Lambda runtime. No dependencies to patch.

**Set it up** (the secret is created by hand so the values never enter this repository or the Terraform state):

1. In Salesforce, create an External Client App with the OAuth 2.0 client-credentials flow enabled and a Run-As user.
2. In Secrets Manager, create a plaintext secret named `lab/salesforce` containing valid JSON:
   `{"client_id": "...", "client_secret": "...", "instance_url": "https://<your-org>.my.salesforce.com"}`
3. `terraform apply`, then test from the Lambda console with `{"Details": {"Parameters": {"callerNumber": "+61..."}}}`.

Salesforce phone fields are free text, so the match is exact: the lookup tries the E.164 form and the Australian national form (`04...`), and a Contact stored as `(02) 1234 5678` will not match.

---

## Event-driven recording router

The first two Lambdas are called by something (Connect, or API Gateway). This one is not called by anything: a file lands in S3 and Lambda reacts. It is a separate function, not wired into the contact flow, built to show the event-driven plumbing rather than any one business rule.

```text
S3 bucket (incoming/)  ->  Lambda recording_router  ->  DynamoDB (one row per file)
                                  |
                                  +-- fails after 2 retries -> SQS dead-letter queue
                                                              -> CloudWatch alarm -> SNS email
```

**What it does and why**

* **S3 `ObjectCreated` event triggers Lambda.** Uploading to `incoming/` is the only thing that starts it. The function records the arrival in DynamoDB. In a real contact centre the same trigger would start transcription, redaction or archiving; here the plumbing is the point.
* **Idempotent writes.** S3 delivers events at least once, so the same event can arrive twice. Each row is written with a conditional put keyed on the object key plus its ETag, so a duplicate delivery is recognised and skipped. It is not an error and it does not overwrite anything.
* **Failures are loud, expected conditions are quiet.** S3 invokes Lambda asynchronously, so there is no caller to hand an error to. A malformed event or a failed DynamoDB write raises; Lambda retries twice and then parks the event in an SQS dead-letter queue. A duplicate, or a key outside `incoming/`, does not raise, because it would otherwise burn retries and land in the dead-letter queue for no reason. Both behaviours are pinned by tests.
* **An alarm on the dead-letter queue.** A message in the queue means a file was not processed and nobody was told. The alarm sends that to the same SNS topic as the Connect alarms.
* **Object keys are decoded.** S3 sends keys URL-encoded (a space is `+`, other characters are `%XX`). Using the raw key would record a name that does not exist in the bucket.
* **The function never reads the file.** It works from the event metadata only, so its role has no S3 or KMS permission, just `dynamodb:PutItem` on one table and `sqs:SendMessage` on one queue.
* **A private, encrypted bucket.** Public access blocked, versioning on, a customer-managed KMS key with rotation, and files expire after 7 days so a lab cannot quietly accumulate cost.

**Try it:** upload any file under `incoming/` in the bucket named by `terraform output recordings_bucket`, then look at the DynamoDB table named by `terraform output recording_events_table`. To see the duplicate protection, replay the same S3 event twice from the Lambda console Test tab: the first call returns `recorded: 1`, the second `duplicate: 1`, and the table still has one row. (Uploading the same file again is a new object with a new ETag, so it is correctly a new row.)

---

## AI assist: guardrails, grounding and monitoring

A guarded generative answer. A caller's question goes to a foundation model through the Amazon Bedrock Converse API, with a **Bedrock guardrail** attached, and the Lambda returns flat strings (`status`, `answer`, `reason`) so a contact flow can read them. It is a separate function (`lambda/ai_assist/`, `terraform/ai_assist.tf`). It is not called by a flow; it is tested from the Lambda console and by an evaluation script.

* **The guardrail is Terraform, and it is a numbered version.** It denies two topics (financial and legal advice), sets five content filters (hate, insults, violence, misconduct, prompt attack) and handles three kinds of personal data (card numbers blocked, email and phone masked). The Lambda is pinned to the published version, never `DRAFT`, so the behaviour under test is the behaviour that runs.
* **The caller's text is sent as `guardContent`.** The guardrail then evaluates exactly the caller's words, including for prompt attacks, rather than the whole prompt.
* **The prompt is grounded.** `system_prompt.txt` holds the only facts the model may use and says to admit "I do not have that" and offer an agent instead of guessing. Replies are capped at two short sentences because they are read aloud.
* **A blocked prompt is a normal result, not an error.** `status` is `blocked` and `reason` names which policy fired (for example `guardrail:topicPolicy`), never the content.
* **It never raises.** A timeout, throttling, a permissions error or an empty reply each return `status = "error"` and a safe sentence, so a caller is handed to an agent rather than left in silence. The client gives up at 5 seconds, under Connect's 8-second ceiling.
* **Monitoring without leaking.** Each call prints one JSON line (status, policies fired, latency, question length). The question is never logged by the function. Three metric filters turn the lines into metrics, and three alarms (blocked-prompt spike, model errors, p95 latency over 4 seconds) notify the existing SNS topic. Bedrock model invocation logging is also switched on, to a separate log group.
* **It is evaluated, not just configured.** `evals/guardrail_cases.json` holds 15 prompts: 5 in scope, 2 grounding checks, 2 financial, 2 legal, 2 prompt-attack, 1 card number and 1 abuse. `scripts/run_ai_eval.py` sends each to the deployed function, compares the outcome with the expected one and writes `evals/results.json`.

## Knowledge base for Amazon Q in Connect

The retrieval half of the AI work. Five short, fictional help documents in `knowledge/` (opening hours, reaching an agent, what to have ready, callbacks, privacy and recordings) are loaded into an S3 bucket and added as a knowledge base on an Amazon Connect **AI agents** domain, so an agent can type a question in the agent workspace and get an answer drawn from those documents, with links to the source.

* **Built by hand in the console, not Terraform.** The domain and the S3 integration were created in the Connect console. The documents are the only part held in this repository.
* **The same facts as the guardrail Lambda.** The documents and `lambda/ai_assist/system_prompt.txt` agree (8am to 6pm AEST, press 0 for an agent), so the two AI pieces can be tested against one set of facts.
* **Tested by manual search, with questions that should and should not be answered.** Seven questions the documents answer and two they do not (the model should decline rather than invent), asked from the agent workspace.

## Running it

### Cost

An Amazon Connect instance with no claimed phone number and no calls **costs nothing to exist**. DynamoDB is on-demand, the Lambdas are idle, and the HTTP API bills per request. You can apply this, look at it, and destroy it for approximately nothing.

The two things that do cost money: a **claimed phone number** is billed monthly whether or not it is used, and **any calls** are billed per minute. AWS WAF is deliberately *not* in this build, because a web ACL carries a standing monthly charge regardless of traffic.

### Prerequisites

* Terraform >= 1.6
* AWS CLI configured for an account you own
* A region where Connect is available — this defaults to `ap-southeast-2`

### Apply

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars: instance\_alias must be globally unique in the region

terraform init
terraform plan
terraform apply
```

Terraform builds the instance, queue, routing profile, flow, both Lambdas, the API and the alarms. Three things still need a human, and `terraform output next\_steps` prints them:

1. **Claim a phone number and attach it to the flow.** Australian numbers need identity verification through a support case, which takes one to three business days.
2. **Create an agent** in the admin site, assign the routing profile, open the CCP and go Available. A call with no available agent sits in the queue and proves nothing.
3. **Confirm the SNS email subscription** if you set `alarm\_email` — it stays pending until you click the link.

### Test

```bash
pytest -q                                   # 92 tests, no AWS, Bedrock or Salesforce access needed
python3 scripts/audit\_flow.py terraform/flows/main.json.tftpl
python scripts/run\_ai\_eval.py --function <ai\_assist\_function\_name>   # needs the AI assist deployed
```

### Destroy

```bash
terraform destroy
```

`aws\_connect\_instance` carries `prevent\_destroy = true`, so this will refuse until you remove that lifecycle block. That is intentional: `identity\_management\_type` and `instance\_alias` are both immutable in the API, which means an innocent edit to either would otherwise plan a destroy-and-recreate of the whole contact centre. Release any claimed number separately — `terraform destroy` will not do it, and it keeps billing.

\---

## Honest limitations

A short list of what this is not, because a repository that claims more than it is gets found out in the first technical question.

* **Single environment.** No `dev`/`test`/`prod` separation and no remote state. The S3 backend with DynamoDB locking and KMS encryption is written in `versions.tf` and commented out. For anything shared, uncomment it — local state is a single point of failure and holds resource identifiers in plaintext.
* **No apply job in CI.** The pipeline checks and plans. Applying is a deliberate local act. A production pipeline would add an apply gated on a GitHub environment with required reviewers, running on a separate OIDC role scoped to `main`.
* **CORS is `\*`.** Lock it to a real origin before this is anywhere public.
* **No WAF.** Omitted on cost. In production a web ACL with a rate-based rule belongs in front of the chat API via CloudFront — every request the broker serves creates a real contact occupying a real queue slot, so rate limiting there is queue-flood protection rather than generic web hygiene.
* **No authentication on the chat endpoint.** A real deployment would put the broker behind an authenticated session, so the queue slot is attributable to someone.
* **Terraform-managed flow JSON is workable, not pleasant.** It is verbose and the diffs are noisy. On a large estate I would author flows in the console, export, template and commit them, with the pipeline enforcing that production only ever changes through the repository. Hand-editing flow JSON looks rigorous and produces bugs.
* **The Salesforce flow greets by name; it does not screen-pop.** `lab-salesforce-flow` invokes the lookup and copies the name into contact attributes. Showing it to the agent (a screen pop, or Service Cloud Voice) is not built. It matches one Contact by exact phone number (E.164 or 04xx form). The flow is exercised by hand (flow designer test or a claimed number), not in CI; CI checks the flow JSON structure only.
* **The recording router only records arrivals.** It does not read, transcribe or redact a recording, the bucket is not connected to Connect's recording settings, and nothing redrives messages from the dead-letter queue: a person looks at them. Retries are 2, set in Terraform, and the alarm fires on any message waiting in the queue.
* **The AI assist is not connected to a call, and it is tested on a small set.** Fifteen prompts show the controls work; they do not measure how often a guardrail is wrong at scale. The grounding check is a keyword heuristic. There is no retrieval (no knowledge base), so the model draws on a fixed prompt only. Model invocation logging is one setting per account and region and stores prompt and answer text, so use lab data only.
* **The knowledge base is a manual console build and a small one.** Five fictional documents, tested by typing questions in the agent workspace. It is not wired into a contact flow, not driven by live call analytics, and not managed as code.
* **One queue, one routing profile.** Enough to demonstrate priority and delay; not a routing model.


\---

## Licence

MIT. See [LICENSE](LICENSE).

