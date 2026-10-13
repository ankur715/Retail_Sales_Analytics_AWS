"""SQL generation as a gRPC service (milestone 2): the chatbot's write_sql step, split out.

  sqlgen.proto       the contract (service SqlGenerator, rpc WriteSql)
  sqlgen_pb2*.py     generated stubs -- regenerate after editing the proto (see its header)
  server.py          the service: runs chatbot.llm.write_sql for each call
  client.py          what the chatbot calls: gRPC when SQLGEN_ADDR is set, in-process otherwise
"""
