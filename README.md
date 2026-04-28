# Contoso Bank KB Agent — Azure Foundry Prompt Agent

A Python project that creates and manages an Azure Foundry **prompt agent** grounded on knowledge base articles via **Azure AI Search**. The agent is published to **M365 Agents** (Teams / Microsoft 365 Copilot).

## Architecture

```
┌──────────────────┐      ┌───────────────────────┐      ┌─────────────────────┐
│  M365 Agents     │ ──── │  Azure Foundry         │ ──── │  Azure AI Search    │
│  (Teams/Copilot) │      │  Prompt Agent          │      │  (Vector Index)     │
│                  │      │  contoso-kb-agent       │      │  kb-articles-index  │
└──────────────────┘      └───────────────────────┘      └─────────────────────┘
                                    │
                                    ▼
                          ┌───────────────────────┐
                          │  Model: gpt-4o-mini    │
                          └───────────────────────┘
```

## Project Structure

```
contoso-kb-agent/
├── src/
│   ├── __init__.py
│   ├── config.py            # Configuration from environment variables
│   ├── agent.py             # Agent CRUD operations (create, update, delete, get)
│   └── search_setup.py      # Search index creation + KB document ingestion
├── tests/
│   └── test_agent.py        # Smoke tests and response validation
├── scripts/
│   ├── setup_infra.ps1      # One-time infrastructure setup (search, RBAC, connection)
│   ├── deploy_agent.py      # Deploy/update the agent (validates config, creates agent)
│   └── ingest_kb.py         # One-time/on-demand KB data ingestion
├── data/
│   └── kb_articles.json     # Sample KB articles
├── .foundry/
│   └── agent-metadata.yaml  # Foundry workspace metadata
├── .env.example             # Environment variable template
├── requirements.txt
└── README.md
```

## Prerequisites

- Python 3.10+
- Azure CLI (`az`) + Azure Developer CLI (`azd`)
- Azure subscription with Owner/Contributor role
- Provisioned Foundry project (see `scripts/setup_infra.ps1`)

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your actual values
```

### 3. One-time infrastructure setup

```powershell
# Provisions AI Search, RBAC, and project connection
.\scripts\setup_infra.ps1
```

### 4. Ingest knowledge base data

```bash
python scripts/ingest_kb.py
```

### 5. Deploy the agent

```bash
python scripts/deploy_agent.py
```

### 6. Test the agent

```bash
python -m pytest tests/test_agent.py -v
```

## Publishing to M365 Agents

See [scripts/publish_m365.md](scripts/publish_m365.md) for step-by-step instructions to publish the agent to Microsoft Teams and M365 Copilot.

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Description |
|----------|-------------|
| `PROJECT_ENDPOINT` | Foundry project endpoint URL |
| `MODEL_DEPLOYMENT_NAME` | Deployed model name (e.g., `gpt-4o-mini`) |
| `SEARCH_CONNECTION_NAME` | Foundry project connection to AI Search |
| `SEARCH_INDEX_NAME` | AI Search index name |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI endpoint (for embeddings) |
