import asyncio
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import Protocol

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langgraph.graph import END, START, MessagesState, StateGraph


class ChatModel(Protocol):
    async def ainvoke(self, messages: list[BaseMessage]) -> BaseMessage: ...


class ChatTelemetry(Protocol):
    def track_generation(self) -> AbstractContextManager[None]: ...

    def record_usage(self, usage: Mapping[str, int] | None) -> None: ...


def build_chat_workflow(
    model: ChatModel,
    metrics: ChatTelemetry | None = None,
    provider_semaphore: asyncio.Semaphore | None = None,
):
    """Compile once per process; history is supplied by the persistence service."""

    async def respond(state: MessagesState) -> dict[str, list[AIMessage]]:
        with metrics.track_generation() if metrics else nullcontext():
            messages = [
                SystemMessage(
                    content="You are a helpful support assistant. "
                    "Be concise and honest. "
                    "Do not claim to perform actions or access systems "
                    "you cannot use."
                ),
                *state["messages"],
            ]
            if provider_semaphore is None:
                response = await model.ainvoke(messages)
            else:
                async with provider_semaphore:
                    response = await model.ainvoke(messages)
            if metrics:
                metrics.record_usage(getattr(response, "usage_metadata", None))
            if not isinstance(response.content, str) or not response.content.strip():
                raise ValueError("The chat provider returned no text")
            return {"messages": [AIMessage(content=response.content)]}

    graph = StateGraph(MessagesState)
    graph.add_node("respond", respond)
    graph.add_edge(START, "respond")
    graph.add_edge("respond", END)
    return graph.compile()
