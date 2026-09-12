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
| `GET` | `/links` | Proxies to `links-service` | The link list, or `502`/`503`/`504` |
| `GET` | `/links/{id}` | Proxies to `links-service` | The link, `404` if unknown, or `502`/`503`/`504` |
| `POST` | `/links` | Proxies to `links-service` | `201` + `Location`, `422` if invalid, or `502`/`503`/`504` |
| `DELETE` | `/links/{id}` | Proxies to `links-service` | `200`, `404` if unknown, or `502`/`503`/`504` |
| `GET` | `/` | The dashboard (`S-03`) | HTML |
| `GET` | `/static/*` | Dashboard assets | CSS / JS |

### Not every upstream 4xx is a gateway fault

`404` and `422` above are **passed through**, and that is a deliberate exception to the mapping below. A `404` from `GET /links/{id}` is the correct answer to a question about an id that does not exist; a `422` from `POST` is about what the **caller** sent. Reporting either as `502` would say *the server is broken* and send someone to read gateway's logs instead of checking their own request.

`GET /links` passes **nothing** through, because the collection always exists — a `404` there really does mean links-service is serving something other than what gateway thinks it is. The asymmetry is the point.

`POST` forwards the body as an opaque `dict` rather than validating it. links-service owns the link schema; restating it here would give the project two definitions that drift, and a gateway that silently strips a field it has not been told about looks exactly like a client that never sent one.

## Failure mapping — the point of the service

`links-service` has no dependencies: it is up or it is down. `gateway` can be perfectly healthy while the thing it depends on is broken, so the status code has to say **which service to go and look at.**

| Situation | Code | Means |
|---|---|---|
| Upstream returned 4xx/5xx | `502 Bad Gateway` | I am fine; what I depend on is not |
| Upstream unreachable | `503 Service Unavailable` | Cannot reach it at all |
| Upstream too slow (3s timeout) | `504 Gateway Timeout` | Gave up waiting |

Returning `500` for any of these would claim *gateway* failed, sending you to debug the wrong service.

Error bodies are **fixed strings**. httpx2 puts the attempted URL in its exception message, which in-cluster is `http://links-service:80/links` — internal topology, handed to anyone who curls the public endpoint. The real exception goes to the logs instead.

### `/health` deliberately does not check the upstream

It backs the Kubernetes **liveness** probe. If it verified `links-service`, then `links-service` going down would get `gateway` killed and restarted too — one outage becoming two, with the restart noise burying the real cause.

`/health` answers *"am I alive?"*, not *"is everything alive?"*. A dependency-aware check belongs on a **readiness** probe, precisely because readiness removes a pod from load balancing without killing it.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `LINKS_SERVICE_URL` | `http://localhost:8000` | Base URL of `links-service`. A trailing slash is stripped. |

The default is doing as much work as the variable: with it, local development needs zero configuration, and in the cluster the Deployment's `env:` block supplies `http://links-service:80`. **Note the port changes too** — 8000 is what the *containers* listen on, but the `links-service` Service exposes **80** and forwards to 8000. A consumer addresses the Service, not the container. **Same image, same bytes, different behaviour** — which is why you build a container once and promote it rather than rebuilding per environment.

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

47 tests, in three files: `test_gateway.py` (the original failure mapping), `test_proxy_crud.py` (the other three routes and the passthrough rule) and `test_dashboard.py` (the static page). `links-service` is never started — every upstream response is faked with `httpx2.MockTransport`, which replaces the transport underneath the real `AsyncClient`, so the client, the `await`, the timeout and the exception handling are all genuine while nothing touches a socket. That is what makes the `502` and `504` paths testable at all.

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

**Deployed and verified on EKS, 2026-09-10.** Image at `314146298861.dkr.ecr.ap-south-1.amazonaws.com/app-hub/gateway`, tagged with the git SHA it was built from; manifests live in the separate `app-hub-manifests` repo. Two replicas, since gateway holds no state.

The Service is **`ClusterIP`, not `LoadBalancer`**: one ELB per service does not scale and each one bills. A shared ALB via Ingress is `E-06`, which will also flip `links-service` to `ClusterIP` so it stops being publicly reachable — the point of having a gateway. Until then:

```bash
kubectl -n app-hub port-forward svc/gateway 8001:8001
```

**In-cluster, gateway reaches `links-service` at `http://links-service:80` — the Service's port, not the container's 8000.** Getting that wrong produces `ConnectTimeout` (DNS resolves, packets are dropped), not `ConnectError`. The short DNS name also only resolves **within the same namespace**, so gateway must land in `app-hub` alongside it.

## Background

`learn/21` covers the design rationale — `async`/`await`, the shared client, the config boundary. `learn/27` covers the dashboard and the full proxy.

There is no `learn/22`: steps 4–6 were delegated, and `learn/` files are written for hand-built work. An earlier version of this README pointed at it as though it existed.
