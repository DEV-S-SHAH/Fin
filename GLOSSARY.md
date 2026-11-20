# India Company Graph

A chat-first product that gathers Indian company documents from NSE, BSE, MCA, and SEBI into a just-in-time graph, answers natural-language questions with provenance-tagged evidence, then destroys the graph when the session ends.

## Language

**CIN**:
The MCA Corporate Identification Number — the canonical legal identifier for one Indian company.
_Avoid_: ticker, symbol, company code, registry ID

**Ticker symbol**:
The exchange trading symbol (e.g., RELIANCE on NSE/BSE). An alias for a CIN, not the company identity itself.
_Avoid_: stock code, scrip code, company ID

**ISIN**:
International Securities Identification Number. An alias for a CIN.
_Avoid_: security ID, ticker

**Session graph**:
The in-memory graph built just-in-time for one user session, scoped to exactly one company (one CIN).
_Avoid_: company graph, persistent graph, database

**Ephemeral graph**:
A graph that exists only for the duration of a session and is destroyed when the session ends or times out.
_Avoid_: cache, store, repository

**Session TTL**:
The inactivity period after which a session graph is destroyed.
_Avoid_: expiry, timeout, retention

**JIT build**:
Building the session graph lazily, on the user's first question about a company.
_Avoid_: preload, eager load, warm-up

**Company isolation**:
The rule that one session graph holds exactly one company; cross-company questions are refused.
_Avoid_: multi-tenant graph, cross-company graph, global graph

**Provenance tag**:
The evidence quality label assigned by retrieval code (STATED, DERIVED, INFERRED, EXTERNAL, GAP), never by the model.
_Avoid_: confidence score, citation grade, quality flag

**Fiscal year end**:
The last day of a company's financial year. Default March 31 for Indian companies; per-company override allowed.
_Avoid*: financial year close, accounting year end

**NIFTY 50**:
The initial launch scope — the 50 most liquid NSE-listed companies.
_Avoid*: Nifty, NSE 50, top 50
