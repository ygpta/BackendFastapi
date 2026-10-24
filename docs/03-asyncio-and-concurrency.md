# Lesson 3: asyncio and Concurrent I/O

## Goal

Understand what `async` and `await` do, how an event loop coordinates waiting
tasks, and why blocking work can stall an async application. The current
preview endpoint has no I/O to await; examples in this lesson are standalone
Python snippets, not features already wired into the API.

## 1. Coroutines Cooperate at Await Points

Calling an `async def` function creates a coroutine. It does not run to
completion just because it was called. An event loop runs the coroutine, and
the coroutine can give control back when it awaits an operation that is itself
asynchronous:

```python
import asyncio


async def fetch_ticket(ticket_id: int) -> str:
    await asyncio.sleep(0.1)  # Stand-in for network I/O.
    return f"ticket-{ticket_id}"


async def main() -> None:
    first, second = await asyncio.gather(
        fetch_ticket(1),
        fetch_ticket(2),
    )
    print(first, second)


asyncio.run(main())
```

While one coroutine waits, the event loop may run another ready coroutine.
This is concurrency, not CPU parallelism. The example's two waits overlap; the
event loop is not executing Python bytecode on two CPU cores at once.

`await` only helps when the awaited operation yields control. Awaiting a
synchronous function does not make it asynchronous, and adding `async` to a
function does not make its work nonblocking.

## 2. FastAPI Endpoints and the Event Loop

The project's routes are declared with `async def`, but their current work is
small and has no network or database I/O. They could be ordinary `def`
functions today. FastAPI supports both forms:

- An `async def` route runs on the event loop. Use async-compatible libraries
  and `await` their I/O operations.
- A normal `def` route is run in a thread pool by FastAPI, which is useful for
  synchronous libraries that block while doing I/O.

Do not call blocking work directly from an `async def` route. A blocking HTTP
client, `time.sleep()`, or synchronous database call can occupy the event-loop
thread and delay unrelated requests. For network I/O, prefer an async client
such as `httpx.AsyncClient`. If a legacy blocking I/O library must be used,
`asyncio.to_thread` can move a call to a worker thread, but it still needs
timeouts and concurrency limits.

Heavy CPU work is different: threads do not generally make Python CPU-bound
work run in parallel. Use a process worker or a separate job system when that
work is substantial.

## 3. Timeouts and Cancellation

Waiting on an upstream service without a deadline can tie up resources for too
long. Python 3.13 supports `asyncio.timeout`:

```python
import asyncio


async def call_dependency() -> str:
    await asyncio.sleep(5)
    return "done"


async def bounded_call() -> str:
    async with asyncio.timeout(0.2):
        return await call_dependency()
```

If the deadline expires, `asyncio.timeout` cancels the work and raises
`TimeoutError` outside the context manager. Real HTTP clients should also have
their own connection and read timeouts configured. A timeout is a budget, not
a guarantee that a remote server stopped processing the request.

Cancellation is part of async control flow. Use `try/finally` to release local
resources, and generally allow `CancelledError` to propagate after cleanup.
Swallowing cancellation can leave shutdown or request cancellation stuck.

## 4. Concurrency Is Not Durability

`asyncio.gather` lets related operations wait concurrently within a process.
It does not persist work, survive a process crash, or provide exactly-once
side effects. FastAPI `BackgroundTasks` is also in-process best-effort work.

For the support-agent project, short request-bound I/O can be async. Long-running
or side-effecting agent runs belong in the later durable-worker milestone,
where a database, queue, retries, idempotency, and explicit run state address
different failure modes.

## 5. Try It

Run the standalone examples in this lesson with Python 3.13. Compare the
concurrent example with this sequential version:

```python
first = await fetch_ticket(1)
second = await fetch_ticket(2)
```

Both versions return the same values, but the sequential one waits for each
operation in turn. `asyncio.gather` is appropriate when operations are
independent and should share a time window; it is not automatically correct
when the second operation depends on the first.

The existing app tests confirm the HTTP contract, but do not test async
networking or event-loop behavior. No such dependency exists in the app yet.

## 6. Exercise

1. Add an async helper that fetches two independent values with `asyncio.gather`.
2. Add a timeout around one simulated slow operation and handle `TimeoutError`
   at the caller.
3. Replace the simulated wait with an async HTTP client only when adding a real
   dependency in the async-and-failures milestone.
4. Test the timeout and successful result without relying on long sleeps.

Be ready to explain why `async def` alone cannot prevent blocking, why
concurrency is not durability, and what should happen when cancellation is
received.

## References

- [Python asyncio tasks](https://docs.python.org/3/library/asyncio-task.html)
- [FastAPI concurrency and async](https://fastapi.tiangolo.com/async/)
- [HTTPX async support](https://www.python-httpx.org/async/)