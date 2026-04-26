"""Azure Function — API backend for the Maybank KB Declarative Agent.

This function serves as the bridge between M365 Copilot and the Foundry agent.

Flow:
  M365 Copilot → POST /api/ask → This Function → Foundry Agent → AI Search → Response

Endpoints:
  POST /api/ask       — Ask a question to the KB agent
  POST /api/feedback  — Submit user feedback (rating, category, comment)
  GET  /api/health    — Health check
"""

import json
import logging
import os
from datetime import datetime, timezone

import azure.functions as func
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# ----- Configuration (from Azure Function App Settings) -----
PROJECT_ENDPOINT = os.environ.get("PROJECT_ENDPOINT")
AGENT_NAME = os.environ.get("AGENT_NAME", "maybank-kb-agent")
MODEL_NAME = os.environ.get("MODEL_DEPLOYMENT_NAME", "gpt-4o-mini")
STORAGE_CONNECTION = os.environ.get("AzureWebJobsStorage", "")


def _get_credential():
    """Return the appropriate Azure credential for the current environment."""
    if os.environ.get("AZURE_FUNCTIONS_ENVIRONMENT") == "Production":
        return ManagedIdentityCredential()
    return DefaultAzureCredential()


def _get_openai_client():
    """Create an OpenAI client via AIProjectClient (handles token scope correctly)."""
    from azure.ai.projects import AIProjectClient

    credential = _get_credential()
    project_client = AIProjectClient(
        endpoint=PROJECT_ENDPOINT,
        credential=credential,
    )
    return project_client.get_openai_client()


def _ask_foundry_agent(question: str) -> dict:
    """Send a question to the Foundry agent and return structured response.

    Returns:
        {
            "answer": "The grounded answer text...",
            "citations": [
                {"title": "...", "url": "..."},
            ]
        }
    """
    client = _get_openai_client()

    response = client.responses.create(
        model=MODEL_NAME,
        input=question,
        extra_body={
            "agent_reference": {"name": AGENT_NAME, "type": "agent_reference"}
        },
    )

    # Extract answer text and citations from the response
    answer_text = ""
    citations = []

    for item in response.output:
        if item.type == "message":
            for content in item.content:
                if content.type == "output_text":
                    answer_text = content.text
                    # Extract citation annotations
                    if hasattr(content, "annotations") and content.annotations:
                        for ann in content.annotations:
                            if ann.type == "url_citation":
                                citations.append({
                                    "title": getattr(ann, "title", "Source"),
                                    "url": ann.url,
                                })

    return {"answer": answer_text, "citations": citations}


# ----- HTTP Endpoint: POST /api/ask -----

@app.route(route="ask", methods=["POST"])
def ask(req: func.HttpRequest) -> func.HttpResponse:
    """Handle a question from M365 Copilot.

    Request body:
        { "question": "How do I open a savings account?" }

    Response body:
        {
            "answer": "To open a savings account at Maybank...",
            "citations": [
                { "title": "Account Opening", "url": "https://kb.maybank.com/..." }
            ]
        }
    """
    logging.info("POST /api/ask — received request")

    # Validate configuration
    if not PROJECT_ENDPOINT:
        return func.HttpResponse(
            json.dumps({"error": "SERVER_MISCONFIGURED", "message": "PROJECT_ENDPOINT not set"}),
            status_code=500,
            mimetype="application/json",
        )

    # Parse request body
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "INVALID_JSON", "message": "Request body must be valid JSON"}),
            status_code=400,
            mimetype="application/json",
        )

    question = body.get("question", "").strip()
    if not question:
        return func.HttpResponse(
            json.dumps({"error": "MISSING_QUESTION", "message": "The 'question' field is required"}),
            status_code=400,
            mimetype="application/json",
        )

    # Call the Foundry agent
    try:
        result = _ask_foundry_agent(question)
        logging.info(f"Agent returned answer with {len(result['citations'])} citations")
        return func.HttpResponse(
            json.dumps(result, ensure_ascii=False),
            status_code=200,
            mimetype="application/json",
        )
    except Exception as e:
        logging.error(f"Foundry agent call failed: {e}")
        return func.HttpResponse(
            json.dumps({"error": "AGENT_ERROR", "message": str(e)}),
            status_code=502,
            mimetype="application/json",
        )


# ----- Warm-up timer (keeps function warm, prevents cold starts) -----

@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", run_on_startup=False)
def warmup(timer: func.TimerRequest) -> None:
    """Runs every 5 minutes to keep the Function App warm on Consumption Plan."""
    logging.info("Warm-up ping — keeping function instance alive")


# ----- Health check endpoint -----

@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    """Health check — verifies the function is running and configured."""
    return func.HttpResponse(
        json.dumps({
            "status": "healthy",
            "agent_name": AGENT_NAME,
            "project_endpoint": PROJECT_ENDPOINT[:50] + "..." if PROJECT_ENDPOINT else "NOT SET",
        }),
        status_code=200,
        mimetype="application/json",
    )


# ----- Feedback Storage Helper -----

def _store_feedback(feedback: dict) -> str:
    """Store feedback in Azure Table Storage. Returns the row key."""
    from azure.data.tables import TableServiceClient

    table_service = TableServiceClient.from_connection_string(STORAGE_CONNECTION)
    table_client = table_service.create_table_if_not_exists("AgentFeedback")

    now = datetime.now(timezone.utc)
    row_key = now.strftime("%Y%m%d%H%M%S") + "-" + os.urandom(4).hex()

    entity = {
        "PartitionKey": now.strftime("%Y-%m"),
        "RowKey": row_key,
        "Rating": feedback["rating"],
        "Category": feedback.get("category", ""),
        "Comment": feedback.get("comment", ""),
        "Question": feedback.get("question", ""),
        "Timestamp": now.isoformat(),
    }
    table_client.create_entity(entity)
    return row_key


# ----- HTTP Endpoint: POST /api/feedback -----

VALID_RATINGS = {"thumbs_up", "thumbs_down"}
VALID_CATEGORIES = {"accurate", "inaccurate", "incomplete", "outdated", "other"}


@app.route(route="feedback", methods=["POST"])
def feedback(req: func.HttpRequest) -> func.HttpResponse:
    """Receive user feedback on an agent answer.

    Request body:
        {
            "rating": "thumbs_up" | "thumbs_down",
            "category": "accurate" | "inaccurate" | "incomplete" | "outdated" | "other",
            "comment": "The answer was very helpful!",
            "question": "How do I open a savings account?"
        }

    Response:
        { "status": "received", "feedback_id": "20260425..." }
    """
    logging.info("POST /api/feedback — received request")

    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "INVALID_JSON", "message": "Request body must be valid JSON"}),
            status_code=400,
            mimetype="application/json",
        )

    rating = body.get("rating", "").strip().lower()
    if rating not in VALID_RATINGS:
        return func.HttpResponse(
            json.dumps({"error": "INVALID_RATING", "message": f"Rating must be one of: {', '.join(VALID_RATINGS)}"}),
            status_code=400,
            mimetype="application/json",
        )

    category = body.get("category", "").strip().lower()
    if category and category not in VALID_CATEGORIES:
        return func.HttpResponse(
            json.dumps({"error": "INVALID_CATEGORY", "message": f"Category must be one of: {', '.join(VALID_CATEGORIES)}"}),
            status_code=400,
            mimetype="application/json",
        )

    try:
        feedback_id = _store_feedback({
            "rating": rating,
            "category": category,
            "comment": body.get("comment", "").strip()[:1000],
            "question": body.get("question", "").strip()[:500],
        })
        logging.info(f"Feedback stored: {feedback_id} rating={rating}")
        return func.HttpResponse(
            json.dumps({"status": "received", "feedback_id": feedback_id, "message": "Thank you for your feedback!"}),
            status_code=200,
            mimetype="application/json",
        )
    except Exception as e:
        logging.error(f"Failed to store feedback: {e}")
        return func.HttpResponse(
            json.dumps({"error": "STORAGE_ERROR", "message": str(e)}),
            status_code=500,
            mimetype="application/json",
        )
