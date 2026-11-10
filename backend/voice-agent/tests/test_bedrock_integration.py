"""Integration tests for Bedrock LLM with real API calls.

These tests use actual Bedrock API calls to verify:
- Tool calling works correctly with Bedrock's Converse API
- The system prompt correctly prevents pre-tool explanatory text
- Context aggregation produces valid message structures

Requires AWS credentials with access to Bedrock.
Mark tests with @pytest.mark.integration to skip in CI without credentials.
"""

import json
import os
import pytest
import boto3
from typing import Any, Dict, List, Optional

# Skip all tests in this module if AWS credentials aren't available
pytestmark = pytest.mark.integration

# Same model the app uses by default (LLM_MODEL_ID in pipeline_ecs.py). The
# previous Claude 3.5 Haiku profile reached end of life and every call failed
# with ResourceNotFoundException.
MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def get_bedrock_client():
    """Get a Bedrock runtime client."""
    try:
        session = boto3.Session(region_name=os.environ.get("AWS_REGION", "us-east-1"))
        return session.client("bedrock-runtime")
    except Exception as e:
        pytest.skip(f"AWS credentials not available: {e}")


# Tool definitions in Bedrock format
TOOLS = [
    {
        "toolSpec": {
            "name": "get_current_time",
            "description": "Get the current time in a specified timezone.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "timezone": {
                            "type": "string",
                            "description": "Timezone (default: UTC)",
                        }
                    },
                    "required": [],
                }
            },
        }
    },
    {
        "toolSpec": {
            "name": "echo",
            "description": "Echo back a message. Use this to repeat something the user said.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "The message to echo back",
                        }
                    },
                    "required": ["message"],
                }
            },
        }
    },
]

# System prompt WITHOUT tool guidance (old behavior - causes pre-tool text)
SYSTEM_PROMPT_OLD = """You are a helpful voice assistant.
Keep responses brief and conversational."""

# System prompt WITH tool guidance (new behavior - prevents pre-tool text)
SYSTEM_PROMPT_NEW = """You are a helpful voice assistant.
Keep responses brief and conversational.

Tool Usage:
- When using tools, call them directly without explaining what you're doing first
- After the tool returns, respond naturally with the result
- Do NOT say "Let me..." or "I'll use..." before calling a tool"""


class TestBedrockToolCalling:
    """Test Bedrock Converse API tool calling."""

    def test_basic_tool_call(self):
        """Test that a simple tool call works."""
        client = get_bedrock_client()

        response = client.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"text": "What time is it?"}]}],
            system=[{"text": SYSTEM_PROMPT_NEW}],
            toolConfig={"tools": TOOLS},
        )

        # Should return a tool use
        content = response["output"]["message"]["content"]

        # Check if tool was called
        tool_uses = [c for c in content if "toolUse" in c]
        assert len(tool_uses) > 0, f"Expected tool call, got: {content}"

        tool_use = tool_uses[0]["toolUse"]
        assert tool_use["name"] == "get_current_time"

    def test_tool_call_with_result(self):
        """Test complete tool call flow with result."""
        client = get_bedrock_client()

        # First turn: user asks for time
        response1 = client.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"text": "What time is it?"}]}],
            system=[{"text": SYSTEM_PROMPT_NEW}],
            toolConfig={"tools": TOOLS},
        )

        content1 = response1["output"]["message"]["content"]
        tool_uses = [c for c in content1 if "toolUse" in c]
        assert len(tool_uses) > 0

        tool_use = tool_uses[0]["toolUse"]
        tool_use_id = tool_use["toolUseId"]

        # Second turn: provide tool result
        messages = [
            {"role": "user", "content": [{"text": "What time is it?"}]},
            {"role": "assistant", "content": content1},
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": tool_use_id,
                            "content": [
                                {
                                    "text": json.dumps(
                                        {
                                            "current_time": "3:45 PM",
                                            "timezone": "UTC",
                                        }
                                    )
                                }
                            ],
                        }
                    }
                ],
            },
        ]

        response2 = client.converse(
            modelId=MODEL_ID,
            messages=messages,
            system=[{"text": SYSTEM_PROMPT_NEW}],
            toolConfig={"tools": TOOLS},
        )

        # Should get a text response with the time
        content2 = response2["output"]["message"]["content"]
        text_contents = [c for c in content2 if "text" in c]
        assert len(text_contents) > 0

        response_text = text_contents[0]["text"].lower()
        assert "3:45" in response_text or "time" in response_text


class TestPreToolTextBehavior:
    """Test that system prompt controls pre-tool explanatory text.

    This is the key test for the context aggregation fix. When the LLM
    outputs text BEFORE calling a tool, it causes race conditions in
    the pipeline's context aggregator.
    """

    def test_new_prompt_prevents_pre_tool_text(self):
        """Test that new system prompt prevents pre-tool explanatory text.

        With the new prompt guidance, the LLM should call tools directly
        without first saying things like "Let me check that for you..."
        """
        client = get_bedrock_client()

        # Run multiple times to check consistency
        pre_tool_text_count = 0
        trials = 5

        for _ in range(trials):
            response = client.converse(
                modelId=MODEL_ID,
                messages=[{"role": "user", "content": [{"text": "What time is it?"}]}],
                system=[{"text": SYSTEM_PROMPT_NEW}],
                toolConfig={"tools": TOOLS},
            )

            content = response["output"]["message"]["content"]

            # Check if there's text BEFORE the tool use
            has_text = any("text" in c for c in content)
            has_tool = any("toolUse" in c for c in content)

            if has_text and has_tool:
                # Check order - text before tool is bad
                text_idx = next(i for i, c in enumerate(content) if "text" in c)
                tool_idx = next(i for i, c in enumerate(content) if "toolUse" in c)
                if text_idx < tool_idx:
                    pre_tool_text_count += 1

        # With new prompt, should have minimal pre-tool text
        # Allow some variance since LLM isn't deterministic
        assert pre_tool_text_count <= 2, (
            f"Pre-tool text appeared {pre_tool_text_count}/{trials} times. "
            "System prompt should prevent explanatory text before tool calls."
        )

    def test_echo_tool_no_pre_explanation(self):
        """Test echo tool is called directly without explanation."""
        client = get_bedrock_client()

        response = client.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"text": "Echo hello"}]}],
            system=[{"text": SYSTEM_PROMPT_NEW}],
            toolConfig={"tools": TOOLS},
        )

        content = response["output"]["message"]["content"]

        # Should have tool use
        tool_uses = [c for c in content if "toolUse" in c]
        assert len(tool_uses) > 0, f"Expected tool call, got: {content}"

        # Check for pre-tool text
        text_contents = [c for c in content if "text" in c]
        if text_contents:
            # If there is text, it should be minimal or after the tool
            text_idx = next(i for i, c in enumerate(content) if "text" in c)
            tool_idx = next(i for i, c in enumerate(content) if "toolUse" in c)

            if text_idx < tool_idx:
                pre_text = text_contents[0]["text"]
                # Pre-tool text should be very short if it exists
                assert len(pre_text) < 50, (
                    f"Pre-tool text too long: '{pre_text}'. "
                    "LLM should call tool directly."
                )


class TestContextStructure:
    """Test that context structure is correct for tool calls."""

    def test_valid_context_with_tool_result(self):
        """Test that tool result context is valid for Bedrock."""
        client = get_bedrock_client()

        # Get a tool call
        response1 = client.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"text": "Echo hello world"}]}],
            system=[{"text": SYSTEM_PROMPT_NEW}],
            toolConfig={"tools": TOOLS},
        )

        content1 = response1["output"]["message"]["content"]
        tool_uses = [c for c in content1 if "toolUse" in c]

        if not tool_uses:
            pytest.skip("LLM didn't call tool in this run")

        tool_use = tool_uses[0]["toolUse"]
        tool_use_id = tool_use["toolUseId"]

        # Build valid context structure
        messages = [
            {"role": "user", "content": [{"text": "Echo hello world"}]},
            {"role": "assistant", "content": content1},
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": tool_use_id,
                            "content": [
                                {
                                    "text": json.dumps(
                                        {
                                            "echoed_message": "hello world",
                                        }
                                    )
                                }
                            ],
                        }
                    }
                ],
            },
        ]

        # This should succeed without validation errors
        response2 = client.converse(
            modelId=MODEL_ID,
            messages=messages,
            system=[{"text": SYSTEM_PROMPT_NEW}],
            toolConfig={"tools": TOOLS},
        )

        # Should get a response
        assert response2["output"]["message"]["content"]

    def test_malformed_context_with_split_text(self):
        """Test what happens with malformed context (text after tool result).

        This simulates the bug we're fixing - when text and toolUse are
        split across messages due to race conditions.
        """
        client = get_bedrock_client()

        # Malformed context: text appears AFTER tool result in separate message
        # This is what was happening due to the race condition
        messages = [
            {"role": "user", "content": [{"text": "Echo hello"}]},
            # Tool call with NO text
            {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "test-123",
                            "name": "echo",
                            "input": {"message": "hello"},
                        }
                    }
                ],
            },
            # Tool result
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "test-123",
                            "content": [{"text": '{"echoed_message": "hello"}'}],
                        }
                    }
                ],
            },
            # Text AFTER tool result (this is the malformed part)
            {
                "role": "assistant",
                "content": [{"text": "I'll echo that for you."}],
            },
        ]

        # This should still work, but might produce confused or empty responses
        response = client.converse(
            modelId=MODEL_ID,
            messages=messages,
            system=[{"text": SYSTEM_PROMPT_NEW}],
            toolConfig={"tools": TOOLS},
        )

        # The response might be confused, repetitive, or even empty due to malformed context
        # This test documents the behavior rather than asserting correctness
        content = response["output"]["message"]["content"]
        # Empty or confused responses are expected with malformed context
        # The key insight is: don't let context get malformed in the first place!
        assert response.get("stopReason") is not None  # At least the API completed


class TestMultipleToolCalls:
    """Test multiple tool calls in a conversation."""

    def test_sequential_tool_calls(self):
        """Test two tool calls in sequence."""
        client = get_bedrock_client()
        messages = []

        # First turn: ask for time
        messages.append({"role": "user", "content": [{"text": "What time is it?"}]})

        response1 = client.converse(
            modelId=MODEL_ID,
            messages=messages,
            system=[{"text": SYSTEM_PROMPT_NEW}],
            toolConfig={"tools": TOOLS},
        )

        content1 = response1["output"]["message"]["content"]
        messages.append({"role": "assistant", "content": content1})

        # Provide tool result
        tool_uses = [c for c in content1 if "toolUse" in c]
        if tool_uses:
            tool_use_id = tool_uses[0]["toolUse"]["toolUseId"]
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": tool_use_id,
                                "content": [{"text": '{"current_time": "3:45 PM"}'}],
                            }
                        }
                    ],
                }
            )

            # Get response to tool result
            response2 = client.converse(
                modelId=MODEL_ID,
                messages=messages,
                system=[{"text": SYSTEM_PROMPT_NEW}],
                toolConfig={"tools": TOOLS},
            )
            content2 = response2["output"]["message"]["content"]
            messages.append({"role": "assistant", "content": content2})

        # Second turn: ask to echo
        messages.append({"role": "user", "content": [{"text": "Now echo hello"}]})

        response3 = client.converse(
            modelId=MODEL_ID,
            messages=messages,
            system=[{"text": SYSTEM_PROMPT_NEW}],
            toolConfig={"tools": TOOLS},
        )

        content3 = response3["output"]["message"]["content"]

        # Should call echo tool
        tool_uses3 = [c for c in content3 if "toolUse" in c]
        assert len(tool_uses3) > 0, f"Expected echo tool call, got: {content3}"
        assert tool_uses3[0]["toolUse"]["name"] == "echo"


if __name__ == "__main__":
    # Run with: python -m pytest tests/test_bedrock_integration.py -v -m integration
    pytest.main([__file__, "-v", "-m", "integration"])
