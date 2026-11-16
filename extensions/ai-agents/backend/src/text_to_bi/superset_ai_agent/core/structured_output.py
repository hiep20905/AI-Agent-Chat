import logging
from typing import Any

from pydantic import BaseModel

from .exceptions import MalformedModelOutputError, ModelLimitError

logger = logging.getLogger(__name__)

# A step whose answer does not fit its schema is asked this many times more (`bad_model_output` in
# docs/specs/xu-ly-loi-truy-van.md); a second malformed answer is reported as the step's error.
MALFORMED_RETRIES = 1
# The model provider refused for its own limits: out of credit (402) or too many requests (429).
MODEL_LIMIT_STATUSES = (402, 429)


def structured_answer(model: Any, schema: type[BaseModel], messages: list[tuple[str, str]]) -> Any:
    """The model's answer parsed into `schema`.

    An answer that does not fit it (broken JSON, a missing or mistyped field, no answer at all) is asked for again. A
    refusal from the model provider carries a status code (out of credit, too many requests, a bad key) and is raised
    at once, since asking again would only be refused again; one for the provider's limits becomes ModelLimitError,
    which the routes answer with a message the asker can act on.
    """
    for attempt in range(MALFORMED_RETRIES + 1):
        last = attempt == MALFORMED_RETRIES
        try:
            answer = model.with_structured_output(schema).invoke(messages)
        except Exception as error:
            status = getattr(error, "status_code", None)
            if status in MODEL_LIMIT_STATUSES:
                raise ModelLimitError() from error
            if status is not None or last:
                raise
            logger.warning("Malformed %s answer, asking again: %s", schema.__name__, error)
            continue
        if answer is not None:
            return answer
        if last:
            raise MalformedModelOutputError()
        logger.warning("Empty %s answer, asking again", schema.__name__)
    raise MalformedModelOutputError()
