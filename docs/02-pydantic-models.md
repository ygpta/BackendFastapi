# Lesson 2: Pydantic Models and Validation

For a short Hinglish recap with examples of `Field`, model configuration,
validators, inheritance, nested models, and serialization, see the
[Pydantic revision sheet](pydantic-revision-hinglish.html).

## Goal

Learn how Pydantic turns untrusted input into typed Python data, how model
configuration and field constraints define a contract, and how FastAPI uses
that contract for both requests and responses. The exercise's optional
`customer_reference` field is implemented in the current application.

Keep the [ticket schemas](../src/backendfastapi/schemas/tickets.py),
[ticket endpoint](../src/backendfastapi/api/v1/endpoints/tickets.py), and
[tests](../tests/test_main.py) open while reading.

## 1. An Annotation Is Not Validation

In ordinary Python, this annotation helps tools understand the intended type,
but Python does not reject a bad value at runtime:

```python
def preview(subject: str) -> str:
    return subject

preview(42)  # Runs at runtime, despite the annotation.
```

A Pydantic `BaseModel` uses type annotations as part of runtime parsing and
validation:

```python
from pydantic import BaseModel, ConfigDict, Field


class TicketInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    subject: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=10, max_length=4000)
    customer_reference: str | None = Field(default=None, min_length=1, max_length=64)
```

This is the input model in the project. `subject` and `message` are required;
`customer_reference` is optional because it has a default. `Field` adds
constraints, while `ConfigDict` configures behavior for the whole model:

- `str_strip_whitespace=True` trims string values before validation.
- `extra="forbid"` rejects keys that are not declared fields.
- `min_length` and `max_length` constrain the resulting string values.

These limits do not cap the HTTP body size, prove a caller is authorized, or
make the content safe to send to a model. Those are separate responsibilities.

## 2. Parsing and Validation

FastAPI reads the request body as JSON and asks Pydantic to create a
`TicketInput`. The endpoint receives that model instance only if validation
succeeds. Invalid client input produces HTTP 422 before the endpoint runs.

The existing tests cover whitespace normalization, missing fields, unknown
fields, and values outside the length bounds. For example, a subject made only
of spaces becomes an empty string and fails its minimum length constraint.

Pydantic generally performs useful conversions in its default, non-strict
mode. That can be convenient, but it means validation is not always identical
to "accept only values whose JSON type exactly matches the annotation." Choose
strictness deliberately for each contract; do not assume type annotations
alone express every business rule.

For standalone Python data, Pydantic exposes the same model validation:

```python
payload = {
    "subject": "  Login issue  ",
    "message": "  I cannot log in today.  ",
}
ticket = TicketInput.model_validate(payload)

assert ticket.subject == "Login issue"
assert ticket.message == "I cannot log in today."
```

In a FastAPI route, declare the model parameter instead of manually calling
`model_validate` on the already-parsed request body. FastAPI handles the
request integration and returns a useful 422 response for invalid input.

## 3. Request and Response Models

The app has separate models for input and output:

```python
from typing import Literal

from pydantic import BaseModel


class TicketPreview(BaseModel):
    subject: str
    message: str
    customer_reference: str | None
    status: Literal["validated"]
```

The route declares the input model in its parameter and the output model with
`response_model=TicketPreview`. The endpoint delegates to the ticket service;
FastAPI validates and serializes its returned value against the output
contract. Keeping the two models separate lets the public response differ
from the accepted input as the service grows.

Pydantic models also support explicit serialization and JSON Schema generation:

```python
ticket.model_dump()        # Python dictionary
ticket.model_json_schema() # JSON Schema describing the model
```

FastAPI uses model schemas when generating OpenAPI, which powers the interactive
`/docs` page. Response validation is not a substitute for tests: tests verify
the behavior callers actually depend on.

## 4. Optional Fields and Defaults

An optional value and an optional field are related but distinct ideas:

```python
customer_reference: str | None = None
```

`str | None` allows the value to be either a string or `None`. The default
`None` also makes it optional to provide in the input. Without a default, the
field is still required even if its value may be `None`.

For a field that must be present but may explicitly contain `None`, use a
nullable annotation without a default. Confirm the behavior with a validation
test rather than relying on the word "optional" in conversation.

## 5. Try It in This Project

Run the focused tests:

```bash
uv run pytest tests/test_main.py -q
```

Then try valid and invalid request bodies at
`http://127.0.0.1:8000/docs`. Inspect the 422 response's `detail` entries:
each one identifies a location, a message, and an error type. Treat those
details as diagnostics, not as a stable application-specific error format
unless you explicitly design and test one.

## 6. Review the Exercise

The project now includes an optional `customer_reference` in both models. It
accepts 1 to 64 characters when provided, uses the model's whitespace trimming,
and returns `null` when omitted. The tests cover omission, a trimmed value,
whitespace-only input, and an overlong value.

Trace the value from request parsing through `TicketInput`, the endpoint, the
ticket service, and `TicketPreview`. Then explain why Pydantic validation does
not establish identity, authorization, or safe tool access.

Next: [asyncio and concurrent I/O](03-asyncio-and-concurrency.md), then
application structure and dependency injection.

## References

- [Pydantic models](https://docs.pydantic.dev/latest/concepts/models/)
- [Pydantic fields](https://docs.pydantic.dev/latest/concepts/fields/)
- [FastAPI request body models](https://fastapi.tiangolo.com/tutorial/body/)