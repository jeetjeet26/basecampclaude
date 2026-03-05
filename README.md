# Basecamp MCP Connector for Claude Cowork

A read-only MCP server that connects Claude to your Basecamp account. Exposes 14 tools covering projects, people, to-dos, messages, comments, and documents.

---

## Tools Available

| Tool | Description |
|------|-------------|
| `get_my_profile` | Your Basecamp profile |
| `list_projects` | All active (or archived) projects |
| `get_project` | Full project detail + enabled tools |
| `get_project_people` | Everyone on a project |
| `list_all_people` | Everyone in the account |
| `list_todolists` | All to-do lists in a project |
| `list_todos` | To-do items in a list (with completion filter) |
| `get_todo` | Full to-do detail with assignees, due date, description |
| `list_messages` | All messages on a project's message board |
| `get_message` | Full message body |
| `list_comments` | Comments on any recording (message, to-do, doc) |
| `list_documents` | All docs in a project's vault |
| `get_document` | Full document content |
| `search_recordings` | Browse any recording type across projects |

---

## Step 1 — Register a Basecamp OAuth App

1. Go to **https://launchpad.37signals.com/integrations**
2. Click **Register one now**
3. Fill in:
   - **Name**: Claude Cowork
   - **Website**: anything (e.g. `https://yourco.com`)
   - **Redirect URI**: `https://YOUR-APP-NAME.herokuapp.com/oauth/callback`
     *(you can update this after deploying — use a placeholder for now)*
4. Save — you'll get a **Client ID** and **Client Secret**. Keep these.

---

## Step 2 — Deploy to Heroku

```bash
# Clone / copy this folder, then:
cd basecamp-mcp
git init
git add .
git commit -m "Initial commit"

# Create the Heroku app
heroku create YOUR-APP-NAME

# Deploy
git push heroku main

# Verify it's running
curl https://YOUR-APP-NAME.herokuapp.com/health
```

No environment variables are needed — the server is stateless and reads
the Basecamp OAuth token directly from Claude's `Authorization` header on
each request.

---

## Step 3 — Update Your Basecamp OAuth Redirect URI

Go back to **https://launchpad.37signals.com/integrations** and update the
Redirect URI to:

```
https://YOUR-APP-NAME.herokuapp.com/oauth/callback
```

*(This URI isn't actually used by the MCP server itself — it's needed for
the OAuth app registration to be valid. Claude handles the actual OAuth
flow.)*

---

## Step 4 — Add to Claude Cowork

1. Open Claude → **Settings → Connectors**
2. Click **Add custom connector** (bottom of the list)
3. Fill in:
   - **Name**: Basecamp
   - **URL**: `https://YOUR-APP-NAME.herokuapp.com/`
   - Click **Advanced settings**:
     - **OAuth Client ID**: *(your Client ID from Step 1)*
     - **OAuth Client Secret**: *(your Client Secret from Step 1)*
4. Click **Add**
5. Claude will redirect you to Basecamp to authorize — click **Allow**
6. Done! You'll see a green connected status.

---

## Step 5 — Try It

Ask Claude things like:

- *"List all my active Basecamp projects"*
- *"What are the open to-dos on the [Project Name] project?"*
- *"Show me the latest messages from [Project Name]"*
- *"Who is on the [Project Name] project?"*
- *"Get the full content of document [ID]"*

---

## Architecture Notes

- **Transport**: MCP Streamable HTTP (2025-06-18 spec) — no SSE
- **Auth**: Stateless — Claude passes the Basecamp OAuth Bearer token on every request; the server never stores tokens
- **Read-only**: All tools use GET requests only; no write operations are exposed
- **Pagination**: Automatically follows Basecamp's `Link` header pagination (up to 5 pages per call)
- **Account ID**: Resolved automatically from the token via `/authorization.json`

---

## Local Development

```bash
pip install -r requirements.txt
uvicorn src.server:app --reload --port 8000
```

Test with the MCP Inspector:
```bash
npx @modelcontextprotocol/inspector
# Connect to: http://localhost:8000
# Add Authorization header: Bearer YOUR_BASECAMP_TOKEN
```

To get a test token, complete the OAuth flow manually:
```
https://launchpad.37signals.com/authorization/new?type=web_server&client_id=YOUR_CLIENT_ID&redirect_uri=YOUR_REDIRECT_URI
```
