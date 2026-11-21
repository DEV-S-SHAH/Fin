"""FastAPI backend for the India Company Graph.

Exposes a REST API that the TypeScript frontend consumes:
  POST   /api/session              create a session
  POST   /api/session/{id}/bind    bind a company (ticker -> CIN)
  POST   /api/session/{id}/ask     ask a question, returns answer + evidence
  GET    /api/session/{id}/graph   return graph nodes/edges for visualization
  GET    /api/companies            list NIFTY 50 launch-scope companies
  DELETE /api/session/{id}         destroy a session (graph wiped)
"""
