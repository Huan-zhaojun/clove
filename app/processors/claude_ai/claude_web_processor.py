import time
import base64
import random
import string
from typing import List
from loguru import logger

from app.processors.base import BaseProcessor
from app.processors.claude_ai import ClaudeAIContext
from app.services.session import session_manager
from app.models.internal import ClaudeWebRequest, Attachment
from app.models.claude import Tool
from app.core.exceptions import NoValidMessagesError
from app.core.config import settings
from app.utils.messages import process_messages

# Claude.ai Web 端的搜索 Tool（与官方 API 的 web_search_20250305 不同）
WEB_SEARCH_V0_TOOL = {"type": "web_search_v0", "name": "web_search"}
# 识别客户端发送的 Web Search Server Tool 类型前缀（web_search_20250305, web_search_20260209 等）
WEB_SEARCH_TOOL_PREFIX = "web_search_"


class ClaudeWebProcessor(BaseProcessor):
    """Claude AI processor that handles session management, request building, and sending to Claude AI."""

    @staticmethod
    def _process_web_search_tools(tools: list) -> tuple[bool, list]:
        """检测并替换 web search server tool 为 Claude.ai web 格式。

        客户端发送的 API 格式（如 web_search_20250305）需要替换为
        Claude.ai web 端格式（web_search_v0）才能在 completion 请求中生效。
        """
        has_web_search = False
        filtered_tools = []
        for tool in tools:
            tool_type = getattr(tool, "type", None)
            if (
                tool_type
                and isinstance(tool_type, str)
                and tool_type.startswith(WEB_SEARCH_TOOL_PREFIX)
            ):
                has_web_search = True
            else:
                filtered_tools.append(tool)

        if has_web_search:
            # 注入 Claude.ai web 格式的搜索工具
            web_search_tool = Tool(name="web_search", type="web_search_v0")
            filtered_tools.insert(0, web_search_tool)

        return has_web_search, filtered_tools

    async def process(self, context: ClaudeAIContext) -> ClaudeAIContext:
        """
        Claude AI processor that:
        1. Gets or creates a Claude session
        2. Builds ClaudeWebRequest from messages_api_request
        3. Sends the request to Claude.ai

        Requires:
            - messages_api_request in context

        Produces:
            - claude_session in context
            - claude_web_request in context
            - original_stream in context
        """
        if context.original_stream:
            logger.debug("Skipping ClaudeWebProcessor due to existing original_stream")
            return context

        if not context.messages_api_request:
            logger.warning(
                "Skipping ClaudeWebProcessor due to missing messages_api_request"
            )
            return context

        # Step 1: Get or create Claude session
        if not context.claude_session:
            session_id = context.metadata.get("session_id")
            if not session_id:
                session_id = f"session_{int(time.time() * 1000)}"
                context.metadata["session_id"] = session_id

            logger.debug(f"Creating new session: {session_id}")
            context.claude_session = await session_manager.get_or_create_session(
                session_id
            )

        # Step 2: Build ClaudeWebRequest
        if not context.claude_web_request:
            request = context.messages_api_request

            if not request.messages:
                raise NoValidMessagesError()

            merged_text, images = await process_messages(
                request.messages, request.system
            )
            if not merged_text:
                raise NoValidMessagesError()

            if settings.padtxt_length > 0:
                pad_tokens = settings.pad_tokens or (
                    string.ascii_letters + string.digits
                )
                pad_text = "".join(random.choices(pad_tokens, k=settings.padtxt_length))
                merged_text = pad_text + merged_text
                logger.debug(
                    f"Added {settings.padtxt_length} padding tokens to the beginning of the message"
                )

            image_file_ids: List[str] = []
            if images:
                for i, image_source in enumerate(images):
                    try:
                        # Convert base64 to bytes
                        image_data = base64.b64decode(image_source.data)

                        # Upload to Claude
                        file_id = await context.claude_session.upload_file(
                            file_data=image_data,
                            filename=f"image_{i}.png",  # Default filename
                            content_type=image_source.media_type,
                        )
                        image_file_ids.append(file_id)
                        logger.debug(f"Uploaded image {i}: {file_id}")
                    except Exception as e:
                        logger.error(f"Failed to upload image {i}: {e}")

            await context.claude_session._ensure_conversation_initialized()

            paprika_mode = (
                "extended"
                if (
                    request.thinking
                    and request.thinking.type in ("enabled", "adaptive")
                )
                else None
            )

            await context.claude_session.set_paprika_mode(paprika_mode)

            # 检测 Web Search Tool，替换为 Claude.ai web 格式，并设置对话级搜索开关
            request_tools = request.tools or []
            has_web_search, processed_tools = self._process_web_search_tools(
                request_tools
            )
            if has_web_search:
                await context.claude_session.set_web_search(True)

            web_request = ClaudeWebRequest(
                max_tokens_to_sample=request.max_tokens,
                attachments=[Attachment.from_text(merged_text)],
                files=image_file_ids,
                model=request.model,
                rendering_mode="messages",
                prompt=settings.custom_prompt or "",
                timezone="UTC",
                tools=processed_tools,
            )

            context.claude_web_request = web_request
            logger.debug(f"Built web request with {len(image_file_ids)} images")

        # Step 3: Send to Claude
        logger.debug(
            f"Sending request to Claude.ai for session {context.claude_session.session_id}"
        )

        request_dict = context.claude_web_request.model_dump(exclude_none=True)
        context.original_stream = await context.claude_session.send_message(
            request_dict
        )

        return context
