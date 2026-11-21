"""Typed FOMC retrieval response and controlled reader failures."""

from typing import Literal

from pydantic import BaseModel

NoMatchStatus = Literal["no_documents_in_scope", "corpus_not_ready", "no_lexical_match"]


class RetrievalError(ValueError):
    def __init__(self, code: str, status_code: int) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class Coverage(BaseModel):
    catalog_documents: int
    not_collected: int
    pending: int
    failed: int
    ready: int
