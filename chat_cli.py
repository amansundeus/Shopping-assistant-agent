"""Chat with the shopping assistant agent from the terminal.

    python chat_cli.py                                  # interactive: type messages, Ctrl+C to quit
    python chat_cli.py "Best 5G phone under 20000"      # ask one question and exit

Reads ANTHROPIC_API_KEY, ANTHROPIC_AGENT_ID and ANTHROPIC_ENVIRONMENT_ID from .env.
Follow-up messages go to the same session, so the agent remembers the conversation.
"""

import os
import sys

import anthropic
from dotenv import load_dotenv

# Nobody approves tool calls from here, so any call that pauses for approval is denied
# rather than left waiting forever.
UNATTENDED_DENY_MESSAGE = "No one is available to approve this tool call. Continue without it."


def run_turn(client: anthropic.Anthropic, session_id: str, text: str) -> bool:
    """Send one message and print the reply as it streams. Returns False if the session ended."""
    # Open the stream before sending, so no early events are missed.
    with client.beta.sessions.events.stream(session_id=session_id) as stream:
        client.beta.sessions.events.send(
            session_id=session_id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": text}]}],
        )

        for event in stream:
            if event.type == "agent.message":
                for block in event.content:
                    if block.type == "text":
                        print(block.text, end="", flush=True)
            elif event.type in ("agent.tool_use", "agent.mcp_tool_use"):
                print(f"\n[Using tool: {event.name}]", flush=True)
                if getattr(event, "evaluated_permission", None) == "ask":
                    client.beta.sessions.events.send(
                        session_id=session_id,
                        events=[
                            {
                                "type": "user.tool_confirmation",
                                "tool_use_id": event.id,
                                "result": "deny",
                                "deny_message": UNATTENDED_DENY_MESSAGE,
                            }
                        ],
                    )
            elif event.type == "session.error":
                print(f"\n[Session error: {getattr(event, 'error', 'unknown')}]", file=sys.stderr)
            elif event.type == "session.status_terminated":
                print("\n[Session ended]", file=sys.stderr)
                return False
            elif event.type == "session.status_idle":
                stop_reason = getattr(getattr(event, "stop_reason", None), "type", None)
                # requires_action means the agent is waiting on a reply to a paused call; keep going.
                if stop_reason == "requires_action":
                    continue
                print()
                if stop_reason not in (None, "end_turn"):
                    print(f"[Agent stopped: {stop_reason}]", file=sys.stderr)
                    return False
                return True
    return True


def main() -> int:
    # Windows consoles often default to a code page that can't print "₹".
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv()
    agent_id = os.getenv("ANTHROPIC_AGENT_ID")
    environment_id = os.getenv("ANTHROPIC_ENVIRONMENT_ID")
    if not os.getenv("ANTHROPIC_API_KEY") or not agent_id or not environment_id:
        print("Set ANTHROPIC_API_KEY, ANTHROPIC_AGENT_ID and ANTHROPIC_ENVIRONMENT_ID in .env", file=sys.stderr)
        return 1

    client = anthropic.Anthropic()
    one_shot = " ".join(sys.argv[1:]).strip()

    try:
        session = client.beta.sessions.create(
            agent={"type": "agent", "id": agent_id},
            environment_id=environment_id,
        )
        print(f"Session {session.id}\n")

        if one_shot:
            return 0 if run_turn(client, session.id, one_shot) else 1

        while True:
            text = input("You: ").strip()
            if not text:
                continue
            print("\nAgent: ", end="", flush=True)
            if not run_turn(client, session.id, text):
                return 1
            print()
    except (KeyboardInterrupt, EOFError):
        print("\nBye.")
        return 0
    except anthropic.AuthenticationError:
        print("\nInvalid ANTHROPIC_API_KEY.", file=sys.stderr)
        return 1
    except anthropic.NotFoundError as exc:
        print(f"\nNot found (check the agent and environment IDs): {exc.message}", file=sys.stderr)
        return 1
    except anthropic.APIStatusError as exc:
        print(f"\nAnthropic API error ({exc.status_code}): {exc.message}", file=sys.stderr)
        return 1
    except anthropic.APIConnectionError:
        print("\nCould not reach the Anthropic API. Check your network connection.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
