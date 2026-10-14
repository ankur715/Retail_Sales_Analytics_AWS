"""How the chatbot gets SQL written: over gRPC to the sqlgen service when SQLGEN_ADDR is
set (the compose stack), otherwise in-process (tests, a laptop uvicorn). Same signature
and result as chatbot.llm.write_sql either way."""
import json
import threading

import grpc

from chatbot import llm, telemetry, usage
from chatbot.llm_types import LLMUnavailable, SqlPlan
from etl import config
from sqlgen import sqlgen_pb2, sqlgen_pb2_grpc

# Status codes the server uses for LLM failures (sqlgen.proto), plus the transport's own:
# an unreachable service is UNAVAILABLE and a slow one DEADLINE_EXCEEDED -- both "busy",
# a retryable 503 for the user.
KINDS = {grpc.StatusCode.UNAVAILABLE: "busy", grpc.StatusCode.DEADLINE_EXCEEDED: "busy",
         grpc.StatusCode.RESOURCE_EXHAUSTED: "quota", grpc.StatusCode.FAILED_PRECONDITION: "config",
         grpc.StatusCode.PERMISSION_DENIED: "declined"}
# Retry UNAVAILABLE up to 3 attempts (gRPC service config): a pod that just died, or a
# transient busy model, gets another try -- through the Service, so on a live pod -- inside the
# same deadline. In Kubernetes the address is the sqlgen ClusterIP Service: kube-proxy routes
# around dead pods within seconds. (Client-side round_robin over a headless Service balanced
# better per call, but failed over only as fast as DNS caches expire; see deploy/k8s/sqlgen.yaml.)
SERVICE_CONFIG = json.dumps({
    "methodConfig": [{"name": [{"service": "retail.sqlgen.v1.SqlGenerator"}],
                      "retryPolicy": {"maxAttempts": 3, "initialBackoff": "0.1s", "maxBackoff": "1s",
                                      "backoffMultiplier": 2, "retryableStatusCodes": ["UNAVAILABLE"]}}],
})
# Reconnect fast after the connection drops (its pod died, or the server aged it out):
#  - min_reconnect_backoff is also the connect TIMEOUT, 20 s by default. Right after a pod dies,
#    a connect can go to its vanished IP and hang silently for all 20 s, failing every call
#    meanwhile -- the chaos test measured exactly that. 1 s is plenty inside a cluster.
#  - the backoff between attempts would otherwise grow to 2 min.
CHANNEL_OPTIONS = [("grpc.service_config", SERVICE_CONFIG), ("grpc.enable_retries", 1),
                   ("grpc.min_reconnect_backoff_ms", 1000),
                   ("grpc.initial_reconnect_backoff_ms", 100), ("grpc.max_reconnect_backoff_ms", 1000)]
_stub = None
_lock = threading.Lock()


def stub() -> sqlgen_pb2_grpc.SqlGeneratorStub:
    """One channel per process (gRPC multiplexes calls over it), created on first use."""
    global _stub
    with _lock:
        if _stub is None:
            channel = grpc.insecure_channel(config.SQLGEN_ADDR, options=CHANNEL_OPTIONS)
            _stub = sqlgen_pb2_grpc.SqlGeneratorStub(channel)
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
    usage.add(reply.input_tokens, reply.output_tokens, reply.cost_usd, calls=reply.llm_calls)   # metrics: in sqlgen
    return SqlPlan(sql=reply.sql, explanation=reply.explanation, unanswerable_reason=reply.unanswerable_reason)
