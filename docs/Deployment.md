**[English](Deployment.md)** | [简体中文](部署配置说明_第七阶段.md)

# Deployment Configuration

This document describes the persistent storage layout and resource budgets of a GeoThermoAI deployment. The single goal of the configuration is to let accounts, tasks, artifacts, and memory survive service restarts or container re-creation, while all users of one deployment share explicit resource limits.

## 1. Persistent directories

| Purpose | Environment variable | Docker default | Contents |
|---|---|---|---|
| Application data root | `GTAI_DATA_ROOT` | `/app/data` | Account registry, JWT key, conversations, study areas, user settings, memory, SQLite ledger, and execution manifests |
| Project data root | `WORKSPACE_ROOT` | `/app/data/users` | Large rasters, intermediate tables, and result views inside each user's projects; can be mounted on a separate large disk |
| Temporary directory | `GTAI_TEMP_ROOT` | `/app/data/tmp` | Temporary files; counted in deployment disk planning, not official artifacts |
| State database | `GTAI_STATE_DB` | `${GTAI_DATA_ROOT}/state_kernel/ledger.sqlite3` | Tasks, runs, nodes, attempts, events, artifacts, and pending memory write-backs |

At startup the program creates the directories and performs real write probes. If the SQLite path is located on a network filesystem (UNC, Windows mapped drives, NFS, CIFS, 9p, etc.), the service refuses to start. The project data root may live on a separate disk, but SQLite must not be moved to a shared network directory on that basis.

If only `GTAI_DATA_ROOT` is changed, the program copies accounts and control metadata from the repository's legacy `data` directory; existing new files are not overwritten, and large `workspace` rasters are not copied automatically. Absolute paths recorded in old projects are preserved; when large files really must be moved, an administrator should migrate them explicitly and verify the paths.

## 2. Docker persistence

The minimal reliable startup is:

```bash
docker volume create geothermoai_data
docker run -d --name geothermoai -p 7860:7860 \
  --memory=16g --cpus=16 \
  -v geothermoai_data:/app/data \
  geothermoai-image
```

Example with a separate project disk:

```bash
docker run -d --name geothermoai -p 7860:7860 \
  --memory=16g --cpus=16 \
  -v geothermoai_data:/app/data \
  -v /srv/geothermoai-projects:/projects \
  -e WORKSPACE_ROOT=/projects \
  geothermoai-image
```

When deleting and re-creating the container, the same data volume must be mounted again. Relying only on the `VOLUME` declaration in the Dockerfile without a fixed volume name produces anonymous volumes that are difficult to restore deterministically.

## 3. Scheduling resource budgets

All accounts, projects, and conversations share one deployment budget; quotas are not duplicated per user.

| Item | Environment variable | Default | Description |
|---|---|---:|---|
| Compute jobs | `GTAI_COMPUTE_JOBS` | 1 | Concurrent heavy-computation nodes |
| Download jobs | `GTAI_DOWNLOAD_JOBS` | 1 | Concurrent download nodes |
| Download connections | `GTAI_DOWNLOAD_CONNECTIONS` | 8 | Total connections for the whole deployment; must be ≥ download jobs |
| Prefetch packages | `GTAI_PREFETCH_PACKAGES` | 1 | Allowed prefetch window |
| Catalog requests | `GTAI_CATALOG_JOBS` | 2 | Concurrency for lightweight catalog requests such as STAC |
| Model requests | `GTAI_MODEL_JOBS` | 2 | Concurrency for external model calls |
| CPU budget | `GTAI_CPU_BUDGET` | auto-detected | Takes the strictest boundary among container quota, process affinity, and host value |
| Memory (GiB) | `GTAI_MEMORY_GIB` | auto-detected | Takes the strictest boundary among container and host limits, then reserves 20% headroom |
| Emergency disk margin (GiB) | `GTAI_DISK_MARGIN_GIB` | auto-detected | Stops new dispatch when free space drops below the margin |
| Disk cache (GiB) | `GTAI_DISK_CACHE_GIB` | 20 | Budget for download and execution caches; not exceeding 10% of visible disk |
| Node timeout (seconds) | `GTAI_NODE_TIMEOUT_SECONDS` | 7200 | Upper bound of a single node run |
| Network attempts | `GTAI_NETWORK_ATTEMPTS` | 3 | Bounded retry count for download-type nodes |

All values must be finite positive numbers; job counts, connection counts, prefetch counts, and attempt counts must be positive integers. Invalid configuration does not silently fall back to hidden defaults — the service reports a clear error at startup.

## 4. Memory and caches

| Item | Environment variable | Default | Description |
|---|---|---:|---|
| User runtime cache capacity | `GTAI_USER_CACHE_CAPACITY` | 8 | Maximum number of users cached by assistants, agents, and the memory manager |
| User cache idle seconds | `GTAI_USER_CACHE_TTL_SECONDS` | 1800 | After the timeout only the cache reference is dropped; objects held by in-flight requests are not destroyed |
| Write-back max attempts | `GTAI_MEMORY_WRITEBACK_ATTEMPTS` | 4 | Counted separately for JSON and vector targets; failures are recorded after the limit |
| Write-back base backoff seconds | `GTAI_MEMORY_WRITEBACK_BASE_SECONDS` | 2 | Subsequent retries use exponential backoff |
| Write-back poll seconds | `GTAI_MEMORY_WRITEBACK_POLL_SECONDS` | 1 | Polling period of the dedicated write-back executor |
| Search cache entries | `GTAI_SEARCH_CACHE_ENTRIES` | 64 | LRU capacity of the Data Space search cache |
| Search cache idle seconds | `GTAI_SEARCH_CACHE_TTL_SECONDS` | 600 | Expired entries are actually removed from memory |

Experiment JSON, experiment vectors, successful-workflow JSON, and successful-workflow vectors are four independent pending queues. A failure on one path never turns a completed scientific run into a failure and never deletes official artifacts; the task log shows "result generated, experience saving pending retry". A full pipeline enters the reusable successful-workflow store only when an evaluation result exists and the test-set R² is at least 0.75.

## 5. Configuration priority and example

Environment variables take priority over `config/deployment.json`; an empty path in the file means "use the program default". Example:

```bash
docker run -d --name geothermoai -p 7860:7860 \
  --memory=16g --cpus=16 \
  -v geothermoai_data:/app/data \
  -e GTAI_COMPUTE_JOBS=1 \
  -e GTAI_DOWNLOAD_CONNECTIONS=8 \
  -e GTAI_MEMORY_GIB=16 \
  -e GTAI_DISK_CACHE_GIB=20 \
  geothermoai-image
```

Startup logs print the resolved persistent paths and the final effective budgets, so you can confirm whether the container limits take effect. `config/deployment.json` contains equivalent Chinese annotations and can be used as the versioned default configuration.
