"""Smoke tests for the Maybank KB Agent — validates search grounding and response quality.

Uses the NEW Foundry agent model (azure-ai-projects 2.1.0+) with Responses API.
"""

import pytest
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_config


@pytest.fixture(scope="module")
def project_client():
    config = get_config()
    return AIProjectClient(
        endpoint=config.project_endpoint,
        credential=DefaultAzureCredential(),
    )


@pytest.fixture(scope="module")
def openai_client(project_client):
    return project_client.get_openai_client()


@pytest.fixture(scope="module")
def agent_name(project_client):
    config = get_config()
    try:
        agent = project_client.agents.get(agent_name=config.agent_name)
        return agent.name
    except Exception:
        pytest.skip(f"Agent '{config.agent_name}' not found — run create_new_agent.py first")


def _ask_agent(openai_client, agent_name: str, question: str) -> str:
    """Send a question to the agent via Responses API and return the text response."""
    response = openai_client.responses.create(
        model="gpt-4o-mini",
        input=question,
        extra_body={"agent_reference": {"name": agent_name, "type": "agent_reference"}},
    )

    for item in response.output:
        if item.type == "message":
            for content in item.content:
                if content.type == "output_text":
                    return content.text
    return ""


class TestKBSearchGrounding:
    """Tests that verify the agent retrieves and cites KB articles."""

    def test_account_opening(self, openai_client, agent_name):
        response = _ask_agent(openai_client, agent_name, "How do I open a savings account?")
        assert response, "Agent returned empty response"
        response_lower = response.lower()
        assert any(
            term in response_lower for term in ["nric", "passport", "rm250", "maybank2u"]
        ), f"Response missing expected KB content: {response[:200]}"

    def test_credit_card_requirements(self, openai_client, agent_name):
        response = _ask_agent(
            openai_client, agent_name, "What are the requirements for a credit card?"
        )
        assert response, "Agent returned empty response"
        response_lower = response.lower()
        assert any(
            term in response_lower for term in ["21 years", "rm24,000", "salary slips"]
        ), f"Response missing expected KB content: {response[:200]}"

    def test_citation_presence(self, openai_client, agent_name):
        response = _ask_agent(openai_client, agent_name, "Tell me about internet banking registration")
        assert response, "Agent returned empty response"
        assert any(
            term in response.lower() for term in ["kb.maybank.com", "source", "internet-banking"]
        ), f"Response missing citations: {response[:300]}"

    def test_no_answer_graceful(self, openai_client, agent_name):
        response = _ask_agent(
            openai_client, agent_name, "What is the weather in Kuala Lumpur today?"
        )
        assert response, "Agent returned empty response"
        response_lower = response.lower()
        assert any(
            term in response_lower
            for term in ["could not find", "not found", "knowledge base", "1-300-88-6688"]
        ), f"Agent should gracefully decline off-topic questions: {response[:200]}"


class TestAgentBasics:
    """Basic agent health checks."""

    def test_agent_exists(self, project_client, agent_name):
        agent = project_client.agents.get(agent_name=agent_name)
        assert agent.name == get_config().agent_name

    def test_agent_has_search_tool(self, project_client, agent_name):
        agent = project_client.agents.get(agent_name=agent_name)
        versions = agent.versions
        latest = versions.get("latest", {})
        definition = latest.get("definition", {})
        tools = definition.get("tools", [])
        tool_types = [t.get("type") for t in tools]
        assert "azure_ai_search" in tool_types, f"Expected azure_ai_search tool, got: {tool_types}"
