"""etf_holding_raw_identifier

Keep the identifier a US issuer actually published when it is not an ISIN.

Three of the four adapters added alongside this migration — Invesco, First Trust, Defiance —
publish nine-character identifiers rather than ISINs, and those identifiers are not all
CUSIPs. Measured on the live files (2026-08-16): 77 of GRID's 128 rows carry a **CINS**
(a foreign issuer in the same numbering space, marked by a leading letter — `G29183103`
Eaton, `F86921107` Schneider), and 20 of QTUM's 89 carry a **SEDOL** in a column headed
"CUSIP". Neither can be converted to an ISIN locally: `US` + a CINS is a check-digit-valid
ISIN belonging to nothing.

Before these columns those rows reached `etf_holdings` with `constituent_isin = NULL` and
nothing else, so the company could never fold with the same company held directly or inside
another fund, whatever provider was asked later.

Two columns rather than one, because **OpenFIGI's mapping endpoint answers with a FIGI and
never with an ISIN** (measured 2026-08-16: `ID_CINS G29183103` returns 104 venue rows carrying
figi / compositeFIGI / shareClassFIGI / ticker / exchCode and no ISIN anywhere). So the
resolved value goes into its own `constituent_share_class_figi`, which is already one of the
keys `company_identity.company_groups` unions on, rather than being coerced into
`constituent_isin` as a fabricated identifier.

Note the idType is `ID_CINS`, not `ID_CUSIP`: asked as a CUSIP, `G29183103` returns **zero
rows** — resolving nothing, silently, which is why this was checked against the live API
before being written.

Both nullable and empty for every existing row, so the upgrade is a pure add and the downgrade
loses only what a re-import plus one CLI run reproduces.

Revision ID: r1a8c5d2e6f7
Revises: q0f7b4c1d5e6
Create Date: 2026-08-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'r1a8c5d2e6f7'
down_revision: Union[str, None] = 'q0f7b4c1d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'etf_holdings',
        sa.Column('constituent_identifier', sa.String(20), nullable=True),
    )
    op.add_column(
        'etf_holdings',
        sa.Column('constituent_share_class_figi', sa.String(12), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('etf_holdings', 'constituent_share_class_figi')
    op.drop_column('etf_holdings', 'constituent_identifier')
