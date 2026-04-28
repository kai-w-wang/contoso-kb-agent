# Implementation Challenges & Resolutions
## M365 Declarative Agent with Azure AI Foundry — Private Network

> **Timeline**: 2 days (April 25-26, 2026)  
> **Purpose**: Document real implementation challenges encountered during build — essential awareness for project teams adopting this pattern.

---

## Summary

Building this pattern from scratch took **~16 hours across 2 days** with **4 major phases**. Each phase surfaced challenges that are not documented in Microsoft's official guides. This document captures every significant blocker, workaround, and resolution — organized by phase.

| Phase | Duration | Major Blockers |
|-------|----------|----------------|
| 1. Foundry Agent + AI Search | ~3 hours | SDK mismatches, connection auth, query type failures |
| 2. M365 Declarative Agent + API Plugin | ~4 hours | Schema bugs, upload location confusion, EasyAuth conflict, timeout |
| 3. VNet + APIM Enterprise Security | ~4 hours | VM quota = 0, EasyAuth + auth:None conflict, private endpoint breaks agent |
| 4. Private Foundry VNet Deployment | ~5 hours | Subnet locks, account name constraints, RBAC propagation, bootstrap paradox |

---

## Phase 1: Foundry Agent with AI Search

### Challenge 1.1: Classic vs New Agent API
**Problem**: Started building with the classic Assistants API (`AgentsClient.create_agent`). Discovered that M365 publishing requires the **new Foundry agent model** with versioning support.

**Impact**: Had to rewrite agent creation, invocation, and test code.

**Resolution**: Migrated to `AIProjectClient.agents.create_version()` with `PromptAgentDefinition`. The new API supports agent versioning, unique Entra identity, and the `agent_reference` protocol needed by the Function bridge.

**Customer Awareness**: Start with the new agent model from day one. The classic Assistants API is being deprecated for new development.

---

### Challenge 1.2: SDK Parameter Name Inconsistencies
**Problem**: The Azure AI SDK has inconsistent parameter names across versions and APIs:
- `index_connection_id` (old assistants API) vs `project_connection_id` (new agent versions API)
- `agent` (deprecated, returns 400) vs `agent_reference` (correct) in `extra_body`
- `AzureAISearchToolDefinition()` takes NO parameters — search config goes in `tool_resources`

**Impact**: Multiple 400 errors during agent creation until correct parameter names discovered through trial and error.

**Resolution**: Documented the correct parameter mapping:
```
Assistants API:  index_connection_id  →  Agent Versions API:  project_connection_id
extra_body:      "agent" (deprecated)  →  "agent_reference" (correct)
```

**Customer Awareness**: SDK documentation may lag behind API changes. Test with REST API first to validate payloads before writing SDK code.

---

### Challenge 1.3: Search Query Type Failures
**Problem**: `VECTOR_SEMANTIC_HYBRID` and `SEMANTIC` query types failed with `tool_user_error` even though the index had a vectorizer and semantic configuration.

**Root Cause**: The search index vectorizer had `apiKey: null` — when using AAD auth on the Foundry connection, the vectorizer couldn't authenticate to the embedding endpoint.

**Resolution**: Used `SIMPLE` query type which works reliably. For vector/semantic, the vectorizer needs explicit credentials or the search service MI needs `Cognitive Services OpenAI User` on the Foundry account.

**Customer Awareness**: Always validate search query types independently before configuring the agent. Simple query works out of the box; vector/semantic requires additional auth setup.

---

### Challenge 1.4: Token Scope Confusion
**Problem**: Manual token acquisition with `https://ai.azure.com/.default` scope worked for REST API calls but **failed** when used with `AIProjectClient`.

**Root Cause**: The SDK internally uses `https://cognitiveservices.azure.com/.default` as the token audience, not `https://ai.azure.com/.default`.

**Resolution**: Use `AIProjectClient(endpoint, credential).get_openai_client()` which handles token scope automatically. Never manually acquire tokens for SDK-based calls.

**Customer Awareness**: Different Foundry APIs use different token audiences. Let the SDK handle token acquisition.

---

## Phase 2: M365 Declarative Agent & API Plugin

### Challenge 2.1: Upload Location Confusion
**Problem**: Spent time trying to upload the agent ZIP to **M365 Admin Center → Settings → Integrated Apps**. The upload succeeded but the agent never appeared in Copilot.

**Root Cause**: Declarative agents must be uploaded via **M365 Admin Center → Copilot → Agents** — a completely different section.

**Resolution**: Uploaded to the correct location. Agent appeared in Copilot immediately.

**Customer Awareness**: The "Integrated Apps" section is for traditional Teams apps. Copilot declarative agents have their own dedicated upload path under **Copilot → Agents**.

---

### Challenge 2.2: Manifest Schema Version Bugs
**Problem**: Using manifest schema `v1.26` (latest at the time) caused validation failures with error: `anyOf: []` on `copilotAgents`.

**Root Cause**: Schema v1.26 has a known bug in the `copilotAgents` property definition.

**Resolution**: Downgraded to schema `v1.19` which is stable and fully supports declarative agents.

**Customer Awareness**: Latest schema version ≠ most stable. Use `v1.19` for declarative agents until Microsoft confirms newer versions are fixed.

---

### Challenge 2.3: API Plugin Response Semantics Format
**Problem**: Setting `response_semantics.properties.title` to `{"static_value": "Contoso Bank KB Answer"}` (as per some documentation examples) caused upload failure.

**Resolution**: Values must be **plain strings**: `"title": "Contoso Bank KB Answer"`, NOT objects.

**Customer Awareness**: The API plugin schema documentation has inconsistencies between examples and actual validation. Test uploads incrementally.

---

### Challenge 2.4: EasyAuth Blocks Copilot Calls
**Problem**: After enabling Azure App Service EasyAuth on the Function, Copilot could not reach the API. Error: "We couldn't load the info about this agent."

**Root Cause**: The API plugin was configured with `auth.type: "None"`. Copilot sends unauthenticated API calls, but EasyAuth returns 401 for all unauthenticated requests.

**Resolution**: Disabled EasyAuth. For production, configure OAuth properly in both EasyAuth AND api-plugin.json, or use APIM as the auth gateway.

**Customer Awareness**: EasyAuth and API plugin auth must be **aligned**. If plugin uses `auth: None`, EasyAuth must be off (or allow anonymous). If plugin uses OAuth, EasyAuth must validate the same token.

---

### Challenge 2.5: No Interactive Adaptive Cards
**Problem**: Built a feedback form with `Action.Submit`, `Input.ChoiceSet`, and `Input.Text` in the `static_template`. The card rendered but buttons did nothing.

**Root Cause**: API Plugin `static_template` supports **display-only** Adaptive Cards. Interactive elements (Submit, Input) are NOT supported — they require Bot Framework (Option A architecture).

**Resolution**: Changed feedback to text-based prompts ("Say 'that was helpful' to give feedback") which Copilot routes to the `submitFeedback` function.

**Customer Awareness**: This is a fundamental architectural constraint. If your use case needs interactive forms, you must use Bot Framework (Option A) instead of API Plugin (Option B).

---

### Challenge 2.6: Copilot Timeout (~10-15 seconds)
**Problem**: Cold-start Function responses took 12-15 seconds, causing Copilot to timeout intermittently.

**Resolution**: Added a **timer trigger warm-up** in the Function that fires every 4 minutes to keep the Function warm. Reduced response time from 12-15s to ~5-7s.

**Customer Awareness**: Budget your total response time under 10 seconds. Account for: Function cold start (0-3s), token acquisition (~500ms), Foundry agent + search (~3-5s), GPT inference (~2-4s). Use always-ready instances or warm-up triggers.

---

## Phase 3: VNet + APIM Enterprise Security

### Challenge 3.1: VM Quota = Zero
**Problem**: The subscription had **zero VM quota** for all SKU families in East US. Traditional VNet integration for Azure Functions requires an App Service Plan (which needs VMs).

**Impact**: Could not create any VM-backed resources. Quota increase request was pending.

**Resolution**: Discovered **Flex Consumption** plan which supports VNet integration via `Microsoft.App/environments` delegation — **no VMs needed**. Deleted the old Function and recreated on Flex Consumption.

**Customer Awareness**: Check VM quota before planning VNet integration. Flex Consumption is the recommended approach as it avoids quota issues entirely. Traditional App Service plans (B1/P1) require VM quota.

---

### Challenge 3.2: Private Endpoint Breaks Foundry Agent Search Tool
**Problem**: After creating a private endpoint for AI Search, the Foundry agent's built-in `azure_ai_search` tool started failing with `tool_user_error: "Invalid endpoint or connection failed"`.

**This happened even though AI Search still had `publicNetworkAccess: Enabled`.**

**Root Cause**: The Foundry agent runtime (when running on Microsoft's shared infrastructure, NOT in a VNet) resolves DNS through shared infrastructure. When a private DNS zone exists with an A record for the search service, the shared infrastructure picks up the private IP — but can't reach it because it's not in the VNet.

**Resolution (immediate)**: Deleted the private endpoint. Agent worked again instantly.

**Resolution (permanent)**: Deployed Foundry inside the VNet with a capability host (Phase 4). Once Foundry is in the VNet, its runtime resolves DNS correctly through the VNet's private DNS zones.

**Customer Awareness**: **This is the most critical gotcha in this architecture.** You CANNOT add private endpoints to AI Search while using a public (non-VNet) Foundry account. You must deploy Foundry with a capability host inside the VNet FIRST, then add private endpoints.

---

### Challenge 3.3: APIM Provisioning Time
**Problem**: APIM Developer tier took **45 minutes** to provision. During this time, no configuration could be applied.

**Customer Awareness**: Plan for 30-45 minutes for APIM provisioning (Developer tier). Premium tier can take up to 60 minutes. Factor this into deployment scripts and CI/CD pipelines.

---

## Phase 4: Private Foundry VNet Deployment

### Challenge 4.1: Storage Account Name Constraints from Bicep Template
**Problem**: The official Bicep template for private Foundry (`foundry-samples/15-private-network-standard-agent-setup`) generates a storage account name by appending the `aiServices` parameter name with a unique suffix. Storage names must be ≤24 chars, lowercase, no hyphens.

**First attempt**: `aiServices = "ai-contoso-private"` → storage name too long and contained hyphens.  
**Second attempt**: `aiServices = "ai-mbk-pvt"` → still contained hyphens.  
**Third attempt**: `aiServices = "aimbkpvt"` → worked (8 chars, no hyphens).

**Customer Awareness**: Keep the `aiServices` parameter **short** (≤8 chars) and use **only lowercase letters/numbers, no hyphens**. The template appends a unique suffix that adds ~5 chars.

---

### Challenge 4.2: Subnet Service Association Lock After Failed Deployment
**Problem**: After a failed capability host deployment, the subnet retained a `serviceAssociationLink/legionservicelink` that prevented reuse. The Foundry account was deleted and purged, but the subnet link persisted for **up to 20 minutes**.

**Impact**: Could not redeploy to the same subnet. Creating a new account on the locked subnet also failed.

**Resolution**: Created a **new subnet** (`subnet-agent-v2` at `10.0.5.0/24`) instead of waiting for the lock to release.

**Customer Awareness**: If a Foundry deployment fails, the subnet may be locked for 15-20 minutes. Always have a backup subnet address range planned. Do not attempt to reuse the same subnet immediately after a failed deployment.

---

### Challenge 4.3: The Bootstrap Paradox — Private Account Needs Public Access for Setup
**Problem**: The Foundry account was deployed with `publicNetworkAccess: Disabled`. But agent creation requires REST API calls from a developer machine, which is outside the VNet.

**Impact**: RBAC was correctly assigned but all API calls returned `403: Access denied due to Virtual Network/Firewall rules`.

**Resolution**: Temporarily set `publicNetworkAccess: Enabled` and `networkAcls.defaultAction: Allow` via ARM REST API, created the agent and version, then locked down to private again.

**Customer Awareness**: Plan for a **bootstrap phase** where the Foundry account temporarily allows public access. Document this as an operational procedure with mandatory lock-down after setup. Alternatively, use a jumpbox VM inside the VNet for all setup operations.

---

### Challenge 4.4: RBAC Propagation Delays
**Problem**: After assigning `Azure AI Developer` role on the Foundry account, agent creation calls returned `PermissionDenied` for **30-60 seconds**.

**Additional complication**: Needed roles on both the **account** level AND the **project** level. Account-level roles alone were insufficient.

**Resolution**: Wait 60 seconds after role assignment before attempting API calls. Assign roles at both account and project scope.

**Customer Awareness**: RBAC propagation takes 30-120 seconds. In automated deployment scripts, add explicit wait periods after role assignments. Always assign roles at both account and project scope for Foundry operations.

---

### Challenge 4.5: Agent Version Required for agent_reference
**Problem**: Created the agent via assistants API (`POST /assistants`). The Function's `agent_reference` lookup returned `404: Agent contoso-kb-agent with version  not found`.

**Root Cause**: The `agent_reference` protocol resolves agents by **name + version**, not by assistant ID. An agent version must be explicitly created via the agent versions API.

**Resolution**: Created an agent version via `POST /agents/contoso-kb-agent/versions` with the full definition including `kind: "prompt"`.

**Additional complication**: The agent versions API uses a **different tool schema** than the assistants API:
- Assistants: `tools: [{"type": "azure_ai_search"}]` + `tool_resources: {azure_ai_search: {indexes: [...]}}`
- Agent versions: `tools: [{"type": "azure_ai_search", "azure_ai_search": {"indexes": [...]}}]` (inline)

And the API version must be `2025-05-15-preview` (not `2025-05-01`).

**Customer Awareness**: Always create an agent version after creating the assistant. The assistants API and agent versions API have different schemas — test both independently.

---

## Cross-Cutting Challenges

### Challenge C.1: Connection Auth — AAD vs API Key
**Problem**: Initial AAD-auth connection to AI Search caused "Forbidden" errors despite correct RBAC. Switched to API Key auth which worked. Later, for the private deployment, switched back to AAD auth with workspace managed identity which worked correctly.

**Root Cause**: The initial AAD connection may have had incorrect identity configuration, or RBAC wasn't fully propagated.

**Customer Awareness**: API Key connections are simpler for POC but create key rotation burden. AAD connections with managed identity are preferred for production but require careful RBAC setup. Always use `listSecrets` API to verify connection credentials were stored correctly (ARM GET always shows `credentials: null`).

---

### Challenge C.2: Function Environment Variable Reload
**Problem**: After updating Function app settings (e.g., `PROJECT_ENDPOINT`), a simple restart did not pick up the new values. The Function kept using the old endpoint.

**Root Cause**: Python Functions with Flex Consumption plan load environment variables at **module import time**. A restart may reuse the existing container.

**Resolution**: **Stop** the Function completely, wait 5 seconds, then **start** it. This forces a fresh container with new environment variables.

**Customer Awareness**: For configuration changes, always `stop` then `start` — never just `restart`. This applies to Flex Consumption and Consumption plans.

---

### Challenge C.3: Multiple API Versions Required
**Problem**: Different Foundry operations require different API versions, and using the wrong version returns cryptic errors.

| Operation | API Version | Error if Wrong |
|-----------|------------|----------------|
| Agent creation (assistants) | `2025-05-01` | 400 |
| Agent versions | `2025-05-15-preview` | `UnsupportedApiVersion` |
| Connections (ARM) | `2025-04-01-preview` | 405 Method Not Allowed |
| Connections (data plane) | `v1` | Read-only (no write) |
| Search index | `2024-07-01` | — |

**Customer Awareness**: Pin API versions explicitly in all code and scripts. Do not use `latest` or assume consistency across Foundry operations.

---

## Timeline Summary

```
Day 1 (April 25):
  09:00 - 12:00  Phase 1: Foundry agent + AI Search (working)
  12:00 - 16:00  Phase 2: M365 declarative agent + API plugin (working in Copilot)
  16:00 - 20:00  Phase 3: VNet + APIM setup (APIM provisioning, VM quota blocked)

Day 2 (April 26):
  00:00 - 03:00  Phase 3 continued: Flex Consumption migration, APIM config, E2E working
  03:00 - 08:00  Phase 4: Private Foundry deployment (3 failed attempts, then success)
  08:00 - 08:30  Final lockdown + E2E validation (fully private pipeline confirmed)
```

---

## Recommendations for Project Teams

1. **Start with the new agent model** — skip the classic Assistants API entirely
2. **Plan VNet architecture before deploying anything** — retrofitting is painful
3. **Deploy Foundry with capability host BEFORE adding private endpoints** to other services
4. **Keep backup subnet address ranges** — failed deployments lock subnets
5. **Use Flex Consumption** for Functions — avoids VM quota issues
6. **Budget 10s total response time** — use warm-up triggers and always-ready instances
7. **Test manifest uploads incrementally** — schema validation errors are cryptic
8. **Document the bootstrap procedure** — private accounts need temporary public access
9. **Pin all API versions** — different Foundry operations need different versions
10. **Allow 60+ seconds for RBAC propagation** — especially in automated scripts

---

*Based on a real implementation. All challenges were encountered and resolved during the build.*
