"""The SQL generation service: one RPC, WriteSql, wrapping chatbot.llm.write_sql -- the
same prompt and provider (LLM_PROVIDER) the chatbot uses in-process.

Why a separate service: writing SQL is the step most likely to change on its own (prompt
work, a different or self-hosted model, RAG over the catalog in milestone 5) and to need
its own scaling. Behind a typed contract it can be redeployed, scaled and load-tested
without the web app.

LLM failures cross the wire as status codes (see sqlgen.proto) and the client turns them
back into LLMUnavailable, so the chatbot's 503 handling is unchanged. Traces continue
across the call (the gRPC instrumentation carries the context), and the service's
step metrics are on :9101/metrics.

Run:  python -m sqlgen.server        (compose service `sqlgen`, port 50051)
"""
import logging
import os
import signal
from concurrent import futures

import grpc
from prometheus_client import start_http_server

from chatbot import catalog, llm, telemetry
from chatbot.llm_types import LLMUnavailable
from sqlgen import sqlgen_pb2, sqlgen_pb2_grpc

SERVICE = "sqlgen"
CODES = {"busy": grpc.StatusCode.UNAVAILABLE, "quota": grpc.StatusCode.RESOURCE_EXHAUSTED,
         "config": grpc.StatusCode.FAILED_PRECONDITION, "declined": grpc.StatusCode.PERMISSION_DENIED}
log = logging.getLogger(SERVICE)
# Close each client connection after ~30 s (clients reconnect transparently). Behind a ClusterIP
# Service, a long-lived HTTP/2 connection would pin a client to one pod forever; reconnecting
# lets kube-proxy spread clients across pods, including ones added by a scale-up.
OPTIONS = [("grpc.max_connection_age_ms", 30_000), ("grpc.max_connection_age_grace_ms", 10_000)]
STOP_GRACE_S = 15   # on SIGTERM, in-flight calls get this long to finish (pod terminationGracePeriodSeconds: 20)


class SqlGenerator(sqlgen_pb2_grpc.SqlGeneratorServicer):
    def WriteSql(self, request, context):
        unknown = [v for v in request.views if v not in catalog.view_names()]
        if not request.question.strip() or not request.views or unknown:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT,
                          f"need a question and catalog views (unknown: {unknown})")
        try:
            plan = llm.write_sql(request.question, list(request.views), request.details,
                                 previous_error=request.previous_error or None,
                                 previous_sql=request.previous_sql or None)
        except LLMUnavailable as e:
            context.abort(CODES.get(e.kind, grpc.StatusCode.UNAVAILABLE), str(e))
        return sqlgen_pb2.SqlPlan(sql=plan.sql, explanation=plan.explanation,
                                  unanswerable_reason=plan.unanswerable_reason)


def build(address: str, workers: int) -> tuple[grpc.Server, int]:
    """A started-later server bound to `address`; returns it and the bound port (tests use port 0)."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=workers), options=OPTIONS)
    sqlgen_pb2_grpc.add_SqlGeneratorServicer_to_server(SqlGenerator(), server)
    return server, server.add_insecure_port(address)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if telemetry.init_tracing(SERVICE):
        from opentelemetry.instrumentation.grpc import GrpcInstrumentorServer
        GrpcInstrumentorServer().instrument()
    start_http_server(int(os.environ.get("SQLGEN_METRICS_PORT", "9101")))
    # Each call blocks on the model for ~1 s, so threads, not cores, set the concurrency.
    server, port = build(f"0.0.0.0:{os.environ.get('SQLGEN_PORT', '50051')}",
                         int(os.environ.get("SQLGEN_WORKERS", "32")))
    server.start()
    log.info("sqlgen listening on :%s (provider %s)", port, llm.config.LLM_PROVIDER)
    # Kubernetes sends SIGTERM before killing a pod: stop taking new calls, let running ones finish.
    signal.signal(signal.SIGTERM, lambda *_: (log.info("SIGTERM: draining"), server.stop(STOP_GRACE_S)))
    server.wait_for_termination()


if __name__ == "__main__":
    main()
