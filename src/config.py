"""Configuration module — loads settings from environment variables or .env file."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / ".env")


@dataclass
class Config:
    """Application configuration resolved from environment variables."""

    project_endpoint: str
    model_deployment_name: str
    search_connection_name: str
    search_index_name: str
    azure_openai_endpoint: str
    embedding_deployment_name: str
    agent_name: str

    @classmethod
    def from_env(cls) -> "Config":
        """Create Config from environment variables. Raises ValueError for missing required vars."""
        required = {
            "PROJECT_ENDPOINT": os.getenv("PROJECT_ENDPOINT"),
            "MODEL_DEPLOYMENT_NAME": os.getenv("MODEL_DEPLOYMENT_NAME"),
            "SEARCH_CONNECTION_NAME": os.getenv("SEARCH_CONNECTION_NAME"),
            "SEARCH_INDEX_NAME": os.getenv("SEARCH_INDEX_NAME"),
            "AZURE_OPENAI_ENDPOINT": os.getenv("AZURE_OPENAI_ENDPOINT"),
            "EMBEDDING_DEPLOYMENT_NAME": os.getenv("EMBEDDING_DEPLOYMENT_NAME"),
            "AGENT_NAME": os.getenv("AGENT_NAME"),
        }

        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        return cls(
            project_endpoint=required["PROJECT_ENDPOINT"],
            model_deployment_name=required["MODEL_DEPLOYMENT_NAME"],
            search_connection_name=required["SEARCH_CONNECTION_NAME"],
            search_index_name=required["SEARCH_INDEX_NAME"],
            azure_openai_endpoint=required["AZURE_OPENAI_ENDPOINT"],
            embedding_deployment_name=required["EMBEDDING_DEPLOYMENT_NAME"],
            agent_name=required["AGENT_NAME"],
        )


def get_config() -> Config:
    """Convenience function to load config."""
    return Config.from_env()
