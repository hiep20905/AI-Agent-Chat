from typing import Literal

from pydantic import BaseModel, Field

MAX_SUB_QUESTIONS = 4

Intent = Literal[
    "value", "list", "ranking", "trend", "comparison", "breakdown",
    "range", "anomaly", "correlation", "diagnostic", "forecast",
]

# What the asker hears when a question is blocked; written here, not by the model, so a blocked question never reaches
# a model that could be talked into answering it.
BLOCKED_REPLIES = {
    "injection": (
        "Tôi chỉ trả lời câu hỏi về số liệu trên dashboard này, và không chia sẻ chỉ dẫn, cấu hình hay thông tin kỹ "
        "thuật nội bộ của hệ thống."
    ),
    "write": (
        "Tôi chỉ đọc dữ liệu, không thêm, sửa hay xoá dữ liệu. Bạn có thể hỏi tôi về số liệu hiện có trên dashboard "
        "này."
    ),
    "pii": "Tôi không cung cấp thông tin cá nhân của một người cụ thể.",
    "access": "Tôi chỉ trả lời trong phạm vi dữ liệu bạn được xem trên dashboard này.",
    "harmful": "Tôi không thể hỗ trợ yêu cầu này.",
}

# How each kind of question is logged: the ones the system declines are kept apart from the ones it answered.
TRIAGE_STATUSES = {
    "metadata": "resolved",
    "chitchat": "resolved",
    "out_of_domain": "declined",
    "unsupported": "declined",
    "unclear": "declined",
    "blocked": "blocked",
}


class SubQuestion(BaseModel):
    id: int
    text: str = Field(description="Câu hỏi tự đứng được; chỗ dùng kết quả câu con khác viết {{q<id>}}.")
    intent: Intent
    depends_on: list[int] = Field(default_factory=list)


class SetAsidePart(BaseModel):
    text: str
    triage: Literal["metadata", "out_of_domain", "unsupported"]


class QuestionTriage(BaseModel):
    triage: Literal["answerable", "metadata", "chitchat", "out_of_domain", "unsupported", "unclear", "blocked"]
    blocked_reason: Literal["injection", "write", "pii", "access", "harmful"] | None = None
    follow_up: Literal["new", "refine", "replace", "about_result"] = "new"
    execution: Literal["single", "parallel", "sequential"] | None = None
    sub_questions: list[SubQuestion] = Field(default_factory=list)
    set_aside: list[SetAsidePart] = Field(default_factory=list)
    reason: str = ""


def checked_triage(value: QuestionTriage) -> QuestionTriage:
    """The model's labels made consistent with docs/specs/taxonomy-cau-hoi.md §8; nothing it says is taken on trust.

    Only an answerable question keeps sub-questions and parts set aside, at most `MAX_SUB_QUESTIONS` of them with text.
    A dependency must point to an earlier kept sub-question, and `execution` follows from what is left. An answerable
    question the model gave no sub-question is answered as it was asked.
    """
    if value.triage != "answerable":
        blocked_reason = value.blocked_reason if value.triage == "blocked" else None
        return value.model_copy(update={
            "blocked_reason": blocked_reason, "execution": None, "sub_questions": [], "set_aside": [],
        })
    kept: list[SubQuestion] = []
    for sub_question in value.sub_questions:
        text = sub_question.text.strip()
        if text and len(kept) < MAX_SUB_QUESTIONS and all(item.id != sub_question.id for item in kept):
            earlier = {item.id for item in kept}
            depends_on = [item for item in dict.fromkeys(sub_question.depends_on) if item in earlier]
            kept.append(sub_question.model_copy(update={"text": text, "depends_on": depends_on}))
    if len(kept) <= 1:
        execution = "single"
    else:
        execution = "sequential" if any(item.depends_on for item in kept) else "parallel"
    set_aside = [part for part in value.set_aside if part.text.strip()]
    return value.model_copy(update={
        "blocked_reason": None, "execution": execution, "sub_questions": kept, "set_aside": set_aside,
    })


def blocked_reply(value: QuestionTriage) -> str:
    return BLOCKED_REPLIES.get(value.blocked_reason or "harmful", BLOCKED_REPLIES["harmful"])
