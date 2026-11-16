from ...common.exceptions import NotAuthorizedError, NotFoundError, ValidationError


class QueryPlanError(ValidationError):
    """The plan cannot be run as asked; the message is safe to show to the user."""


class ChatQuestionError(ValidationError):
    message = "Câu hỏi phải là chuỗi không rỗng, tối đa 2000 ký tự."


class RetriedQuestionError(ValidationError):
    message = "Câu hỏi cần trả lời lại không hợp lệ."


class RetriedQuestionNotFoundError(NotFoundError):
    message = "Không tìm thấy câu hỏi cần trả lời lại."


class DashboardRequiredError(ValidationError):
    message = "Chọn dashboard trước khi hỏi AI Agent."


class ChatSessionIdError(ValidationError):
    message = "Cuộc trò chuyện không hợp lệ."


class ClassifyAdminRequiredError(NotAuthorizedError):
    message = "Chỉ quản trị viên được chạy riêng bước phân loại câu hỏi."


class ClassifyRequestError(ValidationError):
    message = "Cần `question` (chuỗi không rỗng) và `history` là danh sách `{question, answer}`."
