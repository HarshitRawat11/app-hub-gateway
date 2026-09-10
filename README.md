# gateway

The entry-point service for [app-hub](https://github.com/HarshitRawat11) — one front door, so internal services stop being publicly reachable. Service #2 of the hub.

Runs on port **8001**. `links-service` owns 8000, and both run side by side in local development.

## Why it exists

**The product reason:** with `links-service`, `aggregator` and a frontend each exposed directly, you would have three public endpoints, three places to add authentication and three CORS configurations. A gateway collapses that to one.

**The reason it earns a place in a learning project:** it proves that **one pod can reach another by Kubernetes DNS name.** That claim underpins the whole architecture, and before this it was only demonstrated by a disposable `curl` pod. `gateway` turns it into a real, running dependency.

## API

| Method | Path | Purpose | Returns |
|---|---|---|---|
| `GET` | `/health` | Liveness probe. **Does not check `links-service`** — see below. | `{"status": "ok"}` |
| `GET` | `/links` | Proxies to `links-service` | Whatever `links-service` returns, or `502`/`503`/`504` |

## Failure mapping — the point of the service

`links-service` has no dependencies: it is up or it is down. `gateway` can be perfectly healthy while the thing it depends on is broken, so the status code has to say **which service to go and look at.**

| Situation | Code | Means |
|---|---|---|
| Upstream returned 4xx/5xx | `502 Bad Gateway` | I am fine; what I depend on is not |
| Upstream unreachable | `503 Service Unavailable` | Cannot reach it at all |
| Upstream too slow (3s timeout) | `504 Gateway Timeout` | Gave up waiting |

Returning `500` for any of these would claim *gateway* failed, sending you to debug the wrong service.

Error bodies are **fixed strings**. httpx puts the attempted URL in its exception message, which in-cluster is `http://links-service:8000/links` — internal topology, handed to anyone who curls the public endpoint. The real exception goes to the logs instead.

### `/health` deliberately does not check the upstream

It backs the Kubernetes **liveness** probe. If it verified `links-service`, then `links-service` going down would get `gateway` killed and restarted too — one outage becoming two, with the restart noise burying the real cause.

`/health` answers *"am I alive?"*, not *"is everything alive?"*. A dependency-aware check belongs on a **readiness** probe, precisely because readiness removes a pod from load balancing without killing it.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `LINKS_SERVICE_URL` | `http://localhost:8000` | Base URL of `links-service`. A trailing slash is stripped. |

The default is doing as much work as the variable: with it, local development needs zero configuration, and in the cluster the Deployment's `env:` block supplies `http://links-service:8000`. **Same image, same bytes, different behaviour** — which is why you build a container once and promote it rather than rebuilding per environment.

Read once at module scope, so `"which URL is this pod using?"` has exactly one answer for the life of the process.

## Running locally

`uv` lives in WSL, not on Windows (root `CLAUDE.md § 5`). From WSL, with `links-service` already up on 8000:

```bash
uv sync && uv run uvicorn app.main:app --reload --port 8001
```

```bash
curl http://localhost:8001/links
```

## Tests

```bash
uv run pytest
```

15 tests. `links-service` is never started — every upstream response is faked with `httpx.MockTransport`, which replaces the transport underneath the real `AsyncClient`, so the client, the `await`, the timeout and the exception handling are all genuine while nothing touches a socket. That is what makes the `502` and `504` paths testable at all.

`tests/fake_upstream.py` is a separate manual fixture for driving a real misbehaving upstream on a spare port:

```bash
python3 tests/fake_upstream.py 8002 404      # then point LINKS_SERVICE_URL at :8002
```

Modes: `ok`, `404`, `html500`, `slow`.

## Running in Docker

Docker Desktop runs on Windows but is reachable from WSL as `docker.exe`, so one shell drives everything:

```bash
docker.exe build -t gateway:local .
```

```bash
docker.exe run --rm --read-only -p 8001:8001 gateway:local
```

`--read-only` matches what the Deployment enforces, so a stray write shows up here rather than as a CrashLoopBackOff in the cluster.

**`/links` will return `503` in that container, and that is correct.** Inside the container, `localhost:8000` is the *container's* localhost, not your machine's. To make the two actually talk, put them on a shared network and address by name — which also rehearses the Kubernetes behaviour for free:

```bash
docker.exe network create app-hub-test
docker.exe run -d --name ls-test --network app-hub-test --network-alias links-service --read-only links-service:local
docker.exe run -d --name gw-test --network app-hub-test --read-only -p 8001:8001 \
  -e LINKS_SERVICE_URL=http://links-service:8000 gateway:local
curl -s localhost:8001/links
```

The Kubernetes version differs only in who supplies the DNS name — a Service instead of a network alias.

## Deployment

Not deployed yet — `S-01` step 6. Image will go to ECR at `314146298861.dkr.ecr.ap-south-1.amazonaws.com/app-hub/gateway`; manifests live in the separate `app-hub-manifests` repo.

The Service will be **`ClusterIP`, not `LoadBalancer`**: one ELB per service does not scale and each one bills. A shared ALB via Ingress is `E-06`.

The short DNS name `links-service` only resolves **within the same namespace**, so `gateway` must land in `app-hub` alongside it. In `default` it fails with a DNS resolution error that reads like a network problem.

## Background

`learn/21` covers the design rationale — `async`/`await`, the shared client, the config boundary. `learn/22` records what was built.
