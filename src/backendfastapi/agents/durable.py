"""Learn: a bounded workflow passes state through retrieval, generation and tools.

Read this before worker.py. Tests inject fake models and an in-memory checkpointer;
production injects the provider client and PostgreSQL checkpointer.
"""

from collections.abc import Callable
from time import perf_counter
from typing import TypedDict

import tiktoken
from langchain_core.messages import HumanMessage, SystemMessage, convert_to_messages
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from backendfastapi.schemas.platform import GeneratedAnswer


class RunState(TypedDict, total=False):
    # Learn: nodes return partial state updates; checkpoints retain completed state.
    documents: list[dict]
    text: str
    usage: dict
    tool_result: dict
    structured: dict


def build_run_graph(
    model,
    payload: dict,
    search,
    tool,
    publish,
    checkpointer,
    ttft_observer: Callable[[float], None] | None = None,
):
    """Checkpoint each completed node; all tool arguments come from reviewed input."""

    async def retrieve_documents(state: RunState):
        return {
            "documents": await search(payload["message"]) if payload["use_rag"] else []
        }

    async def generate(state: RunState):
        documents = state.get("documents", [])
        context = "\n\n".join(
            f"[document:{item['id']}] {item['title']}\n{item['content']}"
            for item in documents
        )
        messages = [SystemMessage(content=payload["system_prompt"])]
        if context:
            # This label helps interpret sources; it is not a security sandbox.
            messages.append(
                SystemMessage(content="UNTRUSTED REFERENCE DATA:\n" + context)
            )
        messages.extend(convert_to_messages(payload.get("history", [])))
        messages.append(HumanMessage(content=payload["message"]))
        if payload.get("structured_output"):
            messages.append(
                SystemMessage(
                    content=(
                        "Return only a JSON object with exactly these fields: "
                        '{"answer":"non-empty string","citations":["document-id"]}. '
                        "Use only citation IDs from the supplied reference data. "
                        "If no references apply, return an empty citations array."
                    )
                )
            )
        try:
            encoding = tiktoken.encoding_for_model(payload["model"])
        except KeyError as exc:
            raise ValueError("unsupported_tokenizer") from exc
        token_count = sum(
            len(encoding.encode(message.content, disallowed_special=())) + 16
            for message in messages
        )
        if token_count > payload["max_input_tokens"]:
            raise ValueError("input_token_limit")
        # Learn: enforce the input bound before starting a potentially billable call.
        await publish("generation_started", {})
        combined = None
        pending = ""
        generation_started = perf_counter()
        first_token_seen = False
        async for chunk in model.astream(messages):
            combined = chunk if combined is None else combined + chunk
            if isinstance(chunk.content, str):
                if chunk.content and not first_token_seen:
                    first_token_seen = True
                    elapsed = perf_counter() - generation_started
                    if ttft_observer is not None:
                        ttft_observer(elapsed)
                    await publish(
                        "first_token", {"elapsed_ms": round(elapsed * 1000, 2)}
                    )
                pending += chunk.content
            if len(pending) >= 64:
                await publish("token", {"text": pending})
                pending = ""
        if pending:
            await publish("token", {"text": pending})
        if (
            combined is None
            or not isinstance(combined.content, str)
            or not combined.content.strip()
        ):
            raise ValueError("empty_provider_response")
        if payload.get("structured_output"):
            try:
                parsed = GeneratedAnswer.model_validate_json(combined.content)
            except (ValidationError, ValueError) as exc:
                raise ValueError("invalid_structured_output") from exc
            source_ids = {item["id"] for item in documents}
            if any(citation not in source_ids for citation in parsed.citations):
                raise ValueError("invalid_structured_citation")
            structured = parsed.model_dump()
            await publish("structured_response", structured)
            return {
                "text": parsed.answer,
                "structured": structured,
                "usage": combined.usage_metadata or {},
            }
        return {"text": combined.content, "usage": combined.usage_metadata or {}}

    async def execute_tool(state: RunState):
        # Tool arguments are reviewed payload data, not arbitrary model-generated code.
        return {
            "tool_result": await tool(payload["tool"]) if payload.get("tool") else {}
        }

    graph = StateGraph(RunState)
    graph.add_node("retrieve", retrieve_documents)
    graph.add_node("generate", generate)
    graph.add_node("tool", execute_tool)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "generate")
    graph.add_edge("generate", "tool")
    graph.add_edge("tool", END)
    # A checkpoint is not exactly-once execution: an interrupted node may run again.
    return graph.compile(checkpointer=checkpointer)
