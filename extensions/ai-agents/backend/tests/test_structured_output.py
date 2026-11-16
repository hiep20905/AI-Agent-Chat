from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from text_to_bi.superset_ai_agent.core.exceptions import MalformedModelOutputError, ModelLimitError
from text_to_bi.superset_ai_agent.core.structured_output import structured_answer


class Answer(BaseModel):
    value: int


class ProviderRefusal(Exception):
    status_code = 402


class ScriptedModel:
    """Answers each call with the next item: an exception is raised, anything else returned."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), 0

    def with_structured_output(self, schema):
        def invoke(messages):
            self.calls += 1
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        return SimpleNamespace(invoke=invoke)


def test_a_malformed_answer_is_asked_for_once_more():
    model = ScriptedModel(ValueError("Invalid JSON"), Answer(value=3))

    assert structured_answer(model, Answer, [("human", "?")]) == Answer(value=3) and model.calls == 2


def test_two_malformed_answers_raise_the_second_error():
    model = ScriptedModel(ValueError("Invalid JSON"), ValueError("missing field"))

    with pytest.raises(ValueError, match="missing field"):
        structured_answer(model, Answer, [("human", "?")])


def test_no_answer_twice_is_reported_as_malformed_output():
    with pytest.raises(MalformedModelOutputError):
        structured_answer(ScriptedModel(None, None), Answer, [("human", "?")])


class BadKey(Exception):
    status_code = 401


def test_running_out_of_credit_is_told_as_the_providers_limit_and_not_asked_again():
    model = ScriptedModel(ProviderRefusal("out of credit"), Answer(value=3))

    with pytest.raises(ModelLimitError, match="hạn mức"):
        structured_answer(model, Answer, [("human", "?")])
    assert model.calls == 1


def test_another_refusal_from_the_provider_is_raised_as_it_is():
    model = ScriptedModel(BadKey("invalid key"), Answer(value=3))

    with pytest.raises(BadKey):
        structured_answer(model, Answer, [("human", "?")])
    assert model.calls == 1
