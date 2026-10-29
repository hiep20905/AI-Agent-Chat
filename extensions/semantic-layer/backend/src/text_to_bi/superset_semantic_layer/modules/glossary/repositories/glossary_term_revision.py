from sqlalchemy.orm import Session

from ....common.base_repo import BaseRepository
from ..models import GlossaryTermRevision

# A reviewer decided these revisions; a draft is still waiting.
DECIDED = ("approved", "revoked")


class GlossaryTermRevisionRepository(BaseRepository[GlossaryTermRevision]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, GlossaryTermRevision)

    def current_for(self, term_id: int) -> GlossaryTermRevision | None:
        return self.query().filter_by(term_id=term_id, is_current=True).one_or_none()

    def lock_current(self, term_id: int) -> GlossaryTermRevision | None:
        """Re-read a term's current revision and hold its row lock until the transaction ends."""
        current = self.query().filter_by(term_id=term_id, is_current=True)
        return current.populate_existing().with_for_update().one_or_none()

    def for_term(self, term_id: int) -> list[GlossaryTermRevision]:
        return self.query().filter_by(term_id=term_id).order_by(GlossaryTermRevision.revision_number).all()

    def serving_for(self, term_id: int) -> GlossaryTermRevision | None:
        """The revision the AI Agent uses: the term's latest approved or revoked revision, while it is approved.

        A draft waiting for review does not replace it; approving the draft does, and a revocation ends it.
        """
        decided = (
            self.query()
            .filter(GlossaryTermRevision.term_id == term_id, GlossaryTermRevision.status.in_(DECIDED))
            .order_by(GlossaryTermRevision.revision_number.desc())
            .first()
        )
        return decided if decided is not None and decided.status == "approved" else None

    def serving(self) -> list[GlossaryTermRevision]:
        """Every term's serving revision, as `serving_for` chooses it."""
        decided = (
            self.query()
            .filter(GlossaryTermRevision.status.in_(DECIDED))
            .order_by(GlossaryTermRevision.term_id, GlossaryTermRevision.revision_number)
            .all()
        )
        latest = {revision.term_id: revision for revision in decided}
        return [revision for revision in latest.values() if revision.status == "approved"]
