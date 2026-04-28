"""Deploy or update the Contoso Bank KB prompt agent (New Foundry Model).

Usage:
    python scripts/deploy_agent.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_config
from src.agent import deploy_or_update


def main():
    print("=" * 60)
    print("Contoso Bank KB Agent — Deploy (New Foundry Model)")
    print("=" * 60)

    config = get_config()
    print(f"\nProject endpoint : {config.project_endpoint}")
    print(f"Model            : {config.model_deployment_name}")
    print(f"Search connection: {config.search_connection_name}")
    print(f"Search index     : {config.search_index_name}")
    print(f"Agent name       : {config.agent_name}")
    print()

    version = deploy_or_update(config)

    print(f"\n{'=' * 60}")
    print(f"Deployment complete!")
    print(f"  Agent name : {version.name}")
    print(f"  Version    : {version.version}")
    print(f"  Status     : {version.status}")
    print(f"{'=' * 60}")
    print(f"\nNext steps:")
    print(f"  1. Test:    python -m pytest tests/test_agent.py -v")
    print(f"  2. Chat:    python scripts/chat.py")
    print(f"  3. Publish: Go to ai.azure.com → Publish → Publish to Teams and M365 Copilot")


if __name__ == "__main__":
    main()
