"""
Basecamp MCP Server — Read-Only Connector for Claude Cowork
Implements MCP Streamable HTTP transport (2025-06-18 spec)
"""

import json
import re
import uuid
import httpx
import asyncio
from typing import Any
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Basecamp MCP Connector")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASECAMP_API_BASE = "https://3.basecampapi.com"
USER_AGENT = "BasecampMCP-Claude/1.0 (claude-connector)"

# ---------------------------------------------------------------------------
# Basecamp API client
# ---------------------------------------------------------------------------

async def bc_get(path: str, token: str, params: dict = None) -> dict | list:
    """Make an authenticated GET request to the Basecamp API."""
    url = f"{BASECAMP_API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, params=params or {}, follow_redirects=True)
        if resp.status_code == 401:
            raise HTTPException(status_code=401, detail="Basecamp token invalid or expired")
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Not found: {path}")
        resp.raise_for_status()
        return resp.json()


async def bc_get_paginated(path: str, token: str, params: dict = None, max_pages: int = 5) -> list:
    """Fetch all pages from a paginated Basecamp endpoint."""
    url = f"{BASECAMP_API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
    }
    results = []
    page = 1
    # Only pass params on the first request; subsequent pages use the full next URL from Link header
    first_request_params = params or {}
    async with httpx.AsyncClient() as client:
        while url and page <= max_pages:
            req_params = first_request_params if page == 1 else {}
            resp = await client.get(url, headers=headers, params=req_params, follow_redirects=True)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                results.extend(data)
            else:
                results.append(data)
            # Follow Link header for next page
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
            # href = https://3.basecampapi.com/ACCOUNT_ID
            match = re.search(r"/(\d+)$", href)
            if match:
                return match.group(1)
    raise HTTPException(status_code=400, detail="No Basecamp 4 account found for this token")


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
    }
]


# ---------------------------------------------------------------------------
# Tool Execution
# ---------------------------------------------------------------------------

async def execute_tool(name: str, args: dict, token: str) -> str:
    """Execute a named tool and return a text result."""

    # First, resolve account_id from the token
    auth_data = await bc_get("/authorization.json", token)
    account_id = extract_account_id(auth_data)
    base = f"/{account_id}"

    try:
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
