# Load Balancing & Scaling Research for GraphRAG Financial Search Application

**Target**: 10,000 concurrent users  
**Current Architecture**: Python `ThreadingHTTPServer` (single process, synchronous)  
**Database**: Kuzu (embedded) / Neo4j (optional) via `sandbox_engine`  
**LLM Backends**: NVIDIA NIM, Ollama  
**External APIs**: Yahoo Finance (yfinance)

---

## 1. Architecture Options for 10K Concurrent Users

### Current Bottlenecks
- **Single-process `ThreadingHTTPServer`**: One Python process, GIL-limited CPU-bound work
- **Synchronous I/O**: `requests` library blocks threads on Yahoo Finance, Neo4j, LLM calls
- **No connection pooling**: New DB connection per request (`KnowledgeGraph` creates `lb.Connection`)
- **In-memory caches**: `_markets_cache`, `_company_detail_cache` not shared across processes
- **Global locks**: `KnowledgeGraph.lock` serializes all graph queries

### Scaling Architecture Options

| Approach | Description | Max Concurrent (est.) | Complexity |
|----------|-------------|----------------------|------------|
| **Vertical + Gunicorn** | Multiple workers via prefork, same sync code | ~500-1,000 | Low |
| **Async Migration (FastAPI/Starlette)** | `async/await` with `uvicorn` + `httpx`/`asyncpg` | ~5,000-10,000 | Medium |
| **Hybrid: Sync Workers + Async I/O** | `gunicorn` + `uvicorn` workers, thread pool for blocking calls | ~3,000-5,000 | Medium |
| **Full Microservices** | Split: API Gateway, Query Service, Ingestion, Market Data | 10,000+ | High |

**Recommendation**: **Async migration to FastAPI + uvicorn/gunicorn** — best ROI for Python workloads with high I/O wait (LLM, DB, external APIs).

---

## 2. Load Balancing Strategies

### Layer 4 (TCP) vs Layer 7 (HTTP)

| Factor | Layer 4 (HAProxy TCP, NLB) | Layer 7 (nginx, HAProxy HTTP, Traefik) |
|--------|---------------------------|----------------------------------------|
| **Latency** | Lower (no HTTP parsing) | Slightly higher |
| **Routing** | IP/port only | Path, headers, cookies, TLS termination |
| **Session Persistence** | Source IP hash | Cookie-based, sticky sessions |
| **Health Checks** | TCP connect | HTTP `/health` endpoint with body checks |
| **TLS Termination** | Passthrough only | Full termination, cert management |
| **Observability** | Limited | Rich (request/response metrics) |

**For this app**: **Layer 7 (nginx or HAProxy)** — needed for:
- Path-based routing (`/app`, `/api/*`, `/company/*`, static assets)
- WebSocket/SSE support for streaming RAG responses
- Cookie-based session affinity for auth
- Rate limiting per endpoint (`/api/ask` vs `/api/markets`)

### Load Balancing Algorithms

| Algorithm | nginx Directive | HAProxy Directive | Best For |
|-----------|----------------|-------------------|----------|
| **Round Robin** | default | `balance roundrobin` | Uniform fast requests |
| **Least Connections** | `least_conn` | `balance leastconn` | Variable latency (LLM calls) |
| **Least Time** | `least_time header` | `balance leastconn` + `observe` | Latency-sensitive |
| **IP Hash** | `ip_hash` | `balance source` | Session persistence without cookies |
| **Consistent Hash** | `consistent` (ngx_http_upstream_consistent_hash) | `balance uri` / `balance hdr(cookie)` | Cache affinity |

**Recommendation**: **Least Connections** for LLM-heavy workloads (variable response times 1-30s). Configure health checks on `/api/health` with 5s timeout.

**Sources**:
- nginx load balancing: https://nginx.org/en/docs/http/load_balancing.html
- HAProxy configuration: https://www.haproxy.org/download/2.8/doc/configuration.txt
- HAProxy load balancing algorithms: `balance roundrobin`, `leastconn`, `source` directives

---

## 3. Horizontal Scaling for Python Server

### Option A: Gunicorn + Uvicorn Workers (Recommended)

```bash
# Production command
gunicorn -k uvicorn.workers.UvicornWorker \
  -w $(nproc) \
  --worker-connections 1000 \
  --max-requests 1000 \
  --max-requests-jitter 100 \
  --timeout 120 \
  --graceful-timeout 30 \
  --bind 0.0.0.0:8000 \
  "ui.fingraph:create_app()"
```

**Gunicorn Settings** (from gunicorn docs):
- `-w $(nproc)`: Workers = CPU cores (typically 8-32)
- `--worker-connections 1000`: Max concurrent connections per worker (async)
- `--max-requests 1000`: Recycle workers to prevent memory leaks
- `--timeout 120`: Kill stuck workers (LLM calls can exceed 90s)

**Uvicorn Settings** (from uvicorn docs):
- `--loop uvloop`: 2-3x faster event loop (Linux only)
- `--http httptools`: Faster HTTP parsing
- `--limit-concurrency 1000`: Per-worker connection limit
- `--backlog 2048`: OS listen queue

### Option B: Pure Uvicorn with Multiple Processes

```bash
uvicorn --workers 8 --loop uvloop --http httptools ui.fingraph:app
```

### Worker Count Calculation for 10K Users

```
Target: 10,000 concurrent users
Assume: 100 concurrent requests per worker (async I/O)
Workers needed = 10,000 / 100 = 100 workers
With 8-core instances: 13 instances × 8 workers = 104 workers
```

**Sources**:
- Uvicorn settings: https://www.uvicorn.org/settings/
- Gunicorn configuration: https://docs.gunicorn.org/en/stable/configure.html

---

## 4. Database Connection Pooling & Scaling

### Current State
```python
# sandbox_engine/query_ui.py:788-795
class KnowledgeGraph:
    def __init__(self, db_path: Path, read_only: bool = True):
        self.db = lb.Database(str(db_path), read_only=read_only)
        self.conn = lb.Connection(self.db)  # ONE connection per handler
        self.lock = threading.Lock()        # Global lock serializes ALL queries
```

### Required Changes

#### For Kuzu (Embedded)
- Kuzu doesn't support concurrent writers; use **read replicas** or **connection pooling per process**
- Each worker process gets its own read-only Kuzu instance
- Use `multiprocessing` to share read-only DB via `fork` (copy-on-write)

#### For Neo4j (Client-Server)
```python
# Connection pool configuration
from neo4j import GraphDatabase

driver = GraphDatabase.driver(
    "neo4j://cluster:7687",
    max_connection_pool_size=50,      # Per process
    max_connection_lifetime=30*60,    # 30 min
    connection_acquisition_timeout=30,
    keep_alive=True,
)

# Session per request (auto-returned to pool)
with driver.session(database="finrag") as session:
    result = session.run(cypher, params)
```

**Neo4j Cluster Scaling** (from Neo4j docs):
- **Primary**: Handles writes (ingestion)
- **Secondaries**: Read scaling — add replicas for query throughput
- **Routing driver**: Auto-discovers cluster topology, routes reads to secondaries
- **Recommended**: 1 primary + 3-5 secondaries for 10K concurrent reads

**Sources**:
- Neo4j Clustering: https://neo4j.com/docs/operations-manual/current/clustering/
- Neo4j Python driver connection pooling: `max_connection_pool_size`, `max_connection_lifetime`

### Connection Pool Sizing

| Component | Pool Size (per worker) | Workers | Total Connections |
|-----------|----------------------|---------|-------------------|
| Neo4j | 20-50 | 100 | 2,000-5,000 |
| Redis | 50-100 | 100 | 5,000-10,000 |
| Yahoo Finance (HTTP) | 100 (httpx limits) | 100 | 10,000 |

---

## 5. Caching Strategies

### Redis for Distributed Caching

**Connection Pool** (from redis-py docs):
```python
import redis
from redis.connection import ConnectionPool

pool = ConnectionPool.from_url(
    "redis://redis-cluster:6379/0",
    max_connections=100,
    decode_responses=True,
    socket_keepalive=True,
    health_check_interval=30,
    retry_on_timeout=True,
)

r = redis.Redis(connection_pool=pool)
```

**Cache Layers**:

| Data | TTL | Invalidation | Strategy |
|------|-----|--------------|----------|
| Market quotes (`/api/markets`) | 60s | Time-based | `SETEX` with 60s TTL |
| Company detail (`/api/company/{ticker}`) | 300s | Time + write-event | `SETEX` + pub/sub invalidation |
| GraphRAG query results | 300-3600s | Question hash + graph version | Cache key: `sha256(question + graph_version)` |
| LLM responses | 3600s | Prompt hash | Cache by prompt fingerprint |
| Static assets | 1 year (CDN) | Content hash in filename | nginx `expires 1y` + `etag` |

**Redis Cluster** for HA:
- 3 masters + 3 replicas minimum
- `RedisCluster` client with `read_from_replicas=True`
- Automatic failover via Redis Sentinel or Cluster mode

### CDN for Static Assets

```
Cloudflare / CloudFront / nginx proxy_cache
  ├── /static/* (JS, CSS, images) → 1 year, immutable
  ├── /vendor/* (d3, gsap) → 1 year
  ├── /landing/* → 1 hour (marketing updates)
  └── /app/* (SPA shell) → 5 min (versioned)
```

**nginx proxy_cache config**:
```nginx
proxy_cache_path /var/cache/nginx levels=1:2 keys_zone=static:10m max_size=1g;
server {
    location /static/ {
        proxy_cache static;
        proxy_cache_valid 200 1y;
        add_header X-Cache-Status $upstream_cache_status;
    }
}
```

---

## 6. Async/Await Migration vs Thread Pool vs Multiprocessing

### Comparison

| Approach | Pros | Cons | Max Concurrency |
|----------|------|------|-----------------|
| **Thread Pool** (`concurrent.futures`) | Minimal code change, works with sync libs | GIL limits CPU, memory per thread (~8MB) | ~200-500 |
| **Multiprocessing** | True parallelism, bypasses GIL | High memory (full process), IPC overhead | ~50-100 processes |
| **Async/Await** (FastAPI + async libs) | Single-threaded, 10K+ connections, low memory | Requires async drivers, learning curve | **10,000+** |

### Migration Path

**Phase 1: Async HTTP Client** (Immediate win)
```python
# Replace requests with httpx.AsyncClient
import httpx

async def markets_async() -> list[dict]:
    async with httpx.AsyncClient(timeout=10.0, limits=httpx.Limits(max_connections=100)) as client:
        tasks = [fetch_one_async(client, t) for t in MARKET_TICKERS]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    return [r for r in results if r and not isinstance(r, Exception)]
```

**Phase 2: Async DB Driver**
- Neo4j: `neo4j.AsyncGraphDatabase` + `async_session`
- Kuzu: Run in thread pool executor (no async driver)

**Phase 3: FastAPI Migration**
```python
from fastapi import FastAPI
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http_client = httpx.AsyncClient(limits=httpx.Limits(max_connections=200))
    app.state.neo4j_driver = AsyncGraphDatabase.driver(...)
    yield
    await app.state.http_client.aclose()
    await app.state.neo4j_driver.close()

app = FastAPI(lifespan=lifespan)
```

**Phase 4: Run with uvicorn/gunicorn**

### Thread Pool for Blocking Calls (Transition)

```python
from concurrent.futures import ThreadPoolExecutor
import asyncio

# For libraries without async support (Kuzu, some LLM clients)
thread_pool = ThreadPoolExecutor(max_workers=32)

async def run_in_thread(func, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(thread_pool, func, *args)

# Usage
graph_result = await run_in_thread(kg.execute, cypher, params)
```

**Sources**:
- Python async/await: https://fastapi.tiangolo.com/async/
- Python multiprocessing: https://docs.python.org/3/library/multiprocessing.html
- redis-py connection pooling: https://redis.readthedocs.io/en/stable/connections.html

---

## 7. Technology Recommendations

### Load Balancer

| Tool | Pros | Cons | Verdict |
|------|------|------|---------|
| **nginx** | Mature, low memory, great static serving, Lua scripting | Config complexity | ✅ **Primary** |
| **HAProxy** | Best observability, stick tables, runtime API | No static serving | ✅ **Alternative** |
| **Traefik** | Auto-discovery, Let's Encrypt, Kubernetes native | Less mature for bare metal | Consider for K8s |
| **AWS ALB / GCP LB** | Managed, integrates with cloud | Cost, vendor lock-in | Cloud deployments |

**nginx config for this app**:
```nginx
upstream fingraph {
    least_conn;
    server app-1:8000 max_fails=3 fail_timeout=30s;
    server app-2:8000 max_fails=3 fail_timeout=30s;
    # ... more servers
    keepalive 64;
}

server {
    listen 443 ssl http2;
    server_name fingraph.example.com;

    # Static assets
    location /static/ {
        proxy_cache static;
        proxy_pass http://fingraph;
    }

    # API with longer timeouts for LLM
    location /api/ask {
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        proxy_pass http://fingraph;
    }

    # Default
    location / {
        proxy_pass http://fingraph;
    }
}
```

### Application Server

| Tool | Pros | Cons | Verdict |
|------|------|------|---------|
| **uvicorn + gunicorn** | Production-ready, async, worker management | Two processes | ✅ **Recommended** |
| **granian** | Rust-based, faster, single binary | Newer, less tested | Evaluate |
| **hypercorn** | HTTP/3, trio support | Slower than uvicorn | If HTTP/3 needed |

### Database

| Database | Scaling Model | Verdict |
|----------|---------------|---------|
| **Neo4j Enterprise Cluster** | Read replicas, causal clustering | ✅ Production |
| **Kuzu (embedded)** | Read-only copies per process | Development/small scale |
| **Kuzu + Neon/PostgreSQL** | Hybrid: graph + relational | If migrating |

### Caching

| Layer | Tool | Use Case |
|-------|------|----------|
| L1 (in-process) | `functools.lru_cache` / `cachetools` | Hot paths, single-worker |
| L2 (distributed) | **Redis Cluster** | Multi-worker, session, query cache |
| L3 (CDN) | **Cloudflare** / CloudFront | Static assets, landing page |

### Monitoring & Observability

- **Metrics**: Prometheus + Grafana (uvicorn exports `/metrics` with `prometheus-client`)
- **Logging**: Structured JSON logs → Loki / Elasticsearch
- **Tracing**: OpenTelemetry → Jaeger / Tempo
- **Health Checks**: `/api/health` endpoint checking DB, Redis, LLM connectivity

---

## 8. Infrastructure Requirements

### Capacity Planning for 10,000 Concurrent Users

#### Assumptions
- **Concurrent users**: 10,000
- **Requests/sec**: ~500 RPS (avg 20s session, 2-3 req/session)
- **Peak RPS**: 2,000 (burst factor 4x)
- **LLM latency**: 5-30s (streaming)
- **Graph query latency**: 50-200ms
- **Market data latency**: 200-500ms (parallel Yahoo calls)

#### Application Tier

| Component | Spec | Count | Monthly Cost (est. AWS) |
|-----------|------|-------|-------------------------|
| **App Servers** | c6i.2xlarge (8 vCPU, 16 GB) | 12 | $12,000 |
| **Load Balancer** | nginx on c6i.xlarge (4 vCPU, 8 GB) | 3 (AZs) | $1,800 |
| **Redis Cluster** | r6g.2xlarge (8 vCPU, 64 GB) | 6 (3M+3R) | $4,500 |
| **Neo4j Cluster** | r6g.4xlarge (16 vCPU, 128 GB) | 4 (1P+3S) | $8,000 |
| **CDN** | Cloudflare Pro / CloudFront | - | $200-500 |
| **Monitoring** | Prometheus/Grafana (managed) | - | $500 |

**Total Estimated Monthly**: **~$27,000-30,000**

#### Resource Utilization Targets

| Resource | Target | Alert Threshold |
|----------|--------|-----------------|
| CPU (app) | < 60% | > 80% |
| Memory (app) | < 70% | > 85% |
| Redis Memory | < 70% | > 85% |
| Neo4j Heap | < 70% | > 85% |
| Network | < 50% | > 75% |
| p99 Latency | < 2s (API), < 5s (LLM) | > 5s / > 30s |

#### Auto-scaling Rules

```yaml
# Example K8s HPA / ASG policy
scale_up:
  - metric: cpu_utilization > 70% for 2min
  - metric: request_queue_length > 50 per worker
  - metric: p99_latency > 3s
scale_down:
  - metric: cpu_utilization < 30% for 10min
  - min_replicas: 8 (baseline for HA)
max_replicas: 20
```

---

## 9. Implementation Roadmap

### Phase 1: Foundation (Week 1-2)
- [ ] Add `/api/health` endpoint with dependency checks
- [ ] Introduce `httpx.AsyncClient` for Yahoo Finance (replace `requests`)
- [ ] Add Redis connection pool, migrate in-memory caches
- [ ] Configure nginx as reverse proxy with least_conn

### Phase 2: Async Migration (Week 3-4)
- [ ] Create FastAPI app wrapper around existing handlers
- [ ] Migrate `KnowledgeGraph` to async Neo4j driver (or thread pool for Kuzu)
- [ ] Convert SSE streaming endpoint to native `StreamingResponse`
- [ ] Add structured logging + Prometheus metrics

### Phase 3: Horizontal Scale (Week 5-6)
- [ ] Deploy gunicorn + uvicorn workers (8 per instance)
- [ ] Configure Neo4j read replicas + routing driver
- [ ] Set up Redis Cluster with Sentinel
- [ ] Load test with `locust` / `k6` targeting 10K concurrent

### Phase 4: Optimization (Week 7-8)
- [ ] Implement query result caching with cache keys
- [ ] Add CDN for static assets
- [ ] Tune connection pools, timeouts, worker counts
- [ ] Chaos engineering: kill instances, verify failover

---

## 10. Key Code Changes Required

### 1. Create FastAPI App (`ui/fingraph/app.py`)
```python
from fastapi import FastAPI, Request, Response
from contextlib import asynccontextmanager
import httpx
from neo4j import AsyncGraphDatabase

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    app.state.neo4j = AsyncGraphDatabase.driver(
        "neo4j://neo4j-cluster:7687",
        max_connection_pool_size=50,
    )
    yield
    await app.state.http.aclose()
    await app.state.neo4j.close()

app = FastAPI(lifespan=lifespan)
# ... mount existing handlers as async routes
```

### 2. Gunicorn Config (`gunicorn.conf.py`)
```python
bind = "0.0.0.0:8000"
workers = 8
worker_class = "uvicorn.workers.UvicornWorker"
worker_connections = 1000
max_requests = 1000
max_requests_jitter = 100
timeout = 120
graceful_timeout = 30
keepalive = 5
preload_app = True  # Share Kuzu DB via fork (read-only)
```

### 3. Nginx Config (`nginx.conf`)
```nginx
upstream fingraph {
    least_conn;
    least_time header;
    server app-1:8000 max_fails=3 fail_timeout=30s;
    server app-2:8000 max_fails=3 fail_timeout=30s;
    keepalive 64;
}

server {
    listen 443 ssl http2;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    
    location /api/ask {
        proxy_read_timeout 300s;
        proxy_cache off;
        proxy_pass http://fingraph;
    }
    
    location /static/ {
        proxy_cache static;
        expires 1y;
        proxy_pass http://fingraph;
    }
}
```

---

## References

1. **nginx Load Balancing** - https://nginx.org/en/docs/http/load_balancing.html
2. **HAProxy Configuration Manual** - https://www.haproxy.org/download/2.8/doc/configuration.txt
3. **Uvicorn Settings** - https://www.uvicorn.org/settings/
4. **Gunicorn Configuration** - https://docs.gunicorn.org/en/stable/configure.html
5. **FastAPI Concurrency & Async** - https://fastapi.tiangolo.com/async/
6. **Python multiprocessing** - https://docs.python.org/3/library/multiprocessing.html
7. **redis-py Connections** - https://redis.readthedocs.io/en/stable/connections.html
8. **Neo4j Clustering** - https://neo4j.com/docs/operations-manual/current/clustering/
9. **Neo4j Python Driver** - Connection pooling parameters: `max_connection_pool_size`, `max_connection_lifetime`

---

*Generated: 2026-10-03*  
*For: Fin GraphRAG Financial Search Application*