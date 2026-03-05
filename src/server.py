"""
Basecamp MCP Server — Read-Only Connector for Claude Cowork
Implements MCP Streamable HTTP transport (2025-06-18 spec) with OAuth 2.0 proxy.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import urllib.parse
import uuid
import httpx
import asyncio
from typing import Any
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Basecamp MCP Connector")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASECAMP_API_BASE = "https://3.basecampapi.com"
BASECAMP_AUTH_BASE = "https://launchpad.37signals.com"
USER_AGENT = "BasecampMCP-Claude/1.0 (claude-connector)"

# Shared async HTTP client — reused across requests for connection pooling.
# 10-second timeout prevents tool calls from hanging indefinitely.
_http_client: httpx.AsyncClient | None = None

# Cache account_id per token — avoids an extra Basecamp roundtrip on every tool call.
# Tokens are long-lived per session so this is safe. Capped at 500 entries.
_account_id_cache: dict[str, str] = {}


@app.on_event("startup")
async def _startup():
    global _http_client
    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )


@app.on_event("shutdown")
async def _shutdown():
    if _http_client:
        await _http_client.aclose()

# OAuth config — set via Heroku config vars
BASECAMP_CLIENT_ID = os.environ.get("BASECAMP_CLIENT_ID", "")
BASECAMP_CLIENT_SECRET = os.environ.get("BASECAMP_CLIENT_SECRET", "")

# The public base URL of this server — used to build redirect URIs
SERVER_BASE_URL = os.environ.get(
    "SERVER_BASE_URL",
    "https://basecamp-mcp-claude-38e78468b14b.herokuapp.com"
).rstrip("/")

# ---------------------------------------------------------------------------
# Secret used to sign stateless OAuth tokens so they can't be forged.
# Generated once per deployment; set as OAUTH_SECRET env var for stability.
SECRET_KEY = os.environ.get("OAUTH_SECRET", os.urandom(32).hex()).encode()


def _b64_encode(data: dict) -> str:
    """Encode a dict as URL-safe base64."""
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def _b64_decode(s: str) -> dict:
    """Decode a URL-safe base64 string back to a dict."""
    padding = 4 - len(s) % 4
    return json.loads(base64.urlsafe_b64decode(s + "=" * padding).decode())


def _sign(payload: str) -> str:
    """Return HMAC-SHA256 hex digest of payload."""
    return hmac.new(SECRET_KEY, payload.encode(), hashlib.sha256).hexdigest()


def _make_signed_token(data: dict) -> str:
    """Encode data + HMAC signature into a URL-safe token."""
    payload = _b64_encode(data)
    sig = _sign(payload)
    return f"{payload}.{sig}"


def _decode_signed_token(token: str) -> dict | None:
    """Verify signature and decode token. Returns None if invalid."""
    try:
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(_sign(payload), sig):
            return None
        return _b64_decode(payload)
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Basecamp API client
# ---------------------------------------------------------------------------

def _bc_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}


async def bc_get(path: str, token: str, params: dict = None) -> dict | list:
    """Make an authenticated GET request to the Basecamp API."""
    client = _http_client or httpx.AsyncClient(timeout=10.0)
    url = f"{BASECAMP_API_BASE}{path}"
    resp = await client.get(url, headers=_bc_headers(token), params=params or {}, follow_redirects=True)
    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="Basecamp token invalid or expired")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Not found: {path}")
    resp.raise_for_status()
    return resp.json()


async def bc_get_paginated(path: str, token: str, params: dict = None, max_pages: int = 5) -> list:
    """Fetch all pages from a paginated Basecamp endpoint using the shared client."""
    client = _http_client or httpx.AsyncClient(timeout=10.0)
    url = f"{BASECAMP_API_BASE}{path}"
    headers = _bc_headers(token)
    results = []
    page = 1
    first_request_params = params or {}
    while url and page <= max_pages:
        req_params = first_request_params if page == 1 else {}
        resp = await client.get(url, headers=headers, params=req_params, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            results.extend(data)
        else:
            results.append(data)
        link_header = resp.headers.get("Link", "")
        next_url = None
        for part in link_header.split(","):
            if 'rel="next"' in part:
                match = re.search(r"<([^>]+)>", part)
                if match:
                    next_url = match.group(1)
        url = next_url
        page += 1
    return results


async def bc_post(path: str, token: str, body: dict) -> dict:
    """Make an authenticated POST request to the Basecamp API."""
    client = _http_client or httpx.AsyncClient(timeout=10.0)
    url = f"{BASECAMP_API_BASE}{path}"
    headers = {**_bc_headers(token), "Content-Type": "application/json"}
    resp = await client.post(url, headers=headers, json=body, follow_redirects=True)
    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="Basecamp token invalid or expired")
    if resp.status_code == 403:
        raise HTTPException(status_code=403, detail="Permission denied")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Not found: {path}")
    resp.raise_for_status()
    return resp.json()


def extract_token(request: Request) -> str:
    """Extract Bearer token from Authorization header."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    raise HTTPException(status_code=401, detail="Missing Authorization header")


def extract_account_id(token_data: dict) -> str:
    """Get the bc3 account ID from /authorization.json response."""
    for acct in token_data.get("accounts", []):
        if acct.get("product") == "bc3":
            href = acct.get("href", "")
            match = re.search(r"/(\d+)$", href)
            if match:
                return match.group(1)
    raise HTTPException(status_code=400, detail="No Basecamp 4 account found for this token")


async def get_account_id(token: str) -> str:
    """Return the Basecamp account ID for this token, using an in-process cache.

    Authorization data lives at launchpad.37signals.com, not 3.basecampapi.com.
    """
    if token in _account_id_cache:
        return _account_id_cache[token]
    client = _http_client or httpx.AsyncClient(timeout=10.0)
    resp = await client.get(
        f"{BASECAMP_AUTH_BASE}/authorization.json",
        headers=_bc_headers(token),
        follow_redirects=True,
    )
    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="Basecamp token invalid or expired")
    resp.raise_for_status()
    auth_data = resp.json()
    account_id = extract_account_id(auth_data)
    if len(_account_id_cache) >= 500:
        _account_id_cache.pop(next(iter(_account_id_cache)))
    _account_id_cache[token] = account_id
    return account_id


# ---------------------------------------------------------------------------
# MCP Tool Definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "get_my_profile",
        "description": "Get the current authenticated user's profile information from Basecamp.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "list_projects",
        "description": "List all active Basecamp projects you have access to, including their names, descriptions, and dock (tools) info.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["active", "archived", "trashed"],
                    "description": "Filter projects by status. Defaults to active."
                }
            },
            "required": []
        }
    },
    {
        "name": "get_project",
        "description": "Get full details about a specific Basecamp project including its tools (dock), description, and team.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "The numeric ID of the Basecamp project."
                }
            },
            "required": ["project_id"]
        }
    },
    {
        "name": "get_project_people",
        "description": "List all people who have access to a specific Basecamp project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "The numeric ID of the Basecamp project."
                }
            },
            "required": ["project_id"]
        }
    },
    {
        "name": "list_all_people",
        "description": "List all people in your Basecamp account.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "list_todolists",
        "description": "List all to-do lists in a Basecamp project. Each project has a todoset (container); this returns the individual lists inside it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "The numeric ID of the Basecamp project."
                }
            },
            "required": ["project_id"]
        }
    },
    {
        "name": "list_todos",
        "description": "List to-do items in a specific to-do list. Can filter by completion status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "The numeric ID of the Basecamp project."
                },
                "todolist_id": {
                    "type": "string",
                    "description": "The numeric ID of the to-do list."
                },
                "completed": {
                    "type": "boolean",
                    "description": "If true, returns only completed to-dos. If false (default), returns only pending to-dos."
                }
            },
            "required": ["project_id", "todolist_id"]
        }
    },
    {
        "name": "get_todo",
        "description": "Get full details of a single to-do item including description, assignees, due date, and completion status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "The numeric ID of the Basecamp project."
                },
                "todo_id": {
                    "type": "string",
                    "description": "The numeric ID of the to-do item."
                }
            },
            "required": ["project_id", "todo_id"]
        }
    },
    {
        "name": "list_messages",
        "description": "List all messages on a project's message board.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "The numeric ID of the Basecamp project."
                }
            },
            "required": ["project_id"]
        }
    },
    {
        "name": "get_message",
        "description": "Get the full content of a specific message including its rich text body.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "The numeric ID of the Basecamp project."
                },
                "message_id": {
                    "type": "string",
                    "description": "The numeric ID of the message."
                }
            },
            "required": ["project_id", "message_id"]
        }
    },
    {
        "name": "list_comments",
        "description": "List all comments on a Basecamp recording (message, to-do, document, etc.).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "The numeric ID of the Basecamp project."
                },
                "recording_id": {
                    "type": "string",
                    "description": "The numeric ID of the recording (message, to-do, document, etc.) to get comments for."
                }
            },
            "required": ["project_id", "recording_id"]
        }
    },
    {
        "name": "list_documents",
        "description": "List all documents in a Basecamp project's Docs & Files vault.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "The numeric ID of the Basecamp project."
                }
            },
            "required": ["project_id"]
        }
    },
    {
        "name": "get_document",
        "description": "Get the full content of a specific document in Basecamp.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "The numeric ID of the Basecamp project."
                },
                "document_id": {
                    "type": "string",
                    "description": "The numeric ID of the document."
                }
            },
            "required": ["project_id", "document_id"]
        }
    },
    {
        "name": "search_recordings",
        "description": "Search across all recordings of a specific type (Todo, Message, Document, etc.) optionally filtered by project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["Todo", "Todolist", "Message", "Document", "Upload"],
                    "description": "The type of recording to search."
                },
                "project_id": {
                    "type": "string",
                    "description": "Optional: limit results to a specific project ID."
                },
                "sort": {
                    "type": "string",
                    "enum": ["created_at", "updated_at"],
                    "description": "Sort order. Defaults to created_at."
                }
            },
            "required": ["type"]
        }
    },
    {
        "name": "create_todo",
        "description": "Create a new to-do item in a Basecamp to-do list.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "The numeric ID of the Basecamp project."
                },
                "todolist_id": {
                    "type": "string",
                    "description": "The numeric ID of the to-do list to add the item to."
                },
                "content": {
                    "type": "string",
                    "description": "The title/content of the to-do item."
                },
                "description": {
                    "type": "string",
                    "description": "Optional longer description (plain text)."
                },
                "due_on": {
                    "type": "string",
                    "description": "Optional due date in YYYY-MM-DD format."
                },
                "starts_on": {
                    "type": "string",
                    "description": "Optional start date in YYYY-MM-DD format."
                },
                "notify": {
                    "type": "boolean",
                    "description": "Whether to notify assignees. Defaults to false."
                }
            },
            "required": ["project_id", "todolist_id", "content"]
        }
    },
    {
        "name": "post_comment",
        "description": "Post a comment on a Basecamp recording (to-do, message, document, etc.).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "The numeric ID of the Basecamp project."
                },
                "recording_id": {
                    "type": "string",
                    "description": "The numeric ID of the recording to comment on (to-do ID, message ID, document ID, etc.)."
                },
                "content": {
                    "type": "string",
                    "description": "The plain text content of the comment."
                }
            },
            "required": ["project_id", "recording_id", "content"]
        }
    }
]


# ---------------------------------------------------------------------------
# Tool Execution
# ---------------------------------------------------------------------------

async def execute_tool(name: str, args: dict, token: str) -> str:
    """Execute a named tool and return a text result."""
    try:
        account_id = await get_account_id(token)
        base = f"/{account_id}"

        if name == "get_my_profile":
            data = await bc_get(f"{base}/my/profile.json", token)
            return json.dumps({
                "name": data.get("name"),
                "email": data.get("email_address"),
                "title": data.get("title"),
                "bio": data.get("bio"),
                "time_zone": data.get("time_zone"),
                "admin": data.get("admin"),
            }, indent=2)

        elif name == "list_projects":
            status = args.get("status", "active")
            params = {"status": status} if status != "active" else {}
            data = await bc_get_paginated(f"{base}/projects.json", token, params=params)
            projects = []
            for p in data:
                projects.append({
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "description": p.get("description"),
                    "status": p.get("status"),
                    "created_at": p.get("created_at"),
                    "updated_at": p.get("updated_at"),
                    "app_url": p.get("app_url"),
                    "tools": [d.get("name") for d in p.get("dock", []) if d.get("enabled")],
                })
            return json.dumps(projects, indent=2)

        elif name == "get_project":
            pid = args["project_id"]
            data = await bc_get(f"{base}/projects/{pid}.json", token)
            return json.dumps({
                "id": data.get("id"),
                "name": data.get("name"),
                "description": data.get("description"),
                "status": data.get("status"),
                "created_at": data.get("created_at"),
                "app_url": data.get("app_url"),
                "dock": data.get("dock", []),
            }, indent=2)

        elif name == "get_project_people":
            pid = args["project_id"]
            data = await bc_get_paginated(f"{base}/projects/{pid}/people.json", token)
            people = [{"id": p.get("id"), "name": p.get("name"), "email": p.get("email_address"), "title": p.get("title")} for p in data]
            return json.dumps(people, indent=2)

        elif name == "list_all_people":
            data = await bc_get_paginated(f"{base}/people.json", token)
            people = [{"id": p.get("id"), "name": p.get("name"), "email": p.get("email_address"), "title": p.get("title")} for p in data]
            return json.dumps(people, indent=2)

        elif name == "list_todolists":
            pid = args["project_id"]
            # Get the project to find the todoset ID from the dock
            project = await bc_get(f"{base}/projects/{pid}.json", token)
            todoset = next((d for d in project.get("dock", []) if d.get("name") == "todoset"), None)
            if not todoset:
                return json.dumps({"error": "This project has no to-do lists enabled."})
            todoset_id = todoset["id"]
            data = await bc_get(f"{base}/buckets/{pid}/todosets/{todoset_id}/todolists.json", token)
            lists = [{"id": tl.get("id"), "name": tl.get("title"), "completed_ratio": tl.get("completed_ratio"), "app_url": tl.get("app_url")} for tl in (data if isinstance(data, list) else [])]
            return json.dumps(lists, indent=2)

        elif name == "list_todos":
            pid = args["project_id"]
            tlid = args["todolist_id"]
            params = {}
            if args.get("completed"):
                params["completed"] = "true"
            data = await bc_get_paginated(f"{base}/buckets/{pid}/todolists/{tlid}/todos.json", token, params=params)
            todos = []
            for t in data:
                todos.append({
                    "id": t.get("id"),
                    "title": t.get("title"),
                    "completed": t.get("completed"),
                    "due_on": t.get("due_on"),
                    "assignees": [a.get("name") for a in t.get("assignees", [])],
                    "app_url": t.get("app_url"),
                })
            return json.dumps(todos, indent=2)

        elif name == "get_todo":
            pid = args["project_id"]
            tid = args["todo_id"]
            data = await bc_get(f"{base}/buckets/{pid}/todos/{tid}.json", token)
            import html
            import re as _re
            desc_html = data.get("description", "") or ""
            desc_text = _re.sub(r"<[^>]+>", " ", desc_html).strip()
            return json.dumps({
                "id": data.get("id"),
                "title": data.get("title"),
                "description": desc_text,
                "completed": data.get("completed"),
                "due_on": data.get("due_on"),
                "starts_on": data.get("starts_on"),
                "assignees": [a.get("name") for a in data.get("assignees", [])],
                "creator": data.get("creator", {}).get("name"),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
                "comments_count": data.get("comments_count", 0),
                "app_url": data.get("app_url"),
            }, indent=2)

        elif name == "list_messages":
            pid = args["project_id"]
            project = await bc_get(f"{base}/projects/{pid}.json", token)
            board = next((d for d in project.get("dock", []) if d.get("name") == "message_board"), None)
            if not board:
                return json.dumps({"error": "This project has no message board enabled."})
            board_id = board["id"]
            data = await bc_get_paginated(f"{base}/buckets/{pid}/message_boards/{board_id}/messages.json", token)
            messages = [{"id": m.get("id"), "subject": m.get("subject"), "created_at": m.get("created_at"), "creator": m.get("creator", {}).get("name"), "comments_count": m.get("comments_count", 0), "app_url": m.get("app_url")} for m in data]
            return json.dumps(messages, indent=2)

        elif name == "get_message":
            pid = args["project_id"]
            mid = args["message_id"]
            data = await bc_get(f"{base}/buckets/{pid}/messages/{mid}.json", token)
            import re as _re
            body_html = data.get("content", "") or ""
            body_text = _re.sub(r"<[^>]+>", " ", body_html).strip()
            return json.dumps({
                "id": data.get("id"),
                "subject": data.get("subject"),
                "content": body_text,
                "creator": data.get("creator", {}).get("name"),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
                "comments_count": data.get("comments_count", 0),
                "app_url": data.get("app_url"),
            }, indent=2)

        elif name == "list_comments":
            pid = args["project_id"]
            rid = args["recording_id"]
            data = await bc_get_paginated(f"{base}/buckets/{pid}/recordings/{rid}/comments.json", token)
            import re as _re
            comments = []
            for c in data:
                body_html = c.get("content", "") or ""
                body_text = _re.sub(r"<[^>]+>", " ", body_html).strip()
                comments.append({
                    "id": c.get("id"),
                    "content": body_text,
                    "creator": c.get("creator", {}).get("name"),
                    "created_at": c.get("created_at"),
                })
            return json.dumps(comments, indent=2)

        elif name == "list_documents":
            pid = args["project_id"]
            project = await bc_get(f"{base}/projects/{pid}.json", token)
            vault = next((d for d in project.get("dock", []) if d.get("name") == "vault"), None)
            if not vault:
                return json.dumps({"error": "This project has no Docs & Files vault enabled."})
            vault_id = vault["id"]
            data = await bc_get_paginated(f"{base}/buckets/{pid}/vaults/{vault_id}/documents.json", token)
            docs = [{"id": d.get("id"), "title": d.get("title"), "created_at": d.get("created_at"), "creator": d.get("creator", {}).get("name"), "app_url": d.get("app_url")} for d in data]
            return json.dumps(docs, indent=2)

        elif name == "get_document":
            pid = args["project_id"]
            did = args["document_id"]
            data = await bc_get(f"{base}/buckets/{pid}/documents/{did}.json", token)
            import re as _re
            body_html = data.get("content", "") or ""
            body_text = _re.sub(r"<[^>]+>", " ", body_html).strip()
            return json.dumps({
                "id": data.get("id"),
                "title": data.get("title"),
                "content": body_text,
                "creator": data.get("creator", {}).get("name"),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
                "app_url": data.get("app_url"),
            }, indent=2)

        elif name == "search_recordings":
            rec_type = args["type"]
            params = {"type": rec_type}
            if args.get("project_id"):
                params["bucket"] = args["project_id"]
            if args.get("sort"):
                params["sort"] = args["sort"]
            data = await bc_get_paginated(f"{base}/projects/recordings.json", token, params=params)
            # The API filters by type server-side; keep client-side filter as a safety net
            filtered = [r for r in data if r.get("type") == rec_type]
            results = [{"id": r.get("id"), "title": r.get("title"), "type": r.get("type"), "created_at": r.get("created_at"), "app_url": r.get("app_url")} for r in filtered[:50]]
            return json.dumps(results, indent=2)

        elif name == "create_todo":
            pid = args["project_id"]
            tlid = args["todolist_id"]
            body = {"content": args["content"]}
            if args.get("description"):
                body["description"] = args["description"]
            if args.get("due_on"):
                body["due_on"] = args["due_on"]
            if args.get("starts_on"):
                body["starts_on"] = args["starts_on"]
            if args.get("notify") is not None:
                body["notify"] = args["notify"]
            data = await bc_post(f"{base}/buckets/{pid}/todolists/{tlid}/todos.json", token, body)
            return json.dumps({
                "id": data.get("id"),
                "title": data.get("title"),
                "created": True,
                "app_url": data.get("app_url"),
            }, indent=2)

        elif name == "post_comment":
            pid = args["project_id"]
            rid = args["recording_id"]
            data = await bc_post(
                f"{base}/buckets/{pid}/recordings/{rid}/comments.json",
                token,
                {"content": args["content"]},
            )
            return json.dumps({
                "id": data.get("id"),
                "content": data.get("content"),
                "creator": data.get("creator", {}).get("name"),
                "created_at": data.get("created_at"),
                "posted": True,
            }, indent=2)

        else:
            return json.dumps({"error": f"Unknown tool: {name}"})

    except HTTPException as e:
        return json.dumps({"error": e.detail})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# MCP Protocol Handler
# ---------------------------------------------------------------------------

async def handle_mcp_request(body: dict, token: str) -> dict:
    method = body.get("method")
    req_id = body.get("id")
    params = body.get("params", {})

    if method == "initialize":
        # Negotiate protocol version — accept what the client requests if we support it,
        # otherwise fall back to our latest supported version.
        SUPPORTED_VERSIONS = {"2025-06-18", "2025-03-26", "2024-11-05"}
        LATEST_VERSION = "2025-06-18"
        requested = params.get("protocolVersion", LATEST_VERSION)
        negotiated = requested if requested in SUPPORTED_VERSIONS else LATEST_VERSION
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "basecamp-mcp", "version": "1.0.0"}
            }
        }

    elif method == "notifications/initialized":
        return None  # No response needed for notifications

    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS}
        }

    elif method == "tools/call":
        tool_name = params.get("name")
        tool_args = params.get("arguments", {})
        result_text = await execute_tool(tool_name, tool_args, token)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": result_text}]
            }
        }

    else:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}
        }


# ---------------------------------------------------------------------------
# OAuth 2.0 Proxy Endpoints
# Claude cowork expects the MCP server to be an OAuth 2.0 authorization server
# that proxies through to Basecamp's OAuth.
# ---------------------------------------------------------------------------

@app.get("/.well-known/oauth-authorization-server")
async def oauth_metadata():
    """RFC 8414 — OAuth 2.0 Authorization Server Metadata (used by Claude for discovery)."""
    base = SERVER_BASE_URL
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
    })


@app.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource():
    """RFC 9728 — OAuth 2.0 Protected Resource Metadata (MCP resource server discovery)."""
    base = SERVER_BASE_URL
    return JSONResponse({
        "resource": base,
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{base}/health",
    })


@app.get("/authorize")
async def oauth_authorize(request: Request):
    """
    OAuth authorization endpoint.
    Claude redirects the user here; we proxy them through Basecamp's OAuth.

    Stateless: Claude's redirect_uri and state are encoded into a signed token
    that travels as Basecamp's `state` parameter — no server-side storage needed.
    """
    params = dict(request.query_params)
    claude_redirect_uri = params.get("redirect_uri", "")
    claude_state = params.get("state", "")

    if not BASECAMP_CLIENT_ID:
        raise HTTPException(status_code=500, detail="BASECAMP_CLIENT_ID not configured")

    # Encode Claude's context into a signed state token for Basecamp
    our_state = _make_signed_token({
        "r": claude_redirect_uri,
        "s": claude_state,
    })

    our_callback = f"{SERVER_BASE_URL}/oauth/callback"
    basecamp_url = (
        f"{BASECAMP_AUTH_BASE}/authorization/new"
        f"?type=web_server"
        f"&client_id={urllib.parse.quote(BASECAMP_CLIENT_ID)}"
        f"&redirect_uri={urllib.parse.quote(our_callback, safe='')}"
        f"&state={urllib.parse.quote(our_state, safe='')}"
    )
    return RedirectResponse(url=basecamp_url, status_code=302)


@app.get("/oauth/callback")
async def oauth_callback(code: str = "", state: str = "", error: str = ""):
    """
    Basecamp redirects back here after user authorization.

    Stateless: Claude's context is recovered by verifying and decoding the signed
    state token. The Basecamp access_token is itself signed and sent back to Claude
    as the auth code — no server-side storage needed.
    """
    state_data = _decode_signed_token(state)
    if not state_data:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

    claude_redirect = state_data.get("r", "")
    claude_state = state_data.get("s", "")

    if error:
        return RedirectResponse(
            url=f"{claude_redirect}?error={urllib.parse.quote(error)}",
            status_code=302
        )

    if not BASECAMP_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="BASECAMP_CLIENT_SECRET not configured")

    our_callback = f"{SERVER_BASE_URL}/oauth/callback"

    # Exchange Basecamp code for access token
    client = _http_client or httpx.AsyncClient(timeout=10.0)
    resp = await client.post(
        f"{BASECAMP_AUTH_BASE}/authorization/token",
        params={
            "type": "web_server",
            "client_id": BASECAMP_CLIENT_ID,
            "client_secret": BASECAMP_CLIENT_SECRET,
            "redirect_uri": our_callback,
            "code": code,
        },
        headers={"User-Agent": USER_AGENT},
    )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Basecamp token exchange failed: {resp.text}"
        )
    token_data = resp.json()

    # Encode the token as a signed auth code — no server-side storage
    auth_code = _make_signed_token({
        "at": token_data.get("access_token"),
        "rt": token_data.get("refresh_token"),
        "ei": token_data.get("expires_in"),
        "tt": token_data.get("token_type", "Bearer"),
    })

    callback_url = f"{claude_redirect}?code={urllib.parse.quote(auth_code, safe='')}"
    if claude_state:
        callback_url += f"&state={urllib.parse.quote(claude_state, safe='')}"

    return RedirectResponse(url=callback_url, status_code=302)


@app.post("/token")
async def oauth_token(request: Request):
    """
    Token endpoint — Claude exchanges the signed auth code for the Basecamp access token.
    Stateless: the code IS the signed token containing the access_token.
    """
    content_type = request.headers.get("content-type", "")
    raw = await request.body()

    body: dict = {}
    if "application/json" in content_type:
        try:
            body = json.loads(raw)
        except Exception:
            pass
    elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        try:
            form = await request.form()
            body = dict(form)
        except Exception:
            body = dict(urllib.parse.parse_qsl(raw.decode("utf-8", errors="replace")))
    else:
        try:
            body = json.loads(raw)
        except Exception:
            body = dict(urllib.parse.parse_qsl(raw.decode("utf-8", errors="replace")))

    code = body.get("code")
    if not code:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request", "error_description": "Missing code"}
        )

    token_data = _decode_signed_token(code)
    if not token_data or "at" not in token_data:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_grant", "error_description": "Invalid or expired code"}
        )

    return JSONResponse({
        "access_token": token_data["at"],
        "token_type": token_data.get("tt", "Bearer"),
        "refresh_token": token_data.get("rt"),
        "expires_in": token_data.get("ei"),
        "scope": "",
    })


# ---------------------------------------------------------------------------
# HTTP Endpoints
# ---------------------------------------------------------------------------

@app.head("/")
async def head_root():
    """Required: Claude uses this to detect MCP protocol version."""
    return Response(
        headers={"MCP-Protocol-Version": "2025-06-18"},
        status_code=200
    )

@app.get("/")
async def get_root():
    """Return 405 with Allow header — tells Claude this is a POST-only MCP server."""
    return Response(
        status_code=405,
        headers={"Allow": "POST"}
    )

@app.post("/")
async def mcp_endpoint(request: Request):
    """Main MCP Streamable HTTP endpoint."""
    # Extract token
    try:
        token = extract_token(request)
    except HTTPException as e:
        return JSONResponse(
            status_code=401,
            content={"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": e.detail}}
        )

    # Parse body
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        )

    # Handle batch or single request
    session_id = request.headers.get("Mcp-Session-Id") or str(uuid.uuid4())

    if isinstance(body, list):
        responses = []
        for req in body:
            resp = await handle_mcp_request(req, token)
            if resp is not None:
                responses.append(resp)
        return JSONResponse(
            content=responses,
            headers={"Mcp-Session-Id": session_id}
        )
    else:
        method = body.get("method", "")
        # Notifications don't get a response
        if method.startswith("notifications/"):
            await handle_mcp_request(body, token)
            return Response(status_code=202)

        result = await handle_mcp_request(body, token)
        return JSONResponse(
            content=result,
            headers={"Mcp-Session-Id": session_id}
        )


@app.get("/health")
async def health():
    return {"status": "ok", "server": "basecamp-mcp"}
