targetScope = 'subscription'
// targetScope = 'resourceGroup'

@minLength(1)
@maxLength(64)
@description('Name of the environment that can be used as part of naming resource convention')
param environmentName string

@minLength(1)
@maxLength(90)
@description('Name of the resource group to use or create')
param resourceGroupName string = 'rg-${environmentName}'

// Restricted locations to match list from
// https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/responses?tabs=python-key#region-availability
@minLength(1)
@description('Primary location for all resources')
@allowed([
  'australiaeast'
  'brazilsouth'
  'canadacentral'
  'canadaeast'
  'eastus'
  'eastus2'
  'francecentral'
  'germanywestcentral'
  'italynorth'
  'japaneast'
  'koreacentral'
  'northcentralus'
  'norwayeast'
  'polandcentral'
  'southafricanorth'
  'southcentralus'
  'southeastasia'
  'southindia'
  'spaincentral'
  'swedencentral'
  'switzerlandnorth'
  'uaenorth'
  'uksouth'
  'westus'
  'westus2'
  'westus3'
])
param location string

param aiDeploymentsLocation string

@description('Id of the user or app to assign application roles')
param principalId string

@description('Principal type of user or app')
param principalType string

@description('Optional. Name of an existing AI Services account within the resource group. If not provided, a new one will be created.')
param aiFoundryResourceName string = ''

@description('Optional. Name of the AI Foundry project. If not provided, a default name will be used.')
param aiFoundryProjectName string = 'ai-project-${environmentName}'

@description('List of model deployments')
param aiProjectDeploymentsJson string = '[]'

@description('Default Foundry model deployment name to provision when aiProjectDeploymentsJson is empty.')
param foundryModelDeploymentName string = 'o3'

@description('Default Foundry model name to provision when aiProjectDeploymentsJson is empty.')
param foundryModelName string = 'o3'

@description('Default Foundry model version to provision when aiProjectDeploymentsJson is empty.')
param foundryModelVersion string = '2025-04-16'

@description('Default Foundry model deployment SKU name.')
param foundryModelSkuName string = 'GlobalProvisionedManaged'

@description('Default Foundry model deployment SKU capacity.')
param foundryModelSkuCapacity int = 15

@description('Hermes API mode for the provisioned Foundry model.')
@allowed([
  'chat_completions'
  'codex_responses'
  'anthropic_messages'
])
param foundryModelApiMode string = 'codex_responses'

@description('Hermes auth mode for the provisioned Foundry model.')
@allowed([
  'entra_id'
  'api_key'
])
param foundryModelAuthMode string = 'entra_id'

@description('Optional. OpenAI base URL of a pre-existing AI Services account in another region/account to source models from. When set, no local model deployments are created and the agent calls this endpoint for models. Use the account OpenAI endpoint, e.g. https://<account>.openai.azure.com/.')
param externalModelBaseUrl string = ''

@description('Optional. Resource ID of the external AI Services account that hosts the models. Used to grant the hosted agent identity model access at postdeploy. Required when externalModelBaseUrl is set.')
param externalModelAccountResourceId string = ''

@description('Optional. Primary model deployment name on the external account. Required when externalModelBaseUrl is set.')
param externalModelDeploymentName string = ''

@description('Optional. Auxiliary model deployment name on the external account. Leave empty to omit the auxiliary model.')
param externalModelAuxDeploymentName string = ''

@description('List of connections')
param aiProjectConnectionsJson string = '[]'

@secure()
@description('JSON map of connection name to credentials object. Example: {"my-conn":{"key":"secret"}}')
param aiProjectConnectionCredentialsJson string = '{}'

@description('List of resources to create and connect to the AI project')
param aiProjectDependentResourcesJson string = '[]'

var configuredAiProjectDeployments = json(aiProjectDeploymentsJson)
// When sourcing models from an external account, do not create any local model deployments.
var useExternalModels = !empty(externalModelBaseUrl)
var defaultAiProjectDeployments = [
  {
    name: foundryModelDeploymentName
    model: {
      name: foundryModelName
      format: 'OpenAI'
      version: foundryModelVersion
    }
    sku: {
      name: foundryModelSkuName
      capacity: foundryModelSkuCapacity
    }
  }
]
var aiProjectDeployments = useExternalModels ? [] : (empty(configuredAiProjectDeployments) ? defaultAiProjectDeployments : configuredAiProjectDeployments)
var aiProjectConnections = json(aiProjectConnectionsJson)
var aiProjectConnectionCreds = json(aiProjectConnectionCredentialsJson)
var aiProjectDependentResources = json(aiProjectDependentResourcesJson)
var selectedFoundryModelDeployment = empty(aiProjectDeployments) ? {
  name: ''
  model: {
    name: ''
    format: ''
    version: ''
  }
  sku: {
    name: ''
    capacity: 0
  }
} : aiProjectDeployments[0]

@description('Enable hosted agent deployment')
param enableHostedAgents bool

@description('Enable monitoring for the AI project')
param enableMonitoring bool

@description('When true, skip Foundry project/role/connection provisioning and reference the existing project read-only. Use when pointing at an existing Foundry project via --project-id.')
param useExistingAiProject bool = false

@description('Optional. Existing container registry resource ID. If provided, no new ACR will be created and a connection to this ACR will be established.')
param existingContainerRegistryResourceId string = ''

@description('Optional. Existing container registry endpoint (login server). Required if existingContainerRegistryResourceId is provided.')
param existingContainerRegistryEndpoint string = ''

@description('Optional. Name of an existing ACR connection on the Foundry project. If provided, no new ACR or connection will be created.')
param existingAcrConnectionName string = ''

@description('Optional. Existing Application Insights connection string. If provided, a connection will be created but no new App Insights resource.')
param existingApplicationInsightsConnectionString string = ''

@description('Optional. Existing Application Insights resource ID. Used for connection metadata when providing an existing App Insights.')
param existingApplicationInsightsResourceId string = ''

@description('Optional. Name of an existing Application Insights connection on the Foundry project. If provided, no new App Insights or connection will be created.')
param existingAppInsightsConnectionName string = ''

// Tags that should be applied to all resources.
//
// Note that 'azd-service-name' tags should be applied separately to service host resources.
// Example usage:
//   tags: union(tags, { 'azd-service-name': <service name in azure.yaml> })
var tags = {
  'azd-env-name': environmentName
}

// Check if resource group exists and create it if it doesn't
resource rg 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

// Build dependent resources array conditionally
// Check if ACR already exists in the user-provided array to avoid duplicates
// Also skip if user provided an existing container registry endpoint or connection name
var hasAcr = contains(map(aiProjectDependentResources, r => r.resource), 'registry')
var shouldCreateAcr = enableHostedAgents && !hasAcr && empty(existingContainerRegistryResourceId) && empty(existingAcrConnectionName)
var dependentResources = shouldCreateAcr ? union(aiProjectDependentResources, [
  {
    resource: 'registry'
    connectionName: 'acr-${uniqueString(subscription().id, resourceGroupName, location)}'
  }
]) : aiProjectDependentResources

// AI Project module — only when creating new resources
module aiProject 'core/ai/ai-project.bicep' = if (!useExistingAiProject) {
  scope: rg
  name: 'ai-project'
  params: {
    tags: tags
    location: aiDeploymentsLocation
    aiFoundryProjectName: aiFoundryProjectName
    principalId: principalId
    principalType: principalType
    existingAiAccountName: aiFoundryResourceName
    deployments: aiProjectDeployments
    connections: aiProjectConnections
    connectionCredentials: aiProjectConnectionCreds
    additionalDependentResources: dependentResources
    enableMonitoring: enableMonitoring
    enableHostedAgents: enableHostedAgents
    existingContainerRegistryResourceId: existingContainerRegistryResourceId
    existingContainerRegistryEndpoint: existingContainerRegistryEndpoint
    existingAcrConnectionName: existingAcrConnectionName
    existingApplicationInsightsConnectionString: existingApplicationInsightsConnectionString
    existingApplicationInsightsResourceId: existingApplicationInsightsResourceId
    existingAppInsightsConnectionName: existingAppInsightsConnectionName
  }
}

// Existing project module — read-only reference when reusing an existing Foundry project
module existingAiProject 'core/ai/existing-ai-project.bicep' = if (useExistingAiProject) {
  scope: rg
  name: 'existing-ai-project'
  params: {
    aiServicesAccountName: aiFoundryResourceName
    aiFoundryProjectName: aiFoundryProjectName
    existingAcrConnectionName: existingAcrConnectionName
    existingContainerRegistryEndpoint: existingContainerRegistryEndpoint
    existingApplicationInsightsConnectionString: existingApplicationInsightsConnectionString
    existingApplicationInsightsResourceId: existingApplicationInsightsResourceId
  }
}

// ACR for existing project — create when hosted agents need a registry but the existing project has none
var shouldCreateAcrForExistingProject = useExistingAiProject && shouldCreateAcr
var acrConnectionName = 'acr-${uniqueString(subscription().id, resourceGroupName, location)}'

module acrForExistingProject 'core/host/acr.bicep' = if (shouldCreateAcrForExistingProject) {
  scope: rg
  name: 'acr-for-existing-project'
  params: {
    location: location
    tags: tags
    resourceName: 'cr${uniqueString(subscription().id, resourceGroupName, location)}'
    connectionName: acrConnectionName
    principalId: principalId
    principalType: principalType
    aiServicesAccountName: aiFoundryResourceName
    aiProjectName: aiFoundryProjectName
  }
}

// Resources
output AZURE_RESOURCE_GROUP string = resourceGroupName
output AZURE_AI_ACCOUNT_ID string = useExistingAiProject ? existingAiProject.outputs.accountId : aiProject.outputs.accountId
output AZURE_AI_PROJECT_ID string = useExistingAiProject ? existingAiProject.outputs.projectId : aiProject.outputs.projectId
output AZURE_AI_FOUNDRY_PROJECT_ID string = useExistingAiProject ? existingAiProject.outputs.projectId : aiProject.outputs.projectId
output FOUNDRY_PROJECT_ID string = useExistingAiProject ? existingAiProject.outputs.projectId : aiProject.outputs.projectId
output AZURE_AI_ACCOUNT_NAME string = useExistingAiProject ? existingAiProject.outputs.aiServicesAccountName : aiProject.outputs.aiServicesAccountName
output AZURE_AI_PROJECT_NAME string = useExistingAiProject ? existingAiProject.outputs.projectName : aiProject.outputs.projectName

// Endpoints
output AZURE_AI_PROJECT_ENDPOINT string = useExistingAiProject ? existingAiProject.outputs.AZURE_AI_PROJECT_ENDPOINT : aiProject.outputs.AZURE_AI_PROJECT_ENDPOINT
output FOUNDRY_PROJECT_ENDPOINT string = useExistingAiProject ? existingAiProject.outputs.AZURE_AI_PROJECT_ENDPOINT : aiProject.outputs.AZURE_AI_PROJECT_ENDPOINT
output AZURE_AI_SERVICES_ENDPOINT string = useExistingAiProject ? existingAiProject.outputs.aiServicesEndpoint : aiProject.outputs.aiServicesEndpoint
output AZURE_OPENAI_ENDPOINT string = useExistingAiProject ? existingAiProject.outputs.AZURE_OPENAI_ENDPOINT : aiProject.outputs.AZURE_OPENAI_ENDPOINT
output AZURE_FOUNDRY_BASE_URL string = useExternalModels ? externalModelBaseUrl : (useExistingAiProject ? existingAiProject.outputs.AZURE_OPENAI_ENDPOINT : aiProject.outputs.AZURE_OPENAI_ENDPOINT)
output AZURE_FOUNDRY_MODEL_DEPLOYMENT_NAME string = useExternalModels ? externalModelDeploymentName : selectedFoundryModelDeployment.name
output AZURE_FOUNDRY_MODEL_NAME string = useExternalModels ? externalModelDeploymentName : selectedFoundryModelDeployment.model.name
output AZURE_FOUNDRY_MODEL_API_MODE string = foundryModelApiMode
output AZURE_FOUNDRY_AUTH_MODE string = foundryModelAuthMode
output AZURE_FOUNDRY_AUX_MODEL_DEPLOYMENT_NAME string = useExternalModels ? externalModelAuxDeploymentName : (length(aiProjectDeployments) > 1 ? aiProjectDeployments[1].name : '')
// Resource ID of the account that hosts the models the agent calls. Empty in local mode (agent uses AZURE_AI_ACCOUNT_ID); set to the external account in split mode so postdeploy can grant model access there.
output AZURE_FOUNDRY_MODEL_ACCOUNT_ID string = useExternalModels ? externalModelAccountResourceId : ''
output APPLICATIONINSIGHTS_CONNECTION_STRING string = useExistingAiProject ? existingAiProject.outputs.APPLICATIONINSIGHTS_CONNECTION_STRING : aiProject.outputs.APPLICATIONINSIGHTS_CONNECTION_STRING
output APPLICATIONINSIGHTS_RESOURCE_ID string = useExistingAiProject ? existingAiProject.outputs.APPLICATIONINSIGHTS_RESOURCE_ID : aiProject.outputs.APPLICATIONINSIGHTS_RESOURCE_ID

// Dependent Resources and Connections

// ACR
output AZURE_AI_PROJECT_ACR_CONNECTION_NAME string = shouldCreateAcrForExistingProject ? acrForExistingProject.outputs.containerRegistryConnectionName : (useExistingAiProject ? existingAiProject.outputs.dependentResources.registry.connectionName : aiProject.outputs.dependentResources.registry.connectionName)
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = shouldCreateAcrForExistingProject ? acrForExistingProject.outputs.containerRegistryLoginServer : (useExistingAiProject ? existingAiProject.outputs.dependentResources.registry.loginServer : aiProject.outputs.dependentResources.registry.loginServer)

// Bing Search
output BING_GROUNDING_CONNECTION_NAME  string = useExistingAiProject ? existingAiProject.outputs.dependentResources.bing_grounding.connectionName : aiProject.outputs.dependentResources.bing_grounding.connectionName
output BING_GROUNDING_RESOURCE_NAME string = useExistingAiProject ? existingAiProject.outputs.dependentResources.bing_grounding.name : aiProject.outputs.dependentResources.bing_grounding.name
output BING_GROUNDING_CONNECTION_ID string = useExistingAiProject ? existingAiProject.outputs.dependentResources.bing_grounding.connectionId : aiProject.outputs.dependentResources.bing_grounding.connectionId

// Bing Custom Search
output BING_CUSTOM_GROUNDING_CONNECTION_NAME string = useExistingAiProject ? existingAiProject.outputs.dependentResources.bing_custom_grounding.connectionName : aiProject.outputs.dependentResources.bing_custom_grounding.connectionName
output BING_CUSTOM_GROUNDING_NAME string = useExistingAiProject ? existingAiProject.outputs.dependentResources.bing_custom_grounding.name : aiProject.outputs.dependentResources.bing_custom_grounding.name
output BING_CUSTOM_GROUNDING_CONNECTION_ID string = useExistingAiProject ? existingAiProject.outputs.dependentResources.bing_custom_grounding.connectionId : aiProject.outputs.dependentResources.bing_custom_grounding.connectionId

// Azure AI Search
output AZURE_AI_SEARCH_CONNECTION_NAME string = useExistingAiProject ? existingAiProject.outputs.dependentResources.search.connectionName : aiProject.outputs.dependentResources.search.connectionName
output AZURE_AI_SEARCH_SERVICE_NAME string = useExistingAiProject ? existingAiProject.outputs.dependentResources.search.serviceName : aiProject.outputs.dependentResources.search.serviceName

// Azure Storage
output AZURE_STORAGE_CONNECTION_NAME string = useExistingAiProject ? existingAiProject.outputs.dependentResources.storage.connectionName : aiProject.outputs.dependentResources.storage.connectionName
output AZURE_STORAGE_ACCOUNT_NAME string = useExistingAiProject ? existingAiProject.outputs.dependentResources.storage.accountName : aiProject.outputs.dependentResources.storage.accountName

// Connections
output AI_PROJECT_CONNECTION_IDS_JSON string = useExistingAiProject ? string(existingAiProject.outputs.connectionIds) : string(aiProject.outputs.connectionIds)
