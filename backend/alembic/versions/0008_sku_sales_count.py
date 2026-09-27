"""add SKU sales count and maintain SPU aggregate

Revision ID: 0008_sku_sales_count
Revises: 0007_context_memory_v2
Create Date: 2026-07-19
"""

import sqlalchemy as sa

from alembic import op

revision = "0008_sku_sales_count"
down_revision = "0007_context_memory_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sku",
        sa.Column(
            "sales_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_sku_sales_count_non_negative",
        "sku",
        "sales_count >= 0",
    )

    # Preserve existing SPU totals by distributing them deterministically
    # across their SKUs before SKU becomes the source of truth.
    op.execute(
        """
        WITH ranked_skus AS (
            SELECT sku.id AS sku_id,
                   spu.sales_count AS spu_sales_count,
                   count(*) OVER (PARTITION BY sku.spu_id) AS sku_count,
                   row_number() OVER (
                       PARTITION BY sku.spu_id
                       ORDER BY sku.id
                   ) AS sku_position
            FROM sku
            JOIN spu ON spu.id = sku.spu_id
        )
        UPDATE sku
        SET sales_count = (
            ranked_skus.spu_sales_count / ranked_skus.sku_count
            + CASE
                WHEN ranked_skus.sku_position <= (
                    ranked_skus.spu_sales_count % ranked_skus.sku_count
                ) THEN 1
                ELSE 0
              END
        )::integer
        FROM ranked_skus
        WHERE sku.id = ranked_skus.sku_id
        """
    )
    op.execute(
        """
        UPDATE spu
        SET sales_count = COALESCE(
            (
                SELECT sum(sku.sales_count)::integer
                FROM sku
                WHERE sku.spu_id = spu.id
            ),
            0
        )
        """
    )

    op.execute(
        """
        CREATE FUNCTION sync_spu_sales_count_from_sku()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                UPDATE spu
                SET sales_count = sales_count + NEW.sales_count
                WHERE id = NEW.spu_id;
            ELSIF TG_OP = 'DELETE' THEN
                UPDATE spu
                SET sales_count = sales_count - OLD.sales_count
                WHERE id = OLD.spu_id;
            ELSIF NEW.spu_id = OLD.spu_id THEN
                UPDATE spu
                SET sales_count = sales_count - OLD.sales_count + NEW.sales_count
                WHERE id = NEW.spu_id;
            ELSE
                UPDATE spu
                SET sales_count = sales_count - OLD.sales_count
                WHERE id = OLD.spu_id;
                UPDATE spu
                SET sales_count = sales_count + NEW.sales_count
                WHERE id = NEW.spu_id;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_sku_sync_spu_sales_count
        AFTER INSERT OR UPDATE OF sales_count, spu_id OR DELETE ON sku
        FOR EACH ROW
        EXECUTE FUNCTION sync_spu_sales_count_from_sku()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_sku_sync_spu_sales_count ON sku")
    op.execute("DROP FUNCTION sync_spu_sales_count_from_sku()")
    op.drop_constraint("ck_sku_sales_count_non_negative", "sku", type_="check")
    op.drop_column("sku", "sales_count")
