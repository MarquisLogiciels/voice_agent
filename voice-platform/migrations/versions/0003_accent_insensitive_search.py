"""Accent-insensitive, stemmed (English + French) full-text search for knowledge chunks.

Revision ID: 0003
Revises: 0002
"""
from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    # unaccent() is STABLE; generated columns need IMMUTABLE. Pinning the dictionary makes the
    # wrapper safe to declare immutable (standard pattern).
    op.execute(
        "CREATE OR REPLACE FUNCTION vp_unaccent(text) RETURNS text "
        "LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS "
        "$$ SELECT public.unaccent('public.unaccent'::regdictionary, $1) $$"
    )
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_tsv")
    op.execute("ALTER TABLE knowledge_chunks DROP COLUMN tsv")
    op.execute(
        "ALTER TABLE knowledge_chunks ADD COLUMN tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', vp_unaccent(content)) || to_tsvector('english', vp_unaccent(content)) || to_tsvector('french', vp_unaccent(content))) STORED"
    )
    op.execute("CREATE INDEX ix_knowledge_chunks_tsv ON knowledge_chunks USING gin (tsv)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_tsv")
    op.execute("ALTER TABLE knowledge_chunks DROP COLUMN tsv")
    op.execute(
        "ALTER TABLE knowledge_chunks ADD COLUMN tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED"
    )
    op.execute("CREATE INDEX ix_knowledge_chunks_tsv ON knowledge_chunks USING gin (tsv)")
    op.execute("DROP FUNCTION IF EXISTS vp_unaccent(text)")
