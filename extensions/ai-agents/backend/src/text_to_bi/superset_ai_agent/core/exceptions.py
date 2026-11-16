from ..common.exceptions import NotAuthorizedError, ServiceUnavailableError


class AdminRequiredError(NotAuthorizedError):
    message = "Admin access required."


class CsrfTokenError(NotAuthorizedError):
    message = "CSRF token không hợp lệ."


class ModelLimitError(ServiceUnavailableError):
    message = (
        "Dịch vụ mô hình AI đang hết hạn mức hoặc quá tải nên chưa trả lời được. Hãy thử lại sau ít phút; nếu vẫn lỗi, "
        "báo quản trị viên kiểm tra hạn mức của khoá API."
    )


class MalformedModelOutputError(ServiceUnavailableError):
    message = "Mô hình AI trả lời sai định dạng hai lần liền; hãy thử hỏi lại."


class ChatModelNotConfiguredError(ServiceUnavailableError):
    message = (
        "OPENROUTER_API_KEY chưa được cấu hình. Đặt biến này trong file .env ở thư mục gốc repo "
        "rồi khởi động lại dịch vụ superset."
    )
