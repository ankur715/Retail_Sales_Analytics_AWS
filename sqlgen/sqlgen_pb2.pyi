from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable
from typing import ClassVar as _ClassVar, Optional as _Optional

DESCRIPTOR: _descriptor.FileDescriptor

class WriteSqlRequest(_message.Message):
    __slots__ = ("question", "views", "details", "previous_error", "previous_sql")
    QUESTION_FIELD_NUMBER: _ClassVar[int]
    VIEWS_FIELD_NUMBER: _ClassVar[int]
    DETAILS_FIELD_NUMBER: _ClassVar[int]
    PREVIOUS_ERROR_FIELD_NUMBER: _ClassVar[int]
    PREVIOUS_SQL_FIELD_NUMBER: _ClassVar[int]
    question: str
    views: _containers.RepeatedScalarFieldContainer[str]
    details: str
    previous_error: str
    previous_sql: str
    def __init__(self, question: _Optional[str] = ..., views: _Optional[_Iterable[str]] = ..., details: _Optional[str] = ..., previous_error: _Optional[str] = ..., previous_sql: _Optional[str] = ...) -> None: ...

class SqlPlan(_message.Message):
    __slots__ = ("sql", "explanation", "unanswerable_reason", "llm_calls", "input_tokens", "output_tokens", "cost_usd")
    SQL_FIELD_NUMBER: _ClassVar[int]
    EXPLANATION_FIELD_NUMBER: _ClassVar[int]
    UNANSWERABLE_REASON_FIELD_NUMBER: _ClassVar[int]
    LLM_CALLS_FIELD_NUMBER: _ClassVar[int]
    INPUT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    COST_USD_FIELD_NUMBER: _ClassVar[int]
    sql: str
    explanation: str
    unanswerable_reason: str
    llm_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    def __init__(self, sql: _Optional[str] = ..., explanation: _Optional[str] = ..., unanswerable_reason: _Optional[str] = ..., llm_calls: _Optional[int] = ..., input_tokens: _Optional[int] = ..., output_tokens: _Optional[int] = ..., cost_usd: _Optional[float] = ...) -> None: ...
