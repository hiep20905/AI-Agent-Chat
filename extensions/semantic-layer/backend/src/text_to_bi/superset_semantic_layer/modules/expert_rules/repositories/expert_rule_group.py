from sqlalchemy.orm import Session

from ....common.base_repo import BaseRepository
from ..models import ExpertRuleGroup


class ExpertRuleGroupRepository(BaseRepository[ExpertRuleGroup]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, ExpertRuleGroup)

    def by_name(self) -> list[ExpertRuleGroup]:
        return self.query().order_by(ExpertRuleGroup.name).all()

    def by_normalized_name(self, normalized_name: str) -> ExpertRuleGroup | None:
        return self.query().filter_by(normalized_name=normalized_name).one_or_none()
