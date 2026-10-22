# Amazon Connect reference lab

A small, runnable Amazon Connect contact centre defined entirely in Terraform, with a Python Lambda on the voice path, a credential-broker API behind API Gateway for web chat, CloudWatch alarms tied to failures this build can actually produce, and a GitHub Actions pipeline that checks and plans on every pull request.

> \*\*This is lab work.\*\* It is built in my own AWS account to learn and demonstrate the services, not extracted from any employer or client system. There is no client data, no proprietary configuration and no real customer information anywhere in this repository. Seed data uses fictional numbers.

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
|`tests/`|Seven unit tests pinning the behaviour that keeps the flow safe|
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
pytest -q                                   # 7 tests, no AWS access needed
python3 scripts/audit\_flow.py terraform/flows/main.json.tftpl
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
* **One queue, one routing profile.** Enough to demonstrate priority and delay; not a routing model.
* Workflow rerun test

\---

## Licence

MIT. See [LICENSE](LICENSE).

