from typing import AsyncIterator, Optional
from loguru import logger

from app.processors.base import BaseProcessor
from app.processors.claude_ai import ClaudeAIContext
from app.models.streaming import (
    StreamingEvent,
    ContentBlockStartEvent,
    ContentBlockStopEvent,
    MessageDeltaEvent,
    MessageStopEvent,
    MessageDeltaData,
)
from app.models.claude import ToolResultContent, ToolUseContent
from app.services.tool_call import tool_call_manager


class ToolCallEventProcessor(BaseProcessor):
    """Processor that handles tool use events in the streaming response."""

    @staticmethod
    def _is_server_web_search_tool(tool_name: Optional[str], context: ClaudeAIContext) -> bool:
        """
        Determine whether a tool_use block should be treated as server web search.

        Server web search should continue in the same stream and must not be paused
        like a client tool call.
        """
        if tool_name != "web_search":
            return False

        request = context.messages_api_request
        if not request or not request.tools:
            return False

        for tool in request.tools:
            if getattr(tool, "name", None) != "web_search":
                continue

            tool_type = getattr(tool, "type", None)
            if (
                isinstance(tool_type, str)
                and (tool_type == "web_search_v0" or tool_type.startswith("web_search_"))
            ):
                return True

        return False

    async def process(self, context: ClaudeAIContext) -> ClaudeAIContext:
        """
        Intercept tool use content blocks and inject MessageDelta/MessageStop events.

        Requires:
            - event_stream in context
            - cladue_session in context

        Produces:
            - Modified event_stream with injected events for tool calls
            - Pauses session when tool call is detected
        """
        if not context.event_stream:
            logger.warning(
                "Skipping ToolCallEventProcessor due to missing event_stream"
            )
            return context

        if not context.claude_session:
            logger.warning("Skipping ToolCallEventProcessor due to missing session")
            return context

        logger.debug("Setting up tool call event processing")

        original_stream = context.event_stream
        new_stream = self._process_tool_events(original_stream, context)
        context.event_stream = new_stream

        return context

    async def _process_tool_events(
        self,
        event_stream: AsyncIterator[StreamingEvent],
        context: ClaudeAIContext,
    ) -> AsyncIterator[StreamingEvent]:
        """
        Process events and inject MessageDelta/MessageStop when tool use is detected.
        """
        current_tool_use_id: Optional[str] = None
        current_tool_name: Optional[str] = None
        current_tool_is_server_web_search = False
        tool_use_detected = False
        content_block_index: Optional[int] = None
        tool_result_detected = False
        tool_result_block_index: Optional[int] = None

        async for event in event_stream:
            # Check for ContentBlockStartEvent with tool_use type
            if isinstance(event.root, ContentBlockStartEvent):
                if isinstance(event.root.content_block, ToolUseContent):
                    current_tool_use_id = event.root.content_block.id
                    current_tool_name = event.root.content_block.name
                    current_tool_is_server_web_search = self._is_server_web_search_tool(
                        current_tool_name, context
                    )
                    content_block_index = event.root.index
                    tool_use_detected = True
                    logger.debug(
                        f"Detected tool use start: {current_tool_use_id} "
                        f"(name: {event.root.content_block.name})"
                    )
                elif isinstance(event.root.content_block, ToolResultContent):
                    tool_result_block_index = event.root.index
                    # 默认严格输出标准 Anthropic 事件：
                    # Claude Web 私有 tool_result（knowledge 列表）仅内部消费，不透传给 API 客户端。
                    logger.debug(
                        f"Detected tool result: {event.root.content_block.tool_use_id}"
                    )
                    tool_result_detected = True

            # Yield the original event
            if tool_result_detected:
                logger.debug("Skipping tool result content block")
            else:
                yield event

            # Check for ContentBlockStopEvent for a tool use block
            if isinstance(event.root, ContentBlockStopEvent):
                if (
                    tool_result_block_index is not None
                    and event.root.index == tool_result_block_index
                ):
                    logger.debug("Skipped tool result block ended")
                    tool_result_detected = False
                    tool_result_block_index = None
                if (
                    tool_use_detected
                    and content_block_index is not None
                    and event.root.index == content_block_index
                ):
                    logger.debug(f"Tool use block ended: {current_tool_use_id}")

                    # Server web search continues in the same SSE stream.
                    if current_tool_is_server_web_search:
                        current_tool_use_id = None
                        current_tool_name = None
                        current_tool_is_server_web_search = False
                        tool_use_detected = False
                        content_block_index = None
                        continue

                    message_delta = MessageDeltaEvent(
                        type="message_delta",
                        delta=MessageDeltaData(stop_reason="tool_use"),
                        usage=None,
                    )
                    yield StreamingEvent(root=message_delta)

                    message_stop = MessageStopEvent(type="message_stop")
                    yield StreamingEvent(root=message_stop)

                    # Register the tool call
                    if current_tool_use_id and context.claude_session:
                        tool_call_manager.register_tool_call(
                            tool_use_id=current_tool_use_id,
                            session_id=context.claude_session.session_id,
                            message_id=context.collected_message.id
                            if context.collected_message
                            else None,
                        )

                        logger.info(
                            f"Registered tool call {current_tool_use_id} for session {context.claude_session.session_id}"
                        )

                    current_tool_use_id = None
                    current_tool_name = None
                    current_tool_is_server_web_search = False
                    tool_use_detected = False
                    content_block_index = None

                    break
