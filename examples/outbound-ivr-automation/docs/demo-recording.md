# Recording a demo call

There is one path: the deployed AWS path (`infrastructure/`, see the root README's
"Deploy" section). The dashboard is a Cognito-gated SPA on CloudFront, the agent is the real
Outbound IVR on a Bedrock AgentCore Runtime, and the call goes out over a self-hosted LiveKit
SIP trunk through the Chime SDK Voice Connector to the Connect mock office. No local
containers, no `.env` secrets on your machine for the call itself, only your own cell
phone to answer as the front desk.

1. **Sign in.** Open the CloudFront URL for your deployment and sign in with the
   Cognito user created for you (`aws cognito-idp admin-create-user`, see
   `infrastructure/scripts/deploy.sh`'s output): email + password,
   then a TOTP code from an authenticator app on first sign-in.
2. **Case queue.** The landing page lists seeded cases (`infrastructure/scripts/seed_cases.py`;
   add `--extra` for ten more synthetic cases). The pencil
   on each row edits a case or resets it to `pending`. Pick one.
3. **Start call.** Outbound IVR dials the office through the deployed trunk; the live-call page
   polls the events table and shows the menu navigation as it happens.
4. **Answer as the front desk in the dashboard's CCP.** The office's Connect flow
   transfers the call to the front desk queue, and it rings the embedded Contact Control
   Panel: sign that in once as `outbound-ivr-front-desk` and set yourself Available (see the
   README, "Setting up the Connect front desk"). Play the front desk: give the three items
   when asked, confirm each read-back, or push back to see Outbound IVR escalate.
5. **Review the evidence.** Once Outbound IVR hangs up, open the case's evidence page: items,
   outcome, and the recording (a presigned S3 link). **Monitor call** also works: it's
   always on, no CIDR or flag needed. The dashboard polls its own Cognito-protected
   API, which lists and reads mixed call audio and speaking-indicator levels from a
   dedicated S3 bucket (see the README's "Security and data" for how that transit is
   bounded). Listen-in audio lags the live call by about 3-6 seconds.

Escalation has two modes, and **which one you get depends on `OUTBOUND_IVR_EVIDENCE_MODE`**, so check
before you record. In `direct` (what the reference deployment runs) Outbound IVR simply dials the
evidence number straight into the call as a live colleague: no handoff code, no DTMF, your
phone just rings. In `connect` Outbound IVR reads a 6-digit handoff code by DTMF into the Evidence
team's claimed DID, and Connect looks it up and whispers the summary to whichever agent
answers in their Contact Control Panel. See
[docs/escalation-handoff.md](escalation-handoff.md) for the contract.

## If something goes wrong

- **The live-call page shows only End call**: a previous call's events never stopped;
  reload the page.
- **Monitor returns `404 monitor not enabled`**: `OUTBOUND_IVR_MONITOR_BUCKET` is unset on the
  dashboard API Lambda for this deploy; the Monitor nested stack wasn't built.
- Logs: CloudWatch Logs for the Outbound IVR Runtime and for each Lambda.
