"""Agent CRUD operations for the Maybank KB prompt agent.

Uses the NEW Foundry agent model (azure-ai-projects 2.1.0+) with:
- Versioned agents (not assistants)
- Unique Entra identity per agent
- Stable endpoint for M365 publishing
"""

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AgentDetails,
    AgentVersionDetails,
    PromptAgentDefinition,
    AzureAISearchTool,
    AzureAISearchToolResource,
    AISearchIndexResource,
    AzureAISearchQueryType,
)
from azure.identity import DefaultAzureCredential

from src.config import Config

AGENT_INSTRUCTIONS = (
    "You are Maybank's Knowledge Base Assistant. Your role is to help users "
    "find accurate information from Maybank's knowledge base articles.\n\n"
    "Guidelines:\n"
    "1. Always search the knowledge base before answering questions.\n"
    "2. Provide accurate, concise answers based ONLY on the retrieved articles.\n"
    "3. Always cite the source article title and URL when providing information.\n"
    "4. If the information is not found in the knowledge base, clearly state: "
    '"I could not find this information in the knowledge base. '
    'Please contact Maybank support at 1-300-88-6688."\n'
    "5. Be professional, helpful, and courteous.\n"
    "6. Do not make up or infer information that is not explicitly in the knowledge base.\n"
    "7. When multiple articles are relevant, synthesize the information and cite all sources."
)


def _get_credential():
    return DefaultAzureCredential()


def _get_client(config: Config) -> AIProjectClient:
    return AIProjectClient(
        endpoint=config.project_endpoint,
        credential=_get_credential(),
    )


def _resolve_connection_id(client: AIProjectClient, config: Config) -> str:
    """Resolve the full connection resource ID from the connection name."""
    conn = client.connections.get(config.search_connection_name)
    return conn.id


def _build_definition(config: Config, connection_id: str) -> PromptAgentDefinition:
    """Build the prompt agent definition with AI Search tool."""
    return PromptAgentDefinition(
        model=config.model_deployment_name,
        instructions=AGENT_INSTRUCTIONS,
        tools=[
            AzureAISearchTool(
                azure_ai_search=AzureAISearchToolResource(
                    indexes=[
                        AISearchIndexResource(
                            project_connection_id=connection_id,
                            index_name=config.search_index_name,
                            query_type=AzureAISearchQueryType.VECTOR_SEMANTIC_HYBRID,
                            top_k=5,
                        )
                    ]
                )
            )
        ],
        temperature=0.7,
        top_p=0.95,
    )


def create_agent(config: Config) -> AgentVersionDetails:
    """Create a new version of the prompt agent with Azure AI Search tool."""
    client = _get_client(config)
    connection_id = _resolve_connection_id(client, config)
    print(f"Resolved connection: {config.search_connection_name}")

    definition = _build_definition(config, connection_id)

    version = client.agents.create_version(
        agent_name=config.agent_name,
        definition=definition,
        description="Maybank Knowledge Base Assistant with Azure AI Search grounding.",
    )

    print(f"Agent version created: {version.name} v{version.version} (status: {version.status})")
    return version


def get_agent(config: Config) -> AgentDetails | None:
    """Get existing agent by name. Returns None if not found."""
    client = _get_client(config)
    try:
        return client.agents.get(agent_name=config.agent_name)
    except Exception:
        return None


def delete_agent(config: Config) -> None:
    """Delete an agent entirely."""
    client = _get_client(config)
    client.agents.delete(agent_name=config.agent_name)
    print(f"Agent deleted: {config.agent_name}")


def deploy_or_update(config: Config) -> AgentVersionDetails:
    """Deploy the agent. Creates a new version (new model supports versioning natively)."""
    existing = get_agent(config)
    if existing:
        print(f"Found existing agent: {existing.name}")
        print("Creating new version...")
    else:
        print(f"Creating new agent: {config.agent_name}")

    return create_agent(config)
