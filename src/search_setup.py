"""Search index creation and KB document ingestion."""

import json
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    AzureOpenAIVectorizer,
    AzureOpenAIVectorizerParameters,
    HnswAlgorithmConfiguration,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchProfile,
)
from openai import AzureOpenAI

from src.config import Config


def create_index(config: Config, search_endpoint: str) -> None:
    """Create the KB articles search index with vector + semantic config."""
    index_client = SearchIndexClient(
        endpoint=search_endpoint,
        credential=DefaultAzureCredential(),
    )

    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
        SearchableField(name="title", type=SearchFieldDataType.String, retrievable=True),
        SearchableField(name="content", type=SearchFieldDataType.String, retrievable=True),
        SearchableField(
            name="category",
            type=SearchFieldDataType.String,
            filterable=True,
            facetable=True,
            retrievable=True,
        ),
        SimpleField(name="source_url", type=SearchFieldDataType.String, retrievable=True),
        SearchField(
            name="contentVector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=1536,
            vector_search_profile_name="default-profile",
        ),
    ]

    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(name="hnsw-algorithm"),
        ],
        profiles=[
            VectorSearchProfile(
                name="default-profile",
                algorithm_configuration_name="hnsw-algorithm",
                vectorizer_name="openai-vectorizer",
            ),
        ],
        vectorizers=[
            AzureOpenAIVectorizer(
                vectorizer_name="openai-vectorizer",
                parameters=AzureOpenAIVectorizerParameters(
                    resource_url=config.azure_openai_endpoint.rstrip("/"),
                    deployment_name=config.embedding_deployment_name,
                    model_name=config.embedding_deployment_name,
                ),
            ),
        ],
    )

    semantic_config = SemanticConfiguration(
        name="default-semantic",
        prioritized_fields=SemanticPrioritizedFields(
            title_field=SemanticField(field_name="title"),
            content_fields=[SemanticField(field_name="content")],
        ),
    )

    index = SearchIndex(
        name=config.search_index_name,
        fields=fields,
        vector_search=vector_search,
        semantic_search=SemanticSearch(
            configurations=[semantic_config],
            default_configuration_name="default-semantic",
        ),
    )

    result = index_client.create_or_update_index(index)
    print(f"Index '{result.name}' created/updated with {len(result.fields)} fields")


def generate_embeddings(
    texts: list[str],
    config: Config,
) -> list[list[float]]:
    """Generate embeddings for a list of texts using Azure OpenAI."""
    client = AzureOpenAI(
        azure_endpoint=config.azure_openai_endpoint,
        azure_ad_token_provider=DefaultAzureCredential().get_token(
            "https://cognitiveservices.azure.com/.default"
        ).token,
        api_version="2024-02-01",
    )

    response = client.embeddings.create(
        input=texts,
        model=config.embedding_deployment_name,
    )
    return [item.embedding for item in response.data]


def ingest_documents(
    config: Config,
    search_endpoint: str,
    data_path: Path | None = None,
) -> int:
    """Load KB articles from JSON, generate embeddings, and upload to search index."""
    if data_path is None:
        data_path = Path(__file__).resolve().parent.parent / "data" / "kb_articles.json"

    with open(data_path, encoding="utf-8") as f:
        articles = json.load(f)

    print(f"Loaded {len(articles)} articles from {data_path.name}")

    # Generate embeddings in batch
    contents = [a["content"] for a in articles]
    print("Generating embeddings...")
    vectors = generate_embeddings(contents, config)

    # Add vectors to documents
    for article, vector in zip(articles, vectors):
        article["contentVector"] = vector

    # Upload to search index
    search_client = SearchClient(
        endpoint=search_endpoint,
        index_name=config.search_index_name,
        credential=DefaultAzureCredential(),
    )

    result = search_client.upload_documents(documents=articles)
    succeeded = sum(1 for r in result if r.succeeded)
    print(f"Uploaded {succeeded}/{len(articles)} documents to '{config.search_index_name}'")
    return succeeded
