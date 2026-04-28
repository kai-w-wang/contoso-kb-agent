"""Azure Function — API backend for the Contoso Bank KB Declarative Agent.

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
import uuid
from datetime import datetime, timezone

import azure.functions as func

from maf_chat_backend import ChatRequest, FunctionChatOrchestratorService, setup_logging

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)
setup_logging()
_shared_service: FunctionChatOrchestratorService | None = None

# ----- Configuration (from Azure Function App Settings) -----
PROJECT_ENDPOINT = os.environ.get("PROJECT_ENDPOINT")
AGENT_NAME = os.environ.get("AGENT_NAME", "contoso-kb-agent")
STORAGE_CONNECTION = os.environ.get("AzureWebJobsStorage", "")


def _get_shared_service() -> FunctionChatOrchestratorService:
    global _shared_service
    if _shared_service is None:
        _shared_service = FunctionChatOrchestratorService()
    return _shared_service


def _resolve_session_id(req: func.HttpRequest, body: dict) -> str:
    candidate_values = [
        body.get("session_id"),
        body.get("conversation_id"),
        body.get("thread_id"),
        req.headers.get("x-session-id"),
        req.headers.get("x-conversation-id"),
        req.headers.get("x-ms-client-session-id"),
    ]
    for value in candidate_values:
        if isinstance(value, str) and value.strip():
            return value.strip()[:128]
    return f"m365-{uuid.uuid4()}"


def _to_m365_response(result: dict) -> dict:
    citations = result.get("citations", []) if isinstance(result, dict) else []
    normalized_citations = []
    if isinstance(citations, list):
        for item in citations:
            if not isinstance(item, dict):
                continue
            source = str(item.get("url") or item.get("source") or "")
            normalized_citations.append(
                {
                    "title": str(item.get("title") or "Source"),
                    "url": source,
                    "source": source,
                    "chunk_id": str(item.get("chunk_id") or ""),
                    "score": item.get("score", 0.0),
                }
            )
    return {
        "session_id": result.get("session_id", ""),
        "answer": result.get("answer", ""),
        "citations": normalized_citations,
        "grounded": bool(result.get("grounded", False)),
        "trace": result.get("trace", {}),
        "evaluator": result.get("evaluator", {}),
    }


# ----- HTTP Endpoint: POST /api/ask -----

@app.route(route="ask", methods=["POST"])
async def ask(req: func.HttpRequest) -> func.HttpResponse:
    """Handle a question from M365 Copilot.

    Request body:
        { "question": "How do I open a savings account?" }

    Response body:
        {
            "answer": "To open a savings account at Contoso Bank...",
            "citations": [
                { "title": "Account Opening", "url": "https://kb.contoso-bank.com/..." }
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

    try:
        request_payload = ChatRequest(
            session_id=_resolve_session_id(req, body),
            user_query=question,
        )
        result = await _get_shared_service().process(request_payload)
        response_body = _to_m365_response(result.model_dump())
        logging.info("Shared backend returned answer with %s citations", len(response_body["citations"]))
        return func.HttpResponse(
            json.dumps(response_body, ensure_ascii=False),
            status_code=200,
            mimetype="application/json",
        )
    except Exception as e:
        logging.error("Shared backend call failed: %s", e)
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
