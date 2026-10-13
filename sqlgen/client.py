"""How the chatbot gets SQL written: over gRPC to the sqlgen service when SQLGEN_ADDR is
set (the compose stack), otherwise in-process (tests, a laptop uvicorn). Same signature
and result as chatbot.llm.write_sql either way."""
import threading

import grpc

from chatbot import llm, telemetry
from chatbot.llm_types import LLMUnavailable, SqlPlan
from etl import config
from sqlgen import sqlgen_pb2, sqlgen_pb2_grpc

# Status codes the server uses for LLM failures (sqlgen.proto), plus the transport's own:
# an unreachable service is UNAVAILABLE and a slow one DEADLINE_EXCEEDED -- both "busy",
# a retryable 503 for the user.
KINDS = {grpc.StatusCode.UNAVAILABLE: "busy", grpc.StatusCode.DEADLINE_EXCEEDED: "busy",
         grpc.StatusCode.RESOURCE_EXHAUSTED: "quota", grpc.StatusCode.FAILED_PRECONDITION: "config",
         grpc.StatusCode.PERMISSION_DENIED: "declined"}
_stub = None
_lock = threading.Lock()


def stub() -> sqlgen_pb2_grpc.SqlGeneratorStub:
    """One channel per process (gRPC multiplexes calls over it), created on first use."""
    global _stub
    with _lock:
        if _stub is None:
            _stub = sqlgen_pb2_grpc.SqlGeneratorStub(grpc.insecure_channel(config.SQLGEN_ADDR))
        return _stub


def reset() -> None:
    global _stub
    with _lock:
        _stub = None


def write_sql(question: str, views: list[str], details: str, previous_error: str | None = None,
              previous_sql: str | None = None) -> SqlPlan:
    if not config.SQLGEN_ADDR:
        return llm.write_sql(question, views, details, previous_error=previous_error, previous_sql=previous_sql)
    request = sqlgen_pb2.WriteSqlRequest(question=question, views=views, details=details,
                                         previous_error=previous_error or "", previous_sql=previous_sql or "")
    with telemetry.step("rpc.sqlgen.WriteSql", **{"rpc.system": "grpc", "chat.repair": bool(previous_error)}):
        try:
            reply = stub().WriteSql(request, timeout=config.SQLGEN_TIMEOUT_S)
        except grpc.RpcError as e:
            kind = KINDS.get(e.code())
            if kind is None:   # INVALID_ARGUMENT, INTERNAL, ...: a bug, not a busy model -- let it be a 500
                raise RuntimeError(f"sqlgen {e.code().name}: {e.details()}") from None
            raise LLMUnavailable(f"sqlgen {e.code().name}: {e.details()}", kind=kind) from None
    return SqlPlan(sql=reply.sql, explanation=reply.explanation, unanswerable_reason=reply.unanswerable_reason)
