from functools import lru_cache

import instructor
from openai import AsyncOpenAI

from app.config import get_settings

REQUEST_TIMEOUT = 60.0
TRANSPORT_RETRIES = 2
SCHEMA_RETRIES = 2


@lru_cache
def get_instructor() -> instructor.Instructor:
    settings = get_settings()
    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        timeout=REQUEST_TIMEOUT,
        max_retries=TRANSPORT_RETRIES,
    )
    return instructor.from_openai(client)
