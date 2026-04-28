"""Interactive streaming chat with the Contoso Bank KB Agent (New Foundry Model).

Uses the OpenAI Responses API with agent_reference for the new-model agent.

Usage:
    python scripts/chat.py

Type your questions and see streamed responses in real-time.
Type 'quit' or 'exit' to end the session.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

from src.config import get_config


def stream_response(openai_client, agent_name: str, question: str) -> None:
    """Send a question and stream the response token-by-token."""
    stream = openai_client.responses.create(
        stream=True,
        model="gpt-4o-mini",
        input=question,
        extra_body={"agent_reference": {"name": agent_name, "type": "agent_reference"}},
    )

    citations = []
    for event in stream:
        if event.type == "response.output_text.delta":
            print(event.delta, end="", flush=True)
        elif event.type == "response.output_item.done":
            if (
                event.item.type == "message"
                and event.item.content
                and event.item.content[-1].type == "output_text"
            ):
                for ann in event.item.content[-1].annotations:
                    if ann.type == "url_citation":
                        citations.append(ann.url)

    print()  # newline after streamed response
    if citations:
        print("\n📎 Citations:")
        for url in citations:
            print(f"   {url}")


def main():
    config = get_config()

    print("=" * 60)
    print("  Contoso Bank KB Agent — Interactive Chat (Streaming)")
    print("  (New Foundry Model — Responses API)")
    print("=" * 60)
    print(f"  Agent : {config.agent_name}")
    print(f"  Model : {config.model_deployment_name}")
    print(f"  Index : {config.search_index_name}")
    print(f"  Type 'quit' to exit")
    print("=" * 60)

    project_client = AIProjectClient(
        endpoint=config.project_endpoint,
        credential=DefaultAzureCredential(),
    )

    # Verify agent exists in the new model
    try:
        agent = project_client.agents.get(agent_name=config.agent_name)
        print(f"  Agent found: {agent.name}")
    except Exception:
        print(f"\n❌ Agent '{config.agent_name}' not found. Run create_new_agent.py first.")
        sys.exit(1)

    openai_client = project_client.get_openai_client()

    while True:
        print()
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not question:
            continue
        if question.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break

        print("\nAgent: ", end="", flush=True)
        try:
            stream_response(openai_client, config.agent_name, question)
        except Exception as e:
            print(f"\n❌ Error: {e}")


if __name__ == "__main__":
    main()
