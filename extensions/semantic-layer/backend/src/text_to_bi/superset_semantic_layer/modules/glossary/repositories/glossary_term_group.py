from sqlalchemy.orm import Session

from ....common.base_repo import BaseRepository
from ..models import GlossaryTermGroup


class GlossaryTermGroupRepository(BaseRepository[GlossaryTermGroup]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, GlossaryTermGroup)

    def by_name(self) -> list[GlossaryTermGroup]:
        return self.query().order_by(GlossaryTermGroup.name).all()

    def by_normalized_name(self, normalized_name: str) -> GlossaryTermGroup | None:
        return self.query().filter_by(normalized_name=normalized_name).one_or_none()
