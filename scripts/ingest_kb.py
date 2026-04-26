"""One-time KB data ingestion — creates search index and uploads documents.

Usage:
    python scripts/ingest_kb.py [--search-endpoint URL]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_config
from src.search_setup import create_index, ingest_documents


def main():
    parser = argparse.ArgumentParser(description="Ingest KB articles into Azure AI Search")
    parser.add_argument(
        "--search-endpoint",
        default="https://maybank-kb-search.search.windows.net",
        help="Azure AI Search endpoint URL",
    )
    parser.add_argument(
        "--data",
        default=None,
        help="Path to KB articles JSON file (default: data/kb_articles.json)",
    )
    args = parser.parse_args()

    config = get_config()

    print("=" * 60)
    print("Maybank KB Agent — Data Ingestion")
    print("=" * 60)
    print(f"\nSearch endpoint: {args.search_endpoint}")
    print(f"Index name     : {config.search_index_name}")
    print()

    # Step 1: Create/update index schema
    print("Step 1: Creating search index...")
    create_index(config, args.search_endpoint)

    # Step 2: Upload documents with embeddings
    print("\nStep 2: Ingesting documents...")
    data_path = Path(args.data) if args.data else None
    count = ingest_documents(config, args.search_endpoint, data_path)

    print(f"\nDone! {count} documents ingested into '{config.search_index_name}'")


if __name__ == "__main__":
    main()
