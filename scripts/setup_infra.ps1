# One-time infrastructure setup for Maybank KB Agent
# Run this script once to provision AI Search, configure RBAC, and create the project connection.
#
# Prerequisites:
#   - Azure CLI logged in: az login
#   - Subscription set: az account set --subscription "0d278827-686c-476e-93ef-3d3d6acabb6a"
#   - Foundry project already provisioned via: azd provision

param(
    [string]$ResourceGroup = "rg-maybank-kb-agent",
    [string]$SearchServiceName = "maybank-kb-search",
    [string]$FoundryAccountName = "ai-account-worwjaulcylli",
    [string]$ProjectName = "ai-project-maybank-kb-agent",
    [string]$Location = "eastus",
    [string]$ConnectionName = "maybank-kb-search-connection"
)

$ErrorActionPreference = "Stop"

Write-Host "============================================================"
Write-Host "Maybank KB Agent — Infrastructure Setup"
Write-Host "============================================================"

# --- Step 1: Create AI Search Service ---
Write-Host "`nStep 1: Creating Azure AI Search service..."
az search service create `
    --name $SearchServiceName `
    --resource-group $ResourceGroup `
    --sku standard `
    --location $Location `
    --partition-count 1 `
    --replica-count 1 `
    -o none
Write-Host "  Search service '$SearchServiceName' created."

# Enable RBAC auth (AAD + API key)
az search service update `
    --name $SearchServiceName `
    --resource-group $ResourceGroup `
    --auth-options aadOrApiKey `
    --aad-auth-failure-mode http401WithBearerChallenge `
    -o none
Write-Host "  RBAC auth enabled on search service."

# --- Step 2: Assign RBAC roles ---
Write-Host "`nStep 2: Assigning RBAC roles to Foundry managed identity..."
$principalId = az cognitiveservices account identity show `
    --name $FoundryAccountName `
    --resource-group $ResourceGroup `
    --query principalId -o tsv

$searchResourceId = az search service show `
    --name $SearchServiceName `
    --resource-group $ResourceGroup `
    --query id -o tsv

$roles = @("Search Index Data Contributor", "Search Service Contributor", "Search Index Data Reader")
foreach ($role in $roles) {
    az role assignment create `
        --assignee $principalId `
        --role $role `
        --scope $searchResourceId `
        -o none 2>$null
    Write-Host "  Assigned '$role' to Foundry MI"
}

# Enable system-assigned managed identity on search service (for integrated vectorizer)
az search service update --name $SearchServiceName --resource-group $ResourceGroup --identity-type SystemAssigned -o none 2>$null
$searchPrincipal = az search service show --name $SearchServiceName --resource-group $ResourceGroup --query "identity.principalId" -o tsv 2>&1
$accountId = az cognitiveservices account show --name $FoundryAccountName --resource-group $ResourceGroup --query id -o tsv 2>&1

az role assignment create --assignee $searchPrincipal --role "Cognitive Services OpenAI User" --scope $accountId -o none 2>$null
Write-Host "  Assigned 'Cognitive Services OpenAI User' to Search MI (for integrated vectorizer)"

# --- Step 3: Create project connection ---
Write-Host "`nStep 3: Creating project connection to AI Search..."
$token = az account get-access-token --resource https://management.azure.com --query accessToken -o tsv

$searchKey = az search admin-key show `
    --resource-group $ResourceGroup `
    --service-name $SearchServiceName `
    --query primaryKey -o tsv

$connBody = @{
    properties = @{
        category = "CognitiveSearch"
        target = "https://$SearchServiceName.search.windows.net"
        authType = "ApiKey"
        credentials = @{ key = $searchKey }
    }
} | ConvertTo-Json -Depth 5

$uri = "https://management.azure.com/subscriptions/0d278827-686c-476e-93ef-3d3d6acabb6a/resourceGroups/$ResourceGroup/providers/Microsoft.CognitiveServices/accounts/$FoundryAccountName/projects/$ProjectName/connections/${ConnectionName}?api-version=2025-04-01-preview"

$resp = Invoke-RestMethod -Uri $uri -Method PUT -Body $connBody -Headers @{
    "Content-Type" = "application/json"
    "Authorization" = "Bearer $token"
}
Write-Host "  Connection '$($resp.name)' created."

# --- Step 4: Deploy models ---
Write-Host "`nStep 4: Deploying models..."
az cognitiveservices account deployment create `
    --name $FoundryAccountName `
    --resource-group $ResourceGroup `
    --deployment-name gpt-4o-mini `
    --model-name gpt-4o-mini `
    --model-version "2024-07-18" `
    --model-format OpenAI `
    --sku-capacity 30 `
    --sku-name GlobalStandard `
    -o none 2>$null
Write-Host "  gpt-4o-mini deployed."

az cognitiveservices account deployment create `
    --name $FoundryAccountName `
    --resource-group $ResourceGroup `
    --deployment-name text-embedding-ada-002 `
    --model-name text-embedding-ada-002 `
    --model-version "2" `
    --model-format OpenAI `
    --sku-capacity 30 `
    --sku-name Standard `
    -o none 2>$null
Write-Host "  text-embedding-ada-002 deployed."

Write-Host "`n============================================================"
Write-Host "Infrastructure setup complete!"
Write-Host "============================================================"
Write-Host "`nNext steps:"
Write-Host "  1. Ingest data:  python scripts/ingest_kb.py"
Write-Host "  2. Deploy agent: python scripts/deploy_agent.py"
Write-Host "  3. Run tests:    python -m pytest tests/test_agent.py -v"
