from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader

from backendfastapi.core.config import Settings, get_settings

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    supplied_key: Annotated[str | None, Depends(api_key_header)],
) -> str:
    """A shared key represents one trusted service, not an individual user."""
    if settings.api_key is None:
        return "development"
    expected_key = settings.api_key.get_secret_value()
    if supplied_key is None or not compare_digest(
        supplied_key.encode(), expected_key.encode()
    ):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return "service"
