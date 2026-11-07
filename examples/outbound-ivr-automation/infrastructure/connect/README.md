# Mock office on Amazon Connect

In AWS, Outbound IVR (on AgentCore, dialing out through a Chime Voice Connector) calls a mock
provider office that runs as an Amazon Connect contact flow on its own phone number.
`outbound-ivr-mock-provider-ivr.json` is that flow: here **3, 3, 2** reaches the prior
authorization desk (main menu 3 for providers, 3 for authorizations and referrals, 2
for imaging and procedure prior authorizations).

## Regenerating the flow

The flow is generated, not hand-edited. The shared wording lives in
`infrastructure/connect/prompts.py` (`PROMPTS`); the Connect menu's own lines (`CONNECT_PROMPTS`), its
structure (`MENUS`, `TRANSFER_PATH`) and the menu logic (timeouts, retries, star to go
back, dead ends, the transfer) are in `infrastructure/connect/make_flow.py`.

```
uv run python connect/make_flow.py
```

This writes `infrastructure/connect/flows/outbound-ivr-mock-provider-ivr.json`. Action identifiers are
`uuid5(NAMESPACE, name)`, so regenerating with no changes reproduces the file byte for
byte: `tests/test_connect_flow.py` checks the committed JSON against a fresh run. Commit
the regenerated file whenever `PROMPTS` or the menu structure changes.

This script never calls Polly or touches any AWS resource: it only reads the `PROMPTS`
dict from `infrastructure/connect/prompts.py` and writes JSON locally. Polly is Amazon Connect's own
text-to-speech, invoked at call time by the deployed flow, not by this script.

### The front desk transfer

The 3-3-2 path reaches the front desk through a queue: `UpdateContactTargetQueue` sets
the working queue to `${FRONT_DESK_QUEUE_ARN}`, then `TransferContactToQueue` transfers
to it. `OutboundIvrOfficeStack`'s own agent (`outbound-ivr-front-desk`) answers in the dashboard's
embedded CCP, same as a real office. Both actions' shapes (the `QueueId` parameter,
`TransferContactToQueue`'s required-but-never-taken-on-success `NextAction`, and its
`QueueAtCapacity`/`NoMatchingError` error branches) match the Escalation nested stack's
handoff flow (`infrastructure/stacks/escalation_stack.py`), which Connect validated.

The "please hold while I transfer you to our prior authorization desk" message plays
first. `infrastructure/scripts/deploy_flow.sh` fills `${FRONT_DESK_QUEUE_ARN}` from SSM
`/outbound-ivr/connect/front-desk-queue-arn`, one of `OutboundIvrOfficeStack`'s four SSM parameters
(written on every deploy by `infrastructure/stacks/escalation_stack.py`). That
path is deliberately different from the v1.0.0 deployment's own
`/outbound-ivr/connect/front-desk-queue-arn`, which this project never reads or writes.

## Importing into a test instance

Since v2.0.0 the real deploy doesn't need this: `OutboundIvrOfficeStack`
(`infrastructure/stacks/office_stack.py`), a stand-alone top-level stack, deployed
once, ahead of and independently of `OutboundIvrStack`, creates and owns a Connect instance
for the app outright, and creates both the queue the queue variant transfers to and this
flow directly inside that instance, substituting the queue's real arn at deploy time.
This section is for trying the flow by hand in some *other* Connect instance (your own
sandbox, say) without running any of this project's CDK:

1. In the Amazon Connect admin website for your test instance, go to **Routing > Flows >
   Create flow**.
2. Open the flow's menu (top right) and choose **Import flow (beta)**, then select
   `infrastructure/connect/flows/outbound-ivr-mock-provider-ivr.json`.
3. Before publishing, replace `${FRONT_DESK_QUEUE_ARN}` in the `UpdateContactTargetQueue`
   action's `QueueId` parameter with a real queue ARN on your test instance. Never commit
   a real ARN back into the JSON.
4. Publish the flow and associate it with a claimed phone number on the test instance (or
   a test contact flow entry point) to call it.
5. Check that **Enable logging** is on for the instance (Analytics and optimization >
   Flow logs) if you want to see the flow's run in CloudWatch Logs; the flow itself turns
   on `UpdateFlowLoggingBehavior` already.

Connect validates the whole flow on upload (`infrastructure/scripts/deploy_flow.sh`,
`UpdateContactFlowContent`, or `OutboundIvrOfficeStack`'s own
`AWS::Connect::ContactFlow` create/update; all three go through the same validation).
The **queue variant is the one deployed and live**: the flow on this project's own
Connect instance uses `UpdateContactTargetQueue` and `TransferContactToQueue`,
and it is what rings the front desk in the dashboard's embedded CCP. The committed
`infrastructure/connect/flows/outbound-ivr-mock-provider-ivr.json` carries `${FRONT_DESK_QUEUE_ARN}` as its
only placeholder, which is the same thing from the other direction.

## Design notes: why the flow's logic looks like this

This flow was originally built to match a local Asterisk mock office's behavior
line-for-line; that Asterisk office has since been removed, but the notes below still
explain real decisions baked into the committed flow, so they're kept.

- **The transfer action.** A queue transfer: `UpdateContactTargetQueue` then
  `TransferContactToQueue`, whose required `QueueAtCapacity` and `NoMatchingError` error
  branches (plus any other error) play the front-desk-busy line and disconnect. A
  successful `TransferContactToQueue` ends this flow outright, since the contact is then
  in the queue. See "The front desk transfer" above.
