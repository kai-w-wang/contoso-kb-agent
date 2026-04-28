"""
Step 5: Publish Declarative Agent to M365 via Microsoft Graph API

This script:
1. Packages the appPackage/ folder into a ZIP
2. Uploads it to the M365 App Catalog via Graph API
3. The app then appears in Teams Admin Center for approval

Prerequisites:
- Azure CLI logged in with admin consent for Graph API
- App package files in m365-app/appPackage/
"""

import os
import sys
import json
import zipfile
import subprocess
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
APP_PACKAGE_DIR = os.path.join(PROJECT_ROOT, "m365-app", "appPackage")


def create_zip_package(output_path: str) -> str:
    """Create a ZIP file from the appPackage directory."""
    required_files = [
        "manifest.json",
        "declarativeAgent.json",
        "api-plugin.json",
        "openapi.json",
        "color.png",
        "outline.png",
    ]

    # Verify all required files exist
    for f in required_files:
        fpath = os.path.join(APP_PACKAGE_DIR, f)
        if not os.path.exists(fpath):
            print(f"ERROR: Missing required file: {fpath}")
            sys.exit(1)

    # Create ZIP
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in required_files:
            fpath = os.path.join(APP_PACKAGE_DIR, f)
            zf.write(fpath, f)
            print(f"  Added: {f} ({os.path.getsize(fpath)} bytes)")

    print(f"\nPackage created: {output_path} ({os.path.getsize(output_path)} bytes)")
    return output_path


def get_graph_token() -> str:
    """Get a Microsoft Graph access token via Azure CLI."""
    result = subprocess.run(
        "az account get-access-token --resource https://graph.microsoft.com --query accessToken -o tsv",
        capture_output=True, text=True, shell=True
    )
    if result.returncode != 0:
        print(f"ERROR: Failed to get Graph token: {result.stderr}")
        sys.exit(1)
    return result.stdout.strip()


def publish_to_m365(zip_path: str, token: str):
    """Upload the app package to M365 App Catalog via Graph API."""
    import urllib.request

    url = "https://graph.microsoft.com/v1.0/appCatalogs/teamsApps"

    with open(zip_path, "rb") as f:
        zip_data = f.read()

    req = urllib.request.Request(
        url,
        data=zip_data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/zip",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode())
            print(f"\n✅ App published successfully!")
            print(f"   App ID: {body.get('id', 'N/A')}")
            print(f"   External ID: {body.get('externalId', 'N/A')}")
            print(f"\nNext steps:")
            print(f"  1. Go to Teams Admin Center → Manage Apps")
            print(f"  2. Search for 'Contoso Bank KB Assistant'")
            print(f"  3. Approve the app for your organization")
            print(f"  4. Users can then find it in M365 Copilot")
            return body
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(f"\nERROR: Graph API returned {e.code}")
        print(f"Response: {error_body}")
        if e.code == 409:
            print("\nThe app already exists. Use update instead:")
            print("  POST /appCatalogs/teamsApps/{id}/appDefinitions")
        elif e.code == 403:
            print("\nInsufficient permissions. Ensure your account has:")
            print("  - AppCatalog.Submit or AppCatalog.ReadWrite.All")
            print("  - Teams Admin role for org-wide distribution")
        sys.exit(1)


def main():
    print("=" * 60)
    print("M365 Declarative Agent Publisher")
    print("=" * 60)

    # Step 1: Create ZIP package
    print("\n📦 Step 1: Creating app package...")
    zip_path = os.path.join(PROJECT_ROOT, "m365-app", "contoso-kb-agent.zip")
    create_zip_package(zip_path)

    # Step 2: Get Graph token
    print("\n🔑 Step 2: Acquiring Graph API token...")
    token = get_graph_token()
    print(f"   Token acquired (length: {len(token)})")

    # Step 3: Publish
    print("\n🚀 Step 3: Publishing to M365 App Catalog...")
    publish_to_m365(zip_path, token)


if __name__ == "__main__":
    main()
