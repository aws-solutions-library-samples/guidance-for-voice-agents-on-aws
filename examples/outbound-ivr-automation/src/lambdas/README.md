# `src/lambdas/tools`: the Gateway Lambda target

Workstream 3 (tools backend). Not deployed by this workstream: CDK (workstream 4)
wires the Gateway target, the Policy engine and this function's role and env vars. This
only builds the handler, its schema, and documents how to package it.

## Files

- `handler.py`: `lambda_handler(event, context)`, dispatching on the tool name.
- `schema.json`: the four tools' MCP input schemas, for `--tool-schema-file` /
  `inlinePayload` when CDK creates the Gateway target (see the AgentCore Gateway docs
  for "Add a Lambda target").

## Event format (verified against AWS's AgentCore Gateway docs and the
"How Cohere Health digitizes clinical policies" blog post, October 2026)

The Gateway invokes the Lambda directly per MCP tool call:

- **`event`** is the tool's input fields as a plain dict: no envelope. For
  `record_item` that's `{"case_id": ..., "call": ..., "key": ..., "value": ...,
  "confirmed": ...}`.
- **The tool name is not in `event`.** It's on the Lambda context object:
  `context.client_context.custom["bedrockAgentCoreToolName"]`, prefixed with the
  Gateway target's name and three underscores (Cedar's own action-naming convention is
  the same: `"TargetName___toolName"`). `handler.tool_name_from_context()` strips the
  prefix.
- The handler returns a plain dict; the Gateway wraps it as the tool's MCP result.
  This handler returns `{"result": "<short string>"}` or `{"error": "<message>"}`.

## Dependencies

Stdlib + boto3 only, matching the design doc's "Workstreams" note ("prefer stdlib +
boto3 for the handler"). boto3 doesn't need vendoring: it's in every standard Lambda
Python runtime image.

## Packaging: import `outbound_ivr.tools`, not vendor it

`handler.py` imports `outbound_ivr.tools.business` and `outbound_ivr.tools.store`, i.e. the actual
package this workstream wrote in `src/outbound_ivr/tools/`, so the authorization and
intake rules live in exactly one place (same code path as `OUTBOUND_IVR_TOOLS=local` and the
unit tests). Two ways CDK can get that onto the Lambda's `sys.path`; pick one when
workstream 4 builds the asset:

1. **Preferred: a bundling command that copies the package in.** Point the CDK
   `lambda.Code.fromAsset` (or `PythonFunction`) at `src/lambdas/tools/`, with a
   `BundlingOptions.command` (or a pre-synth build step) that runs, from the repo root:

   ```sh
   cp -r src/outbound_ivr src/lambdas/tools/_build/outbound_ivr   # only tools/, business.py, store.py,
                                                    # and __init__.py are needed; intake.py,
                                                    # prompts.py etc. are not imported by
                                                    # the Lambda and can be left out of the
                                                    # copy if asset size matters.
   ```

   then bundles `src/lambdas/tools/_build` (handler.py, schema.json, outbound_ivr/) as the asset.
   This keeps `outbound_ivr.tools` as regular package imports, so `handler.py` is unchanged
   between a local invoke and a deployed one.

2. **Alternative: `pip install .` the whole `outbound_ivr` project into the asset dir** using
   the repo's `pyproject.toml` (no extras needed, the base dependencies already cover
   `boto3`), e.g. `uv pip install --target src/lambdas/tools/_build .` This pulls in more
   than the Lambda needs (fastapi, uvicorn, etc. from the base deps) but needs no manual
   file list to keep in sync when `tools/` changes.

Either way, `_build/` (or whatever the CDK asset staging directory is called) should be
gitignored and produced only at synth/bundle time: it isn't committed here.
