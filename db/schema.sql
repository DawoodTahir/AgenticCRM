-- ============================================================
-- AGENTIC CRM — PostgreSQL Schema
-- Safe to re-run: all CREATE statements use IF NOT EXISTS
-- ============================================================

CREATE EXTENSION IF NOT EXISTS vector;


-- ============================================================
-- TABLE 1: leads
-- ============================================================

CREATE TABLE IF NOT EXISTS leads (

    id                  SERIAL PRIMARY KEY,
    monday_item_id      TEXT        NOT NULL UNIQUE,
    monday_board_id     TEXT        NOT NULL,
    monday_group_id     TEXT,
    monday_group_name   TEXT,

    name                TEXT,
    email               TEXT,
    phone               TEXT,
    company             TEXT,
    location            TEXT,
    website             TEXT,

    client_status       TEXT,
    spanish_speaking    TEXT,
    position            TEXT,
    value_level         TEXT,
    mood                TEXT,
    follow_up_status    TEXT,
    sentiment           TEXT,

    due_date            DATE,
    notes_text          TEXT,
    assigned_to_name    TEXT,
    raw_column_values   JSONB,

    monday_created_at   TIMESTAMPTZ,
    monday_updated_at   TIMESTAMPTZ,

    ingested_at         TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    last_synced_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()

);

CREATE INDEX IF NOT EXISTS idx_leads_email           ON leads (email);
CREATE INDEX IF NOT EXISTS idx_leads_client_status   ON leads (client_status);
CREATE INDEX IF NOT EXISTS idx_leads_follow_up       ON leads (follow_up_status);
CREATE INDEX IF NOT EXISTS idx_leads_monday_updated  ON leads (monday_updated_at);
CREATE INDEX IF NOT EXISTS idx_leads_board           ON leads (monday_board_id);


-- ============================================================
-- TABLE 2: lead_notes
-- ============================================================

CREATE TABLE IF NOT EXISTS lead_notes (

    id                  SERIAL PRIMARY KEY,
    monday_update_id    TEXT        NOT NULL UNIQUE,
    lead_id             INTEGER     NOT NULL REFERENCES leads (id) ON DELETE CASCADE,
    body_html           TEXT,
    body_text           TEXT,
    creator_name        TEXT,
    creator_email       TEXT,
    monday_created_at   TIMESTAMPTZ,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()

);

CREATE INDEX IF NOT EXISTS idx_lead_notes_lead_id ON lead_notes (lead_id);


-- ============================================================
-- TABLE 3: sync_state
-- ============================================================

CREATE TABLE IF NOT EXISTS sync_state (

    id                  SERIAL PRIMARY KEY,
    source              TEXT        NOT NULL UNIQUE,
    cursor              TEXT,
    last_synced_at      TIMESTAMPTZ,
    total_items_synced  INTEGER     NOT NULL DEFAULT 0

);


-- ============================================================
-- TABLE 4: contacts
-- Identity resolution hub — links same person across all sources
-- ============================================================

CREATE TABLE IF NOT EXISTS contacts (

    id                  SERIAL PRIMARY KEY,
    name                TEXT,
    email               TEXT,
    phone               TEXT,
    company             TEXT,
    monday_lead_id      INTEGER REFERENCES leads (id) ON DELETE SET NULL,
    in_monday           BOOLEAN     NOT NULL DEFAULT FALSE,
    in_gmail            BOOLEAN     NOT NULL DEFAULT FALSE,
    in_whatsapp         BOOLEAN     NOT NULL DEFAULT FALSE,
    resolution_status   TEXT        NOT NULL DEFAULT 'auto',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()

);

CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_email      ON contacts (email) WHERE email IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_phone      ON contacts (phone) WHERE phone IS NOT NULL;
CREATE INDEX        IF NOT EXISTS idx_contacts_monday_lead ON contacts (monday_lead_id);
CREATE INDEX        IF NOT EXISTS idx_contacts_resolution  ON contacts (resolution_status);


-- ============================================================
-- TABLE 5: contact_review_flags
-- Uncertain matches that need human confirmation
-- ============================================================

CREATE TABLE IF NOT EXISTS contact_review_flags (

    id                      SERIAL PRIMARY KEY,
    raw_name                TEXT,
    raw_email               TEXT,
    raw_phone               TEXT,
    source                  TEXT,
    suggested_contact_id    INTEGER REFERENCES contacts (id) ON DELETE SET NULL,
    match_reason            TEXT,
    status                  TEXT        NOT NULL DEFAULT 'pending',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()

);

CREATE INDEX IF NOT EXISTS idx_review_flags_status ON contact_review_flags (status);


-- ============================================================
-- TABLE 6: contact_embeddings
-- Text chunks from all sources, vectorized for semantic search
-- ============================================================

CREATE TABLE IF NOT EXISTS contact_embeddings (

    id              SERIAL      PRIMARY KEY,
    contact_id      INTEGER     NOT NULL REFERENCES contacts (id) ON DELETE CASCADE,
    source          TEXT        NOT NULL,
    source_ref_id   TEXT,
    content_text    TEXT        NOT NULL,
    embedding       vector(1536),
    content_hash    TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()

);

CREATE UNIQUE INDEX IF NOT EXISTS idx_embeddings_hash    ON contact_embeddings (content_hash);
CREATE INDEX        IF NOT EXISTS idx_embeddings_contact  ON contact_embeddings (contact_id);
CREATE INDEX        IF NOT EXISTS idx_embeddings_source   ON contact_embeddings (source);

CREATE INDEX IF NOT EXISTS idx_embeddings_vector
    ON contact_embeddings
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
