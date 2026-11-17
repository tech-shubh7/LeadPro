"""The one interface every pipeline step uses to call a model. Providers (OpenAI, Gemini, manual) are
interchangeable behind it, so services depend on this protocol, never on a vendor class."""

from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMProvider(Protocol):
    @property
    def extract_model(self) -> str:
        """Cheap model for high-volume extraction and triage."""
        ...

    @property
    def judge_model(self) -> str:
        """Strong model for planning and judging."""
        ...

    async def parse(
        self,
        model: str,
        instructions: str,
        data: str,
        schema: type[T],
        effort: str = "low",
        max_output_tokens: int = 16000,
    ) -> T:
        """Structured output validated against `schema`; raises LLMRefusalError when none is usable."""
        ...
