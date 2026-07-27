"""OpenAI-compatible multipart transcription provider."""

import asyncio
import time
from email.utils import parsedate_to_datetime
from typing import Optional

import httpx

from voice.asr.base import ASRAudio, ASRError, ASRProvider, ASRResult

_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class OpenAICompatibleASRProvider(ASRProvider):
    """Call an OpenAI-compatible ``/audio/transcriptions`` endpoint."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout_sec: float = 90,
        max_retries: int = 1,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_sec = timeout_sec
        self.max_retries = max(0, max_retries)
        self._http_client = http_client

    def _configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), 10.0))
            except ValueError:
                try:
                    delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
                    return max(0.0, min(delay, 10.0))
                except (TypeError, ValueError):
                    pass
        return min(0.5 * (2**attempt), 5.0)

    @staticmethod
    def _error_for_status(response: httpx.Response) -> ASRError:
        status = response.status_code
        if status in (401, 403):
            category = "authentication"
        elif status == 413:
            category = "input_too_large"
        elif status in (400, 404, 405, 415, 422):
            category = "provider_contract"
        elif status == 429:
            category = "rate_limited"
        elif status >= 500:
            category = "provider_unavailable"
        else:
            category = "provider_error"
        return ASRError(
            category,
            f"语音转写服务返回错误（HTTP {status}）。",
            retryable=status in _RETRYABLE_STATUS,
            status_code=status,
        )

    async def transcribe(
        self,
        audio: ASRAudio,
        *,
        language: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> ASRResult:
        if not self._configured():
            raise ASRError("configuration", "语音转写服务尚未配置。")

        # OpenAI 与硅基流动的默认转写响应均为 JSON。只发送共同必需的
        # model/file 字段，避免部分兼容服务拒绝未声明的 response_format。
        data = {"model": self.model}
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt
        headers = {"Authorization": f"Bearer {self.api_key}"}
        files = {"file": (audio.filename, audio.data, audio.media_type)}
        url = f"{self.base_url}/audio/transcriptions"
        attempts = self.max_retries + 1

        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_sec))
        try:
            for attempt in range(attempts):
                try:
                    response = await client.post(url, data=data, files=files, headers=headers)
                except (httpx.TimeoutException, httpx.RequestError) as exc:
                    # 上传连接中断或读超时时，服务端可能已经完整收到并计费；
                    # 没有幂等键保证时不能自动重传同一段音频。
                    raise ASRError(
                        "network",
                        "语音转写连接中断，结果状态未知，请稍后手动重试。",
                        retryable=False,
                    ) from exc

                if response.status_code in _RETRYABLE_STATUS and attempt + 1 < attempts:
                    await asyncio.sleep(self._retry_delay(response, attempt))
                    continue
                if response.status_code >= 400:
                    raise self._error_for_status(response)
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ASRError("provider_contract", "语音转写服务返回了无效响应。") from exc
                text = payload.get("text") if isinstance(payload, dict) else None
                if not isinstance(text, str):
                    raise ASRError("provider_contract", "语音转写服务响应缺少文本。")
                return ASRResult(
                    text=text,
                    language=payload.get("language") if isinstance(payload.get("language"), str) else None,
                    request_id=response.headers.get("x-request-id"),
                )
        finally:
            if owns_client:
                await client.aclose()
        raise ASRError("provider_unavailable", "语音转写服务暂时不可用。", retryable=True)
