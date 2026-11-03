"""Create or update the shopping assistant agent and its environment.

The first run creates both and saves their IDs to .env. Later runs update the same
resources in place (the agent gets a new version), so run it again after editing
agent/system-prompt.md or the site list below.

    python setup_agent.py            # create or update
    python setup_agent.py --dry-run  # print what would be sent, change nothing
"""

import json
import os
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv, set_key

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"

AGENT_NAME = "Shopping assistant"
AGENT_DESCRIPTION = "Researches products across Indian stores and returns a shortlist with prices and links."
MODEL = {"id": "claude-opus-5-5", "effort": "medium"}

# The only sites the agent can search and open. Each entry covers its subdomains for the
# web tools; keep this list at 64 entries or fewer.
SITES = [
    # Stores
    "amazon.in",
    "flipkart.com",
    "croma.com",
    "reliancedigital.in",
    "vijaysales.com",
    "tatacliq.com",
    "jiomart.com",
    "myntra.com",
    "ajio.com",
    "nykaa.com",
    "meesho.com",
    "snapdeal.com",
    "pepperfry.com",
    "decathlon.in",
    "firstcry.com",
    "bigbasket.com",
    "ikea.com",
    # Specs, prices, and reviews
    "smartprix.com",
    "91mobiles.com",
    "gsmarena.com",
    "mysmartprice.com",
    "digit.in",
    "gadgets360.com",
    "rtings.com",
    "notebookcheck.net",
    "techradar.com",
]


def environment_config() -> dict:
    # Under limited networking, allowed_hosts also governs the web tools. A bare host there
    # matches only itself, so list the www-style subdomains too.
    hosts = [host for site in SITES for host in (site, f"*.{site}")]
    return {
        "type": "cloud",
        "networking": {
            "type": "limited",
            "allowed_hosts": hosts,
            "allow_package_managers": False,
            "allow_mcp_servers": False,
        },
    }


def agent_tools() -> list[dict]:
    # Only the two web tools are on. They can reach nothing outside SITES, and run without
    # pausing for approval since nobody approves calls from the app.
    allow = {"type": "always_allow"}
    return [
        {
            "type": "agent_toolset_20260401",
            "default_config": {"enabled": False, "permission_policy": allow},
            "configs": [
                {
                    "name": "web_search",
                    "enabled": True,
                    "permission_policy": allow,
                    "allowed_domains": SITES,
                    "user_location": {"type": "approximate", "country": "IN", "timezone": "Asia/Kolkata"},
                },
                {
                    "name": "web_fetch",
                    "enabled": True,
                    "permission_policy": allow,
                    "allowed_domains": SITES,
                    "max_content_tokens": 30000,
                },
            ],
        }
    ]


def main() -> None:
    load_dotenv(ENV_FILE)
    system = (ROOT / "agent" / "system-prompt.md").read_text(encoding="utf-8").strip()

    if "--dry-run" in sys.argv:
        print(json.dumps({"environment": environment_config(), "agent_tools": agent_tools(), "model": MODEL}, indent=2))
        print(f"\nSystem prompt: {len(system)} characters from agent/system-prompt.md")
        return

    client = anthropic.Anthropic()

    try:
        environment_id = os.getenv("ANTHROPIC_ENVIRONMENT_ID")
        if environment_id:
            client.beta.environments.update(environment_id, config=environment_config())
            print(f"Updated environment {environment_id}")
        else:
            environment = client.beta.environments.create(name="shopping-assistant-env", config=environment_config())
            environment_id = environment.id
            set_key(str(ENV_FILE), "ANTHROPIC_ENVIRONMENT_ID", environment_id)
            print(f"Created environment {environment_id}")

        fields = {
            "name": AGENT_NAME,
            "description": AGENT_DESCRIPTION,
            "model": MODEL,
            "system": system,
            "tools": agent_tools(),
        }
        agent_id = os.getenv("ANTHROPIC_AGENT_ID")
        if agent_id:
            agent = client.beta.agents.update(agent_id, **fields)
            print(f"Updated agent {agent.id} (version {agent.version})")
        else:
            agent = client.beta.agents.create(**fields)
            set_key(str(ENV_FILE), "ANTHROPIC_AGENT_ID", agent.id)
            print(f"Created agent {agent.id} (version {agent.version})")
    except anthropic.APIStatusError as exc:
        # A 400 here usually names a rejected site, e.g. "allowed_domains.3: ..."; remove it from SITES.
        sys.exit(f"Anthropic API error ({exc.status_code}): {exc.message}")

    print("IDs are saved in .env. Start the app with: python run_backend.py")


if __name__ == "__main__":
    main()
