"""2단계 인증을 인증 앱(TOTP)에서 간편 비밀번호(숫자 6자리)로 변경

Revision ID: b7e1c4a9d2f3
Revises: 0635c2126000
Create Date: 2026-11-02 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e1c4a9d2f3'
down_revision: Union[str, Sequence[str], None] = '0635c2126000'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 기존 인증 앱 등록은 옮길 수 없어, 대표는 다음 접속 때 간편 비밀번호를 새로 등록한다
    with op.batch_alter_table('user_account', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pin_hash', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('pin_failed', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('pin_locked_until', sa.DateTime(timezone=True), nullable=True))
        batch_op.drop_column('totp_secret')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('user_account', schema=None) as batch_op:
        batch_op.add_column(sa.Column('totp_secret', sa.String(length=64), nullable=True))
        batch_op.drop_column('pin_locked_until')
        batch_op.drop_column('pin_failed')
        batch_op.drop_column('pin_hash')
