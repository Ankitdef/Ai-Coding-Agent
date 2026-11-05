import asyncio
import logging
import random
import uuid
from typing import Any, AsyncGenerator
from openai import APIConnectionError, APIError, AsyncOpenAI, RateLimitError

from client.response import (
    StreamEventType,
    StreamEvent,
    TextDelta,
    TokenUsage,
    ToolCall,
    ToolCallDelta,
    parse_tool_call_arguments,
)
from config.config import Config

logger = logging.getLogger(__name__)


class LLMClient:
    _RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}

    def __init__(self, config: Config) -> None:
        self._client: AsyncOpenAI | None = None
        self._max_retries: int = 5
        self.config = config

    def get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url,  # "https://openrouter.ai/api/v1"
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    def _build_tools(self, tools: list[dict[str, Any]]):
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get(
                        "parameters",
                        {
                            "type": "object",
                            "properties": {},
                        },
                    ),
                },
            }
            for tool in tools
        ]

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = True,
    ) -> AsyncGenerator[StreamEvent, None]:
        client = self.get_client()

        kwargs = {
            "model": self.config.model_name,
            "messages": messages, # system prompt + history
            "stream": stream,
            "temperature": self.config.temperature,
        }

        if self.config.max_tokens:
            kwargs["max_tokens"] = self.config.max_tokens

        if tools:
            kwargs["tools"] = self._build_tools(tools) # menu in openai formte
            kwargs["tool_choice"] = "auto"

        for attempt in range(self._max_retries + 1):
            emitted = False
            try:
                if stream:
                    async for event in self._stream_response(client, kwargs):
                        emitted = True
                        yield event
                else:
                    event = await self._non_stream_response(client, kwargs)
                    yield event
                return
            except APIError as e:
                message, retryable = self._classify_error(e)

                # Replaying the request after events already reached the caller
                # would duplicate text and tool calls, so a failure part-way
                # through a message is terminal.
                if emitted or not retryable or attempt == self._max_retries:
                    yield StreamEvent(
                        type=StreamEventType.ERROR,
                        error=message,
                    )
                    return

                await asyncio.sleep(self._retry_delay(attempt))

    def _classify_error(self, error: APIError) -> tuple[str, bool]:
        if isinstance(error, RateLimitError):
            return f"Rate limit exceeded: {error}", True

        if isinstance(error, APIConnectionError):
            return f"Connection error: {error}", True

        status = getattr(error, "status_code", None)

        # A bare APIError has no status code: the SDK raises one when the
        # server sends an error frame inside the SSE stream rather than an HTTP
        # error, which is how OpenRouter reports an overloaded upstream
        # provider. Those are transient, so they get retried like a 503.
        retryable = status is None or status in self._RETRYABLE_STATUS
        return f"API error: {error}", retryable

    def _retry_delay(self, attempt: int) -> float:
        return min(2**attempt, 8) + random.uniform(0, 1)

    async def _stream_response(
        self,
        client: AsyncOpenAI,
        kwargs: dict[str, Any],
    ) -> AsyncGenerator[StreamEvent, None]:
        response = await client.chat.completions.create(**kwargs)

        finish_reason: str | None = None
        usage: TokenUsage | None = None
        tool_calls: dict[int, dict[str, Any]] = {}

        async for chunk in response:
            if hasattr(chunk, "usage") and chunk.usage:
                usage = TokenUsage(
                    prompt_tokens=chunk.usage.prompt_tokens,
                    completion_tokens=chunk.usage.completion_tokens,
                    total_tokens=chunk.usage.total_tokens,
                    cached_tokens=chunk.usage.prompt_tokens_details.cached_tokens,
                )

            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            if choice.finish_reason:
                finish_reason = choice.finish_reason

            if delta.content:
                yield StreamEvent(
                    type=StreamEventType.TEXT_DELTA,
                    text_delta=TextDelta(delta.content),
                )

            if delta.tool_calls:
                for tool_call_delta in delta.tool_calls:
                    idx = tool_call_delta.index

                    if idx not in tool_calls:
                        tool_calls[idx] = {
                            "id": tool_call_delta.id or "",
                            "name": "",
                            "arguments": "",
                        }

                    if tool_call_delta.id and not tool_calls[idx]["id"]:
                        tool_calls[idx]["id"] = tool_call_delta.id

                    if tool_call_delta.function:
                        if tool_call_delta.function.name:
                            tool_calls[idx]["name"] = tool_call_delta.function.name
                            yield StreamEvent(
                                type=StreamEventType.TOOL_CALL_START,
                                tool_call_delta=ToolCallDelta(
                                    call_id=tool_calls[idx]["id"],
                                    name=tool_call_delta.function.name,
                                ),
                            )

                        if tool_call_delta.function.arguments:
                            tool_calls[idx][
                                "arguments"
                            ] += tool_call_delta.function.arguments

                            yield StreamEvent(
                                type=StreamEventType.TOOL_CALL_DELTA,
                                tool_call_delta=ToolCallDelta(
                                    call_id=tool_calls[idx]["id"],
                                    name=tool_calls[idx]["name"],
                                    arguments_delta=tool_call_delta.function.arguments,
                                ),
                            )

        for idx, tc in tool_calls.items():
            # Some providers stream a tool_call frame with no function name at
            # all. Forwarding it makes the agent invoke "" , feed the model an
            # "Unknown tool" error and try again - a loop the model cannot win.
            if not tc["name"]:
                logger.warning(
                    f"Discarding malformed tool call at index {idx}: no function name"
                )
                continue

            yield StreamEvent(
                type=StreamEventType.TOOL_CALL_COMPLETE,
                tool_call=ToolCall(
                    call_id=tc["id"] or f"call_{uuid.uuid4().hex[:24]}",
                    name=tc["name"],
                    arguments=parse_tool_call_arguments(tc["arguments"]),
                ),
            )

        yield StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            finish_reason=finish_reason,
            usage=usage,
        )

    async def _non_stream_response(
        self,
        client: AsyncOpenAI,
        kwargs: dict[str, Any],
    ) -> StreamEvent:
        response = await client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        message = choice.message

        text_delta = None
        if message.content:
            text_delta = TextDelta(content=message.content)

        tool_calls: list[ToolCall] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append(
                    ToolCall(
                        call_id=tc.id,
                        name=tc.function.name,
                        arguments=parse_tool_call_arguments(tc.function.arguments),
                    )
                )

        usage = None
        if response.usage:
            usage = TokenUsage(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
                cached_tokens=response.usage.prompt_tokens_details.cached_tokens,
            )

        return StreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            text_delta=text_delta,
            finish_reason=choice.finish_reason,
            usage=usage,
        )
