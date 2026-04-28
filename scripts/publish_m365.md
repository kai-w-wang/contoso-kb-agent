# Publishing Contoso Bank KB Agent to M365 Agents

## Overview

Once the Foundry prompt agent is tested and working, publish it to **M365 Agents** so users can access it in **Microsoft Teams** and **Microsoft 365 Copilot**.

## Option 1: Publish via Azure AI Foundry Portal (Recommended)

### Steps

1. **Open Azure AI Foundry portal**
   - Navigate to [https://ai.azure.com](https://ai.azure.com)
   - Select your project: `ai-project-contoso-kb-agent`

2. **Navigate to Agents**
   - Go to **Build** → **Agents**
   - Select `contoso-kb-agent`

3. **Publish to M365**
   - Click **Deploy** → **Publish to Microsoft 365**
   - Configure the agent manifest:
     - **Display name**: `Contoso Bank KB Assistant`
     - **Short description**: `Search Contoso Bank knowledge base articles`
     - **Full description**: `An AI assistant that helps you find information from Contoso Bank's knowledge base. Ask about account opening, credit cards, loans, internet banking, and more.`
     - **Icon**: Upload a custom icon (recommended 192x192 PNG)
     - **Accent color**: `#FFC107` (Contoso Bank brand color)

4. **Submit for Admin Approval**
   - The app will appear in **Teams Admin Center** → **Manage Apps** (pending approval)
   - A tenant admin must approve the app

5. **Distribution**
   - Once approved, users find the agent in:
     - Teams → Apps → Organization apps
     - M365 Copilot → Available agents

## Option 2: Declarative Agent via Teams Toolkit

For more control over the Teams app package, create a **declarative agent** manifest.

### Project Structure

```
teams-app/
├── appPackage/
│   ├── manifest.json
│   ├── declarativeAgent.json
│   ├── color.png          (192x192)
│   └── outline.png        (32x32)
```

### manifest.json

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/teams/vDevPreview/MicrosoftTeams.schema.json",
  "manifestVersion": "devPreview",
  "version": "1.0.0",
  "id": "{{APP_ID}}",
  "developer": {
    "name": "Contoso Bank",
    "websiteUrl": "https://www.contoso-bank.com",
    "privacyUrl": "https://www.contoso-bank.com/privacy",
    "termsOfUseUrl": "https://www.contoso-bank.com/terms"
  },
  "name": {
    "short": "Contoso Bank KB Assistant",
    "full": "Contoso Bank Knowledge Base Assistant"
  },
  "description": {
    "short": "Search Contoso Bank knowledge base articles",
    "full": "An AI assistant that helps you find information from Contoso Bank's knowledge base. Ask about account opening, credit cards, loans, internet banking, and more."
  },
  "icons": {
    "color": "color.png",
    "outline": "outline.png"
  },
  "accentColor": "#FFC107",
  "copilotAgents": {
    "declarativeAgents": [
      {
        "id": "contoso-kb-agent",
        "file": "declarativeAgent.json"
      }
    ]
  }
}
```

### declarativeAgent.json

```json
{
  "$schema": "https://aka.ms/json-schemas/copilot/declarative-agent/v1.3/schema.json",
  "version": "v1.3",
  "name": "Contoso Bank KB Assistant",
  "description": "Searches Contoso Bank knowledge base to answer your questions",
  "instructions": "You are Contoso Bank's Knowledge Base Assistant. Search the knowledge base and provide accurate, cited answers. If information is not found, direct users to Contoso Bank support at 1-300-88-6688.",
  "conversation_starters": [
    { "title": "Open an account", "text": "How do I open a new savings account at Contoso Bank?" },
    { "title": "Credit card", "text": "What are the requirements for a Contoso Bank credit card?" },
    { "title": "Internet banking", "text": "How do I register for Contoso Online Banking internet banking?" },
    { "title": "Foreign exchange", "text": "How can I send money internationally via Contoso Bank?" },
    { "title": "Loan help", "text": "What options are available for loan restructuring?" }
  ]
}
```

### Deployment Steps

1. Install Teams Toolkit: `npm install -g @microsoft/teamsapp-cli`
2. Login: `teamsapp auth login m365`
3. Package: `teamsapp package --env dev`
4. Publish: `teamsapp publish --env dev`
5. Admin approves in Teams Admin Center

## Prerequisites for M365 Publishing

| Requirement | Details |
|-------------|---------|
| **M365 license** | Microsoft 365 E3/E5 or equivalent |
| **Copilot license** | Microsoft 365 Copilot license (for Copilot integration) |
| **Admin access** | Tenant admin must approve the app |
| **Agent tested** | Run `pytest tests/test_agent.py` — all tests should pass |

## Post-Publishing Checklist

- [ ] Verify agent appears in Teams app catalog
- [ ] Test agent in Teams chat
- [ ] Test agent in M365 Copilot (if Copilot-licensed)
- [ ] Verify search grounding works end-to-end
- [ ] Monitor usage via Application Insights
