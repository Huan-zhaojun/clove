import json
from typing import Any, AsyncIterator, Optional
from dataclasses import dataclass
from loguru import logger

from pydantic import ValidationError

from app.models.streaming import (
    StreamingEvent,
    UnknownEvent,
)


@dataclass
class SSEMessage:
    event: Optional[str] = None
    data: Optional[str] = None


class EventParser:
    """Parses SSE (Server-Sent Events) streams into StreamingEvent objects."""

    def __init__(self, skip_unknown_events: bool = True):
        self.skip_unknown_events = skip_unknown_events
        self.buffer = ""

    async def parse_stream(
        self, stream: AsyncIterator[str]
    ) -> AsyncIterator[StreamingEvent]:
        """
        Parse an SSE stream and yield StreamingEvent objects.

        Args:
            stream: AsyncIterator that yields string chunks from the SSE stream

        Yields:
            StreamingEvent objects parsed from the stream
        """
        async for chunk in stream:
            chunk = chunk.replace('\r\n', '\n') # Normalize line endings
            self.buffer += chunk

            async for event in self._process_buffer():
                logger.debug(f"Parsed event:\n{event.model_dump()}")
                yield event

        async for event in self.flush():
            yield event

    async def _process_buffer(self) -> AsyncIterator[StreamingEvent]:
        """Process the buffer and yield complete SSE messages as StreamingEvent objects."""
        while "\n\n" in self.buffer:
            message_end = self.buffer.index("\n\n")
            message_text = self.buffer[:message_end]
            self.buffer = self.buffer[message_end + 2 :]

            sse_msg = self._parse_sse_message(message_text)

            if sse_msg.data:
                event = self._create_streaming_event(sse_msg)
                if event:
                    yield event

    def _parse_sse_message(self, message_text: str) -> SSEMessage:
        """Parse a single SSE message from text."""
        sse_msg = SSEMessage()

        for line in message_text.split("\n"):
            if not line:
                continue

            if ":" not in line:
                field = line
                value = ""
            else:
                field, value = line.split(":", 1)
                if value.startswith(" "):
                    value = value[1:]

            if field == "event":
                sse_msg.event = value
            elif field == "data":
                if sse_msg.data is None:
                    sse_msg.data = value
                else:
                    sse_msg.data += "\n" + value

        return sse_msg

    def _create_streaming_event(self, sse_msg: SSEMessage) -> Optional[StreamingEvent]:
        """
        Create a StreamingEvent from an SSE message.

        Args:
            sse_msg: The parsed SSE message

        Returns:
            StreamingEvent object or None if parsing fails
        """
        try:
            data = json.loads(sse_msg.data)

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON data: {e}")
            logger.debug(f"Raw data: {sse_msg.data}")
            return None

        data = self._normalize_private_event(data)
        if data is None:
            return None

        try:
            streaming_event = StreamingEvent(root=data)
        except ValidationError:
            if self.skip_unknown_events:
                logger.debug(f"Skipping unknown event: {sse_msg.event}")
                return None
            logger.debug(
                f"Unknown/unmodeled streaming event '{sse_msg.event}', falling back to UnknownEvent."
            )
            logger.debug(f"Event data: {data}")
            streaming_event = StreamingEvent(
                root=UnknownEvent(type=sse_msg.event, data=data)
            )

        return streaming_event

    def _normalize_private_event(self, data: Any) -> Optional[dict[str, Any]]:
        """将 Claude Web 私有事件规范化为 Anthropic 兼容事件。"""
        if not isinstance(data, dict):
            return None

        if data.get("type") != "content_block_delta":
            return data

        delta = data.get("delta")
        if not isinstance(delta, dict):
            return data

        delta_type = delta.get("type")
        if delta_type != "citation_start_delta":
            return data

        citation = self._convert_private_citation(delta.get("citation"))
        if not citation:
            return None

        normalized = data.copy()
        normalized["delta"] = {"type": "citations_delta", "citation": citation}
        return normalized

    def _convert_private_citation(self, raw: Any) -> Optional[dict[str, Any]]:
        """
        将 Claude Web 私有 citation 结构转换为 Anthropic web_search_result_location。

        私有 payload（citation_start_delta）不包含 Anthropic 全字段，
        这里合成一个最小可用结构以保留来源链接。
        """
        if not isinstance(raw, dict):
            return None

        url = raw.get("url")
        if not isinstance(url, str) or not url:
            return None

        title = raw.get("title") if isinstance(raw.get("title"), str) else None
        encrypted_index = (
            raw.get("uuid")
            if isinstance(raw.get("uuid"), str) and raw.get("uuid")
            else url
        )
        cited_text = title or ""

        return {
            "type": "web_search_result_location",
            "cited_text": cited_text,
            "encrypted_index": encrypted_index,
            "title": title,
            "url": url,
        }

    async def flush(self) -> AsyncIterator[StreamingEvent]:
        """
        Flush any remaining data in the buffer.

        This should be called when the stream ends to process any incomplete messages.

        Yields:
            Any remaining StreamingEvent objects
        """
        if self.buffer.strip():
            logger.warning(f"Flushing incomplete buffer: {self.buffer[:100]}...")

            self.buffer += "\n\n"

            async for event in self._process_buffer():
                yield event
