import logging

import httpx

logger = logging.getLogger("songseek")


class SongSeekError(Exception):
    def __init__(self, message: str, code: str = "operation_failed", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def public_error(error: Exception) -> str:
    # 上游异常可能包含请求头或签名地址，只公开自己的中文错误。
    if isinstance(error, SongSeekError):
        return str(error)
    if isinstance(error, (httpx.TimeoutException, TimeoutError)):
        return "请求超时"
    if isinstance(error, httpx.HTTPStatusError):
        return f"上游接口返回 HTTP {error.response.status_code}"
    if isinstance(error, httpx.HTTPError):
        return "无法连接音源"
    return "处理失败，请检查音源响应或本地文件"


def log_failure(provider: str, error: Exception) -> str:
    message = public_error(error)
    logger.warning("%s：%s", provider, message)
    return message
