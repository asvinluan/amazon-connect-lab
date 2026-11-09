# Safe AI on a phone call: Sitting 1 guide

For a tired brain. Do the steps in order. Each step says what to type and what you should see. Every step ends in a safe place. All commands are for **Windows PowerShell**.

**Lab only.** This is built in your own AWS account. It is not client work.

---

## 0. Cost note (read first)

| Part | Cost |
|---|---|
| Bedrock model (Nova Lite), about 25 short questions in this sitting | a fraction of a cent each |
| Lambda, Parameter Store (standard), CloudWatch logs | free tier or cents |
| **One claimed phone number** | a small charge **every day it exists**. The claim screen shows the price. This is the only part that keeps costing |
| Calls: about 15 minutes of test calls | a few cents to under a dollar (my estimate, not a quote; I could not read the AWS price page from here) |

**Rule:** release the phone number the same day (Step 12). Look at Cost Explorer tomorrow.

## 1. Why we are building this (the business problem)

Callers want a quick answer without waiting. A generative AI can give one, but it can also be slow, be down, say something it should not, or get attacked with tricky questions. A contact centre cannot let any of that leave a caller in silence.

So the real job is not "add AI". It is **"add AI that can never hurt the call"**. Four rules:

1. **A human is always the last stop.** Every failure ends with "Connecting you to an agent now."
2. **A kill switch.** One setting turns the AI off in seconds, no code change.
3. **Shadow mode.** Run the AI on real calls but do not let callers hear it. Look at the outcome it logs (answered, blocked or error). Only then switch it on.
4. **Break it on purpose.** Prove the fallback works before a customer finds out.

## 2. How it works (plain English)

```
 Caller presses 1, 2, 3 or 4  (a pretend question; Lex replaces the keypad in Sitting 2)
        |
        v
 Contact flow "lab-ai-flow"  -- asks the AI Lambda, waits at most 8 seconds
        |
        v
 Lambda "ai-assist"  -- reads the switch in Parameter Store first
        |
        +-- mode OFF      -> does nothing, says "disabled"
        +-- mode SHADOW   -> asks Bedrock, writes the answer to the LOG ONLY
        +-- mode LIVE     -> asks Bedrock (with the guardrail) and returns the answer
        +-- switch broken -> treated as OFF (fail safe)
        |
        v
 Flow decides:  answered or blocked -> caller hears the sentence
                anything else       -> caller hears nothing from the AI
        |
        v
 Either way: "Connecting you to an agent now"  -> queue
```

The four keys: 1 = "What are your opening hours?" 2 = "Which stock should I buy this week?" (the guardrail refuses) 3 = a prompt attack (the guardrail refuses) 4 = "Can I get a callback?"

**The AWS services, and why each one is here**

| Service | Job in this build | Why this and not something else |
|---|---|---|
| Amazon Connect flow | the call script, with an error branch for every failure | it is the platform; the flow is where "never silent" is enforced |
| Lambda (`ai-assist`) | the logic: read the switch, call the model, return a short answer | a flow cannot run logic or call Bedrock itself |
| Parameter Store | holds the one switch value | free, instant to change, no redeploy, can be locked down to one reader |
| Bedrock + guardrail | the generative answer, with banned topics and attack filters | the guardrail is pinned to a numbered version so tested equals running |
| Polly | speaks the answer (the flow's "Olivia" voice is a Polly voice) | built into Connect; no extra build needed |
| CloudWatch | one log line per call, plus the alarms from the earlier lab | shows the outcome shadow mode recorded (answered, blocked, error, with timing), and tells you when AI misbehaves |
| Terraform | builds the flow, permission and switch the same way every time | repeatable, reviewable, easy to remove |

## 3. Before you start (2 minutes)

Open PowerShell and type:

```
cd "C:\Users\AV\Documents\ITSM\11 GitHub Repos\amazon-connect-lab"
git status --short
```

You should see the new files listed (they start with `??`) plus a few lines starting with `M`. If the list is empty, stop and tell Claude.

Also confirm you are signed in to your lab AWS account:

```
aws sts get-caller-identity
```

You should see an Account number and an Arn. If you see an error, run `aws configure` (or `aws sso login`) and try again.

## 4. The steps

### Step 1. Look at what will change (nothing is changed yet)

```
.\scripts\ai-flow\plan.ps1
```

Type `yes` when it asks. **You should see:** `4 to add, 1 to change, 0 to destroy` and a green line saying this is the plan the lab expects.

What it means: 4 new things (the switch, a permission, a Lambda permission for Connect, the flow) and 1 change (the AI Lambda gets the new code).

A boxed warning saying **"Resource targeting is in effect"** is normal. The plan is limited to the five AI flow resources on purpose, because this folder also holds an old ServiceNow experiment we are leaving alone.

*If you see a different number, or red text, do not continue. Paste it to Claude.*

### Step 2. Build it

```
.\scripts\ai-flow\apply.ps1
```

Type `yes`. **You should see:** `Apply complete! Resources: 4 added, 1 changed, 0 destroyed` and a green line `AI mode switch: /asvinluan-connect-lab/ai/mode = off`.

It starts **off** on purpose: the safe state.

### Step 3. Break it on purpose, without a phone

```
.\scripts\ai-flow\test-modes.ps1
```

Type `yes`. It takes about 3 minutes. It flips the switch through eight cases and sends a test question to the Lambda each time.

**You should see eight PASS lines and `8 of 8 checks passed`.** What each one proves:

| Mode | Question | You should see | Meaning |
|---|---|---|---|
| off | hours | status=disabled | the kill switch works, the model is not called |
| shadow | hours | status=shadow, reason=would_be_answered | the AI ran but the caller would hear nothing |
| live | hours | status=answered | the caller would hear the answer |
| live | stock tip | status=blocked | the guardrail refuses financial advice |
| live | prompt attack | status=blocked | the guardrail refuses the attack |
| test_fail | hours | status=error | an AI error returns a safe sentence |
| test_crash | hours | FUNCTION ERROR | the Lambda crashes; the flow must cope |
| test_slow | hours | FUNCTION ERROR (timed out) | the Lambda is too slow; the flow must cope |

At the end the script sets the mode back to **off**.

### Step 4. See the log (and why it is safe)

```
.\scripts\ai-flow\audit-trail.ps1 -Minutes 20
```

**You should see** a table: time, mode, status, reason, milliseconds and a contact id. The question itself is never in the log. Times are your local time (the AWS console shows UTC).

### Step 5. Look at the pieces in the AWS console (5 minutes, optional but it is how you learn)

Make sure the Region is **Asia Pacific (Sydney)**.

1. **Systems Manager > Parameter Store**: open `/asvinluan-connect-lab/ai/mode`. That is the kill switch.
2. **Lambda > asvinluan-connect-lab-ai-assist > Configuration > Environment variables**: find `AI_MODE_PARAM`. Then **Permissions**: the role can read only that one parameter.
3. **Amazon Connect > your instance > Flows**: open `lab-ai-flow`. Follow the lines. Find the **Invoke AWS Lambda function** block and its red error line going to the "Connecting you to an agent" message.

### Step 6. Claim one phone number and point it at the AI flow

1. **Amazon Connect > asvinluan-connect-lab > Channels > Phone numbers > Claim a number**.
2. Country **Australia**, type **DID**, pick any number. **Note the price shown.**
3. In **Contact flow / IVR**, choose **lab-ai-flow**. Save.

(If you already hold a number from another lab, you can point that one at `lab-ai-flow` instead and point it back afterwards. Write down where it pointed.)

### Step 7. Shadow mode: the AI runs, the caller hears nothing

```
.\scripts\ai-flow\set-mode.ps1 -Mode shadow
```

Wait 15 seconds. Call the number. Press **1**.

**What you should hear:** the menu, then only **"Connecting you to an agent now."** (no AI answer). Hang up.

Now look at what shadow mode recorded. The log keeps the outcome and timing, not the words:

```
.\scripts\ai-flow\audit-trail.ps1 -Minutes 10
```

**You should see** a row with mode=shadow, status=answered. The AI did answer. The caller never heard it. This is how a team judges an AI before a customer ever hears it.

Call again and press **2** (stock tip). The row should show status=blocked: the guardrail refused it.

### Step 8. Live mode: the caller hears the answer

```
.\scripts\ai-flow\set-mode.ps1 -Mode live
```

Wait 15 seconds. Call, press **1**: you hear the opening hours, then "Connecting you to an agent now." Call again and press **2** or **3**: you hear "Sorry, I can't help with that here. I can connect you to an agent." Press **4**: you hear the callback answer. Check the log:

```
.\scripts\ai-flow\audit-trail.ps1 -Minutes 10
```

### Step 9. The kill switch on a real call

```
.\scripts\ai-flow\set-mode.ps1 -Mode off
```

Wait 15 seconds. Call, press **1**: you hear only "Connecting you to an agent now." No AI. That is how an operator turns the AI off in an incident.

### Step 10. Break it on a real call

```
.\scripts\ai-flow\set-mode.ps1 -Mode test_crash
```

Wait 15 seconds. Call, press **1**. **You should hear** "Connecting you to an agent now." even though the Lambda crashed.

Then:

```
.\scripts\ai-flow\set-mode.ps1 -Mode test_slow
```

Wait 15 seconds. Call, press **1**. **You will hear about 7 to 8 seconds of silence**, then "Connecting you to an agent now." That silence is the cost of a slow AI, and the reason the flow gives up at 8 seconds. Write that sentence in your notes: it is an interview answer.

Set it back: `.\scripts\ai-flow\set-mode.ps1 -Mode off`.

### Step 11. Evidence (screenshots to keep)

- The `8 of 8 checks passed` screen from Step 3.
- The audit-trail table showing one shadow row, one live row and one off row.
- The flow in the Connect designer (Step 5).
- The Parameter Store page (Step 5).

Crop out the AWS account number before sharing anything publicly.

### Step 12. Clean up (do this today)

```
.\scripts\ai-flow\remove.ps1
```

It sets the mode to off, asks before releasing each phone number (type `yes` for the one you claimed), then removes `lab-ai-flow`. The guardrail, the Lambda and the switch stay, because the switch is off and they cost almost nothing.

**You should see:** `Clean-up done.` Check in the console that **Channels > Phone numbers** is empty.

### Step 13. Save your work in Git

```
git add .gitignore README.md lambda/ai_assist terraform/ai_assist.tf terraform/ai_flow.tf terraform/flows/ai.json.tftpl tests/test_ai_assist.py tests/test_ai_modes.py tests/test_ai_flow.py scripts/ai-flow docs/AI-FLOW-LAB.md
git status --short
git commit -m "Add AI flow: mode switch, shadow mode, fallbacks, tests and scripts"
git push
```

The repo also holds older ServiceNow files that are not part of this sitting; the `git add` above names each file so it leaves them alone on purpose (do not use `git add .` or add the whole `tests` folder). Check Actions on GitHub goes green.

## 5. What can go wrong

| What you see | What it means | What to do |
|---|---|---|
| `plan.ps1` shows a different number | something else drifted | do not apply; paste the plan to Claude |
| `test-modes.ps1` live case says `error`, reason `bedrock_AccessDeniedException` | the Bedrock model is not enabled for this account or Region | tell Claude; it is a settings fix |
| Mode change seems ignored | the Lambda caches the switch for up to 15 seconds | wait 15 seconds and try again |
| A call goes to the wrong flow | the number is still pointed at another flow | Channels > Phone numbers > edit > choose `lab-ai-flow` |
| `remove.ps1` says the flow is in use | the number still points at it | release the number first, then run it again |
| Nothing appears in the audit trail | calls went to a different flow, or you waited too long | try `-Minutes 120`; check the number's flow |

## 6. What this sitting proves, and what it does not

**Proves (lab):** a guarded generative answer on a call with a kill switch, shadow mode, a tested fallback for failure, crash and timeout, and a safe default if the switch cannot be read.

**Does not prove:** speech input (Lex arrives in Sitting 2), agent assist (Sitting 3), reporting on the logs (Sitting 4), production load, or a red-team. The questions are four fixed ones, not free speech.

## 7. Say it in an interview

- **What if Bedrock does not work?** "The caller never hears the AI fail. The Lambda returns a safe sentence for errors, the flow has an error branch for a crash or a timeout, and every path ends at an agent. I proved it by breaking it on purpose: forced error, forced crash and forced slow answer."
- **How do you roll out AI safely?** "Shadow first. The AI runs on real calls but the caller never hears it. I compare the logged outcomes (answered, blocked, error, latency) against what I expect, then switch to live. A Parameter Store switch can turn it off in seconds without a deploy, and it fails safe to off if it cannot be read."
- **Why Parameter Store for the switch?** "It is free, changes instantly, and the Lambda can be allowed to read just that one value."
- **What does a slow model cost the caller?** "Up to the flow's 8-second limit of silence, then a human. That is why the Lambda has its own shorter timeouts."
