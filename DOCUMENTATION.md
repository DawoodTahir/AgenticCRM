# Agentic CRM — Project Documentation

> AI-powered sales intelligence platform for Goldenberry Farms.
> Syncs leads from Monday.com, Gmail, and WhatsApp into a unified contact database,
> then exposes an intelligent Telegram bot powered by GPT-4o for querying sales data.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Data Flow](#data-flow)
3. [File Structure](#file-structure)
4. [Entry Point — main.py](#entry-point--mainpy)
5. [Database Layer — db/](#database-layer--db)
6. [Connectors — connectors/](#connectors--connectors)
7. [Agent Layer — agents/](#agent-layer--agents)
8. [Bot Layer — bot/](#bot-layer--bot)
9. [Logging — logger.py](#logging--loggerpy)
10. [Security Architecture](#security-architecture)
11. [Environment Variables](#environment-variables)
12. [Setup & Usage](#setup--usage)

---

## Architecture Overview

```
                          Monday.com
                              |
                     (GraphQL API sync)
                              |
    WhatsApp (.zip) ──> [ PostgreSQL + pgvector ] <── Gmail (OAuth2 API)
                              |
                    ┌─────────┴──────────┐
                    |                    |
              contacts table       leads table
              (unified hub)       (Monday data)
                    |                    |
                    └────────┬───────────┘
                             |
                   [ OpenAI Embeddings ]
                   (text-embedding-3-small)
                             |
                   contact_embeddings table
                   (vector 1536 dims)
                             |
                    ┌────────┴────────┐
                    |                 |
              [ LangGraph Agent ]    |
              (GPT-4o + 8 tools)     |
                    |                 |
              [ Telegram Bot ]       |
              (webhook / polling)    |
                    |                 |
                  User           Semantic Search
```

**Four layers:**
1. **Data Ingestion** — Connectors pull data from Monday.com, Gmail, WhatsApp
2. **Database** — PostgreSQL stores leads, contacts, notes, embeddings; pgvector enables semantic search
3. **Agent** — LangGraph ReAct agent with 8 CRM tools, persistent conversation memory via PostgresSaver
4. **Bot** — Telegram interface with inline buttons, prompt injection guards, output sanitization

---

## Data Flow

### Ingestion Pipeline

```
Monday.com API  →  parse_column_values()  →  upsert_lead()       →  leads table
                →  item.updates[]         →  upsert_lead_note()   →  lead_notes table

Gmail API       →  parse headers/body     →  find_or_create_contact()  →  contacts table
                →  content_hash dedup     →  INSERT contact_embeddings

WhatsApp .zip   →  parse_chat_file()      →  find_or_create_contact()  →  contacts table
                →  chunk by 20 messages   →  INSERT contact_embeddings
```

### Contact Resolution

```
leads table  →  resolve_monday_contacts()  →  find_or_create_contact()
                                                |
                                    4-level match hierarchy:
                                    1. Exact email match
                                    2. Exact phone match
                                    3. Exact name match
                                    4. Create new contact
```

### Embedding Generation

```
contact_embeddings (embedding IS NULL)  →  embed_all_pending()
                                        →  OpenAI text-embedding-3-small
                                        →  1536-dim vector stored in DB
```

### Query Flow

```
User (Telegram)  →  is_injection() check
                 →  ask(question, thread_id)
                 →  LangGraph ReAct loop (think → tool call → read result → repeat)
                 →  tool_* functions  →  SQL queries + semantic_search
                 →  sanitize_output()
                 →  Telegram response (split if >4096 chars)
```

---

## File Structure

```
agentic-crm/
├── main.py                     # CLI entry point
├── logger.py                   # Structured logging (structlog + JSON)
├── Procfile                    # Render.com deployment config
├── requirements.txt            # Python dependencies
├── setup.py                    # Package setup
├── .gitignore
│
├── db/
│   ├── __init__.py
│   ├── schema.sql              # Full database schema (6 tables)
│   └── model.py                # All DB functions (read/write/search)
│
├── connectors/
│   ├── __init__.py
│   ├── monday.py               # Monday.com GraphQL sync
│   ├── gmail.py                # Gmail API sync (3 accounts)
│   ├── gmail_auth.py           # Gmail OAuth2 authentication
│   ├── whatsapp.py             # WhatsApp .zip export parser
│   └── embeddings.py           # OpenAI embedding generation
│
├── agents/
│   ├── __init__.py
│   ├── lead_agent.py           # LangGraph ReAct agent + system prompt
│   └── tools.py                # 8 LangChain tool definitions
│
└── bot/
    ├── __init__.py
    └── telegram_bot.py         # Telegram bot with security guards
```

---

## Entry Point — main.py

The CLI orchestrator. Run with `python main.py <command>`.

| Command | What it does |
|---------|-------------|
| `monday` | Sync all leads from Monday.com board |
| `resolve` | Create/link contacts from synced leads |
| `gmail` | Sync emails from 3 Gmail accounts |
| `whatsapp` | Parse WhatsApp .zip exports |
| `embed` | Generate embeddings for unembedded content |
| `merge` | Find and merge duplicate contacts by name |
| `bot` | Start the Telegram bot |
| `ask <question>` | Query the agent directly from terminal |
| *(no args)* | Runs Monday sync, then starts bot |

**Typical full sync order:**
```bash
python main.py monday      # 1. Pull leads
python main.py resolve     # 2. Create contacts from leads
python main.py gmail       # 3. Sync emails
python main.py whatsapp    # 4. Sync WhatsApp chats
python main.py embed       # 5. Generate embeddings
python main.py merge       # 6. Merge duplicates
python main.py bot         # 7. Start Telegram bot
```

---

## Database Layer — db/

### schema.sql — 6 Tables

#### 1. `leads`
Primary lead data from Monday.com. One row per Monday board item.

| Column | Type | Description |
|--------|------|-------------|
| `id` | SERIAL PK | Internal ID |
| `monday_item_id` | TEXT UNIQUE | Monday item ID |
| `monday_board_id` | TEXT | Board ID |
| `monday_group_id` / `monday_group_name` | TEXT | Pipeline stage group |
| `name`, `first_name`, `last_name` | TEXT | Contact name fields |
| `email`, `phone`, `company` | TEXT | Contact info |
| `lead_score` | TEXT | Lead priority score |
| `status` | TEXT | Board status column |
| `sequence_status` | TEXT | Outreach sequence progress |
| `sba` | TEXT | SBA status |
| `cim_sent` | TEXT | CIM sent status |
| `is_broker` | BOOLEAN | Broker flag |
| `proof_of_funds` | BOOLEAN | Proof of funds flag |
| `industry_tags` | TEXT | Industry categories |
| `listing_number` | TEXT | Listing reference |
| `notes_text` | TEXT | Inline notes |
| `assigned_to_name` | TEXT | Assigned salesperson |
| `start_date`, `sequence_start_date`, `date_sent` | DATE | Key dates |
| `raw_column_values` | JSONB | Full Monday column data |
| `monday_created_at`, `monday_updated_at` | TIMESTAMPTZ | Monday timestamps |
| `ingested_at`, `last_synced_at` | TIMESTAMPTZ | Sync timestamps |

#### 2. `lead_notes`
Comments/updates from Monday board items.

| Column | Type | Description |
|--------|------|-------------|
| `monday_update_id` | TEXT UNIQUE | Monday update ID |
| `lead_id` | INTEGER FK → leads | Parent lead |
| `body_html`, `body_text` | TEXT | Note content |
| `creator_name`, `creator_email` | TEXT | Who wrote it |
| `monday_created_at` | TIMESTAMPTZ | When written |

#### 3. `sync_state`
Tracks pagination cursors for resumable syncs.

| Column | Type | Description |
|--------|------|-------------|
| `source` | TEXT UNIQUE | Source identifier (e.g. `monday_board_123`) |
| `cursor` | TEXT | Pagination cursor |
| `total_items_synced` | INTEGER | Running total |

#### 4. `contacts`
**Identity resolution hub** — links the same person across all sources.

| Column | Type | Description |
|--------|------|-------------|
| `name`, `email`, `phone`, `company` | TEXT | Unified contact info |
| `monday_lead_id` | INTEGER FK → leads | Linked Monday lead |
| `in_monday`, `in_gmail`, `in_whatsapp` | BOOLEAN | Source flags |
| `resolution_status` | TEXT | `auto` or `manual` |

Unique partial indexes on `email` and `phone` (WHERE NOT NULL) prevent duplicates.

#### 5. `contact_review_flags`
Uncertain matches needing human confirmation.

#### 6. `contact_embeddings`
Vectorized text chunks from all sources for semantic search.

| Column | Type | Description |
|--------|------|-------------|
| `contact_id` | INTEGER FK → contacts | Linked contact |
| `source` | TEXT | `gmail`, `whatsapp`, etc. |
| `content_text` | TEXT | Raw text chunk |
| `embedding` | vector(1536) | OpenAI embedding vector |
| `content_hash` | TEXT UNIQUE | SHA hash for deduplication |

Uses IVFFlat index with `vector_cosine_ops` for fast similarity search.

---

### model.py — Database Functions

#### Connection
- **`get_connection()`** — Returns psycopg2 connection from `DATABASE_URL`

#### Write Operations

- **`upsert_lead(conn, data: dict)`**
  Inserts or updates a lead. Uses `ON CONFLICT (monday_item_id) DO UPDATE` to handle re-syncs.
  Converts `raw_column_values` to JSONB via `psycopg2.extras.Json`.

- **`upsert_lead_note(conn, data: dict)`**
  Inserts a note. `ON CONFLICT (monday_update_id) DO NOTHING` — idempotent.

- **`save_sync_state(conn, source, cursor, items_added)`**
  Saves pagination cursor. Increments `total_items_synced` on conflict.

- **`get_sync_cursor(conn, source) → Optional[str]`**
  Retrieves saved cursor for a source.

#### Read Operations

- **`get_leads_by_status(conn, status) → list[dict]`**
  Case-insensitive match on `status` column. Returns up to 50 leads ordered by recency.

- **`get_stale_leads(conn, days=30) → list[dict]`**
  Leads not updated in N days, excluding closed/won/lost. Ordered oldest first.

- **`search_leads(conn, query) → list[dict]`**
  ILIKE search across name, email, company. Up to 20 results.

- **`get_lead_with_notes(conn, lead_id) → Optional[dict]`**
  Full lead record plus all associated notes.

- **`get_summary_stats(conn) → dict`**
  Three breakdowns: `by_pipeline_stage` (Monday group), `by_lead_score`, `by_sequence_status`.

#### Contact Resolution

- **`normalize_phone(phone) → Optional[str]`**
  Strips non-digits: `+1 (647) 123-4567` → `16471234567`

- **`find_or_create_contact(conn, name, email, phone, company, lead_id) → (contact_id, method)`**
  4-level matching hierarchy:
  1. **Email match** — exact, case-insensitive
  2. **Phone match** — normalized digits
  3. **Name match** — case-insensitive
  4. **Create new** — if nothing matches

  On match, fills in missing fields via `COALESCE` with safety checks to prevent unique index violations. Returns `(contact_id, method)` where method is one of `email_match`, `phone_match`, `name_match`, `created`.

- **`resolve_monday_contacts(conn) → dict`**
  Loops through all leads, calls `find_or_create_contact` for each. Returns stats with counts per method. Safe to re-run.

- **`merge_duplicate_contacts(conn) → dict`**
  Finds contacts sharing the same name (case-insensitive). Picks the contact with the most filled fields as primary. Moves embeddings, deletes duplicates, merges missing fields via COALESCE. Returns `{groups_found, contacts_merged}`.

#### Semantic Search

- **`semantic_search(conn, query_embedding, contact_id=None, source=None, limit=10) → list[dict]`**
  Cosine distance search using pgvector's `<=>` operator. Optional filters by contact and source. Returns `content_text`, `source`, `contact_name`, `distance` ordered by closest match.

---

## Connectors — connectors/

### monday.py — Monday.com Sync

Syncs leads and notes from board **"CRM - Buyers & Buyer Deals"** (ID: 18408828473) via GraphQL API.

- **`run_query(query) → dict`**
  Sends GraphQL query to Monday API with auth header. Returns parsed JSON.

- **`parse_column_values(column_values) → dict`**
  Extracts 19 fields by matching **column title** (not column type). Handles:
  - Email/Phone — from `email`/`phone` type or title containing "email"/"phone"
  - Names — "first name" / "last name" in title
  - People — `people` type or "assigned" in title
  - Business Name — "business name" in title (dropdown)
  - Checkboxes — `parsed.get("checked")` for broker/proof_of_funds
  - Dates — parses `YYYY-MM-DD` from date columns matched by title
  - Status columns — extracts label, matched by title ("lead score", "sequence status", "sba", etc.)
  - Tags/Text — industry, notes, listing number

- **`fetch_page(cursor) → (items, next_cursor)`**
  Cursor-based pagination, 100 items per page. First page uses `items_page`, subsequent pages use `next_items_page`. Fetches item data, column values, group info, and up to 50 updates (notes) per item.

- **`sync_monday_leads()`**
  Main sync loop:
  1. Always does a full sync (no cursor resume)
  2. Paginates through all items
  3. For each item: parses columns → upserts lead → fetches internal ID → upserts all notes
  4. Commits after each page
  5. Saves sync state with cursor
  6. Rolls back on error

### gmail.py — Gmail Sync

Syncs emails from 3 Gmail accounts into contacts + embeddings.

- **`get_email_body(payload) → str`**
  Extracts plain text body from Gmail message payload. Handles direct body and multipart payloads. Base64 decodes with UTF-8.

- **`sync_gmail(token_file, account_label)`**
  Per-account sync:
  1. Authenticates via `gmail_auth.authenticate()`
  2. Loads already-synced message IDs from DB (deduplication)
  3. Paginates through messages (100/page)
  4. For each new message: extracts headers → parses sender → `find_or_create_contact` → inserts embedding with content hash
  5. Commits per page

- **`sync_all_gmail()`**
  Calls `sync_gmail` for 3 accounts: `token_gmail1.pkl`, `token_gmail2.pkl`, `token_gmail3.pkl`.

### gmail_auth.py — Gmail OAuth2

- **`authenticate(token_file, credentials_file="creds.json") → Credentials`**
  Token refresh logic:
  1. Load pickled token if exists
  2. If expired: refresh with refresh_token
  3. If no token: run OAuth2 flow (opens browser)
  4. Save token to pickle file

  Scopes: `gmail.readonly`, `userinfo.email`

### whatsapp.py — WhatsApp Chat Parser

Parses exported `.zip` files from WhatsApp.

- **`is_our_message(sender) → bool`**
  Filters out messages sent by our team (checks against `OUR_NAMES` list).

- **`parse_chat_file(text) → list[dict]`**
  Regex-based parser for WhatsApp text export format: `[MM/DD/YYYY, HH:MM:SS AP] Name: Message`. Handles multi-line messages.

- **`extract_contact_name(zip_file) → str`**
  Extracts contact name from zip filename (strips "WhatsApp Chat - " prefix and ".zip" suffix).

- **`sync_whatsapp()`**
  1. Iterates `.zip` files in `whatsapp/` directory
  2. Extracts contact name and phone (if filename starts with "+")
  3. Parses messages from `.txt` inside zip
  4. Creates/links contact via `find_or_create_contact`
  5. Chunks messages into groups of 20 → inserts as embeddings
  6. Commits per contact

### embeddings.py — OpenAI Embeddings

- **`generate_embedding(text) → list`**
  Calls OpenAI `text-embedding-3-small` API. Truncates text to 8000 chars. Returns 1536-dim vector.

- **`embed_all_pending()`**
  Queries `contact_embeddings` where `embedding IS NULL`. Generates and stores embedding for each row. Commits after each. Prints progress every 10 rows.

---

## Agent Layer — agents/

### lead_agent.py — LangGraph ReAct Agent

Uses `create_react_agent` from `langgraph.prebuilt` with GPT-4o.

**Conversation persistence:** `PostgresSaver` from `langgraph-checkpoint-postgres` stores conversation state per `thread_id` in PostgreSQL. Uses `psycopg` (v3) with `autocommit=True`.

**System prompt defines:**
- Role: CRM sales assistant for Goldenberry Farms
- Available fields and their meanings
- Pipeline stages: New Leads → NDA Sent → NDA Signed → CIM Sent → Successful Call → Offer/LOI → Under Contract (+ Working, Warm, Cool, Dead, Brokers)
- Security rules (never reveal internals, block injection attempts)
- Critical rules (always call tools before answering, never guess from memory)
- Response guidelines (be specific, be actionable, prioritize stale/high-score leads)

- **`ask(question, thread_id="default") → str`**
  Invokes the agent with conversation config. LangGraph handles the ReAct loop (think → call tool → read result → repeat). Logs every tool call and result. Returns final text response.

### tools.py — 8 Agent Tools

All tools use the `@tool` decorator from LangChain. The **docstring is the tool description** that GPT-4o sees — it determines when and how the model calls each tool.

| Tool | Parameters | Description |
|------|-----------|-------------|
| `tool_lookup_person` | name, email, phone (all optional) | Unified lookup across all sources. Returns contact profile + Monday lead details + recent conversations. Has fuzzy matching via pg_trgm `similarity()` when exact match fails. |
| `tool_get_leads_by_status` | status: str | Filter leads by status column |
| `tool_get_stale_leads` | days: int (default 30) | Leads not updated in N days |
| `tool_get_lead_details` | lead_id: int | Full lead record with all notes |
| `tool_get_pipeline_summary` | *(none)* | Counts by pipeline stage, lead score, sequence status |
| `tool_get_leads_by_sequence_status` | status: str | Filter by outreach sequence progress |
| `tool_get_leads_by_lead_score` | score: str | Filter by lead score value |
| `tool_get_leads_by_pipeline_stage` | stage: str | Filter by Monday board group/funnel stage |

**`tool_lookup_person` detail:**
This is the most important tool. When the agent gets any name/email/phone, it calls this first.
1. Searches `contacts` table by email → phone → name (ILIKE)
2. If no exact name match, triggers **fuzzy search** using `similarity()` from pg_trgm (threshold > 0.2)
3. If multiple matches found, returns list for user disambiguation (Telegram buttons)
4. For single match, returns:
   - Contact profile (name, email, phone, company, which sources they appear in)
   - Monday lead details (pipeline stage, scores, statuses, dates, notes)
   - Recent conversations from semantic search (WhatsApp/Gmail previews)

---

## Bot Layer — bot/

### telegram_bot.py — Telegram Interface

**Handlers:**

- **`/start`** — Welcome message with example queries
- **Message handler** — Main flow:
  1. Optional user whitelist check (`ALLOWED_USER_IDS`)
  2. Input injection guard (`is_injection()`)
  3. Send "Thinking..." placeholder
  4. Call `ask()` agent
  5. Output sanitization (`sanitize_output()`)
  6. If multiple contacts matched → show inline keyboard buttons
  7. Split response if >4096 chars (Telegram limit)
- **Callback handler** — When user taps a contact button, re-queries agent with selected name

**Security functions:**
- `is_injection(text) → bool` — 12 regex patterns catching prompt injection ("ignore previous", "you are now", "system prompt", etc.)
- `sanitize_output(text) → str` — Blocks 14 sensitive terms (table names, column names, SQL keywords, library names)

**Inline buttons:**
- `parse_contact_buttons(answer) → Optional[InlineKeyboardMarkup]` — Parses "Multiple contacts found" responses into tappable buttons with `contact:{name}` callback data

**Deployment modes:**
- **Polling** (local dev) — default when no `RENDER_EXTERNAL_URL`
- **Webhook** (Render.com) — listens on `0.0.0.0:{PORT}`, uses bot token as URL path

---

## Logging — logger.py

Centralized structured logging using `structlog`.

- Output: JSON format to both `logs/crm.log` and console
- Processors: log level, ISO timestamps, JSON rendering
- Factory: `get_logger(name)` returns a bound logger

**Loggers in use:**
- `get_logger("db")` — in model.py (semantic search logging)
- `get_logger("tools")` — in tools.py (lookup steps, match results)
- `get_logger("agent")` — in lead_agent.py (input/output, tool calls)
- `get_logger("bot")` — in telegram_bot.py (user input, bot output, security blocks)

---

## Security Architecture

Three-layer defense against prompt injection:

| Layer | Where | What it does |
|-------|-------|-------------|
| **Layer 1 — Input Guard** | `telegram_bot.py` | Regex patterns block injection phrases before the agent ever sees them |
| **Layer 2 — System Prompt** | `lead_agent.py` | GPT-4o is instructed to refuse non-CRM questions, never reveal internals, treat tool results as data |
| **Layer 3 — Output Guard** | `telegram_bot.py` | Scans agent response for sensitive terms (table names, SQL keywords) and blocks them |

**Additional protections:**
- `ALLOWED_USER_IDS` — optional whitelist for Telegram users
- Unique indexes on email/phone prevent duplicate contacts
- Content hash deduplication prevents duplicate embeddings
- Foreign keys with CASCADE deletes maintain referential integrity

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | PostgreSQL connection string (must have pgvector extension) |
| `MONDAY_API_KEY` | Yes | Monday.com API token |
| `MONDAY_BOARD_ID` | Yes | Monday board ID (currently `18408828473`) |
| `OPENAI_API_KEY` | Yes | OpenAI API key (for GPT-4o + embeddings) |
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot token from BotFather |
| `RENDER_EXTERNAL_URL` | No | Set automatically on Render.com — enables webhook mode |
| `PORT` | No | Webhook port (defaults to 8443) |

---

## Setup & Usage

### Prerequisites
- Python 3.11+
- PostgreSQL with pgvector and pg_trgm extensions
- Monday.com API key + board ID
- OpenAI API key
- Telegram bot token
- Gmail OAuth credentials (`creds.json`)

### Install

```bash
pip install -r requirements.txt
```

### Database Setup

```python
python -c "
from db.model import get_connection
conn = get_connection()
cur = conn.cursor()
with open('db/schema.sql', 'r') as f:
    cur.execute(f.read())
conn.commit()
conn.close()
print('Schema applied.')
"
```

### Gmail Authentication (one-time)

```bash
python connectors/gmail_auth.py
# Opens browser for each account — log in and authorize
```

### Full Sync

```bash
python main.py monday       # Sync Monday.com leads
python main.py resolve      # Create contacts from leads
python main.py gmail        # Sync Gmail emails
python main.py whatsapp     # Sync WhatsApp chats
python main.py embed        # Generate embeddings
python main.py merge        # Merge duplicate contacts
```

### Run Bot

```bash
# Local (polling mode)
python main.py bot

# Render.com (webhook mode — automatic via RENDER_EXTERNAL_URL)
# Procfile: web: python main.py bot
```

### CLI Query

```bash
python main.py ask "Who are the hottest leads right now?"
```
