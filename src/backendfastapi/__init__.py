def main() -> None:
    """Console entry point; containers use uvicorn directly for explicit limits."""
    import uvicorn

    uvicorn.run(
        "backendfastapi.main:app", host="127.0.0.1", port=8000, access_log=False
    )
