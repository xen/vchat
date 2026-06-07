# Reindex Monitoring Report

Date: 2026-06-05
Server: `deploy@cdn.okumy.com`
Project path: `/var/www/vchat`
Commit: `655b16b`

## Scope

Monitor the clean reindex run after deleting derived data while preserving sources and sitemap records. Confirm that crawling, chunk materialization, shingles, and embeddings keep moving. Record issues, recommendations, and follow-up fix plan.

## Runtime Facts

Historical snapshot from this 2026-06-05 run; do not treat these Redis DBs as
the current repo defaults.

- App Redis: `redis://localhost:6379/13`
- Celery broker Redis: DB `14`
- Celery backend Redis: DB `15`
- Redis has 16 DBs on this server, so DB `30` is not available in this runtime.
- Active services: `vchat-backend.service`, `vchat-celery.service`, `vchat-embedder.service`
- Embedder service command includes `--max-tasks-per-child=1`, so every `pending_chunks` task starts a fresh child and reloads the embedding model.

## Cleanup Performed

Preserved:

- `source`
- `sitemap`

Deleted derived data:

- `page`
- `chunk`
- `page_shingle`
- `page_link`
- `trigger_response_cache`
- `crawl_run`
- stale Redis queue keys in broker DB `14`
- stale `vchat:embed:*` schedule/counter keys in app Redis DB `13`

Reindex was started for all 36 active sources.

## Monitoring Notes

### 18:31 CEST

- `page=8565`
- `chunk=2571`
- `chunk_with_embedding=0`
- `chunk_without_embedding=2571`
- `celery=452`
- `embeddings=0`

Finding: crawling and chunk materialization were moving, but no embedding workers had been scheduled yet because `ensure_pending_chunks` was waiting behind the busy default `celery` queue.

Action: ran the normal `ensure_pending_chunks` function from the project venv. It scheduled 32 `pending_chunks` tasks on the `embeddings` queue.

### 18:35 CEST

- `page=8577`
- `chunk=8776`
- `chunk_with_embedding=8`
- `chunk_without_embedding=8768`
- `celery=82`
- `embeddings=30`

Finding: embeddings were being written, but slowly. The first batch completed: 8 chunks in about 102 seconds on CPU.

### 18:40 CEST

- `page=8688`
- `chunk=9919`
- `chunk_with_embedding=16`
- `chunk_without_embedding=9903`
- `celery=0`
- `embeddings=31`

Finding: default Celery queue drained, crawler/indexer were healthy. Embedder remained the bottleneck.

Action: tried `embedding_pending_chunks_batch_size: 32` in server `local.yaml` to reduce model reload overhead.

### 18:49 CEST

- `page=9290`
- `chunk=14820`
- `chunk_with_embedding=32`
- `chunk_without_embedding=14788`

Finding: batch size 32 applied, but selected a 93k-character batch and did not finish promptly.

### Pathological Chunk Finding

Three pages from source `33` generated a pathological number of chunks:

- `8247` `https://ksp.vbudushee.ru/identity/account/login`: 904 chunks, about 6.24M chunk chars
- `8249` `https://ksp.vbudushee.ru/public/home/documents`: 927 chunks, about 6.17M chunk chars
- `8252` `https://ksp.vbudushee.ru/public/home/documents?tagId=949`: 927 chunks, about 6.17M chunk chars

The original page content was under 100k chars, but materialized chunks totaled over 6M chars per page. This is not normal corpus shape.

Action:

- stopped embedder
- cleared generic `embeddings` queue/counters
- marked those 3 pages `status_error='too_big'`
- deleted their chunks and page shingles
- restarted embedder and rescheduled pending chunk workers

### 19:01 CEST

- `page=9714`
- `chunk=17846`
- `chunk_with_embedding=32`
- `chunk_without_embedding=17814`

Finding: batch size 16 also selected a long 47k-character batch and did not commit quickly enough for stable monitoring.

Action: reverted `embedding_pending_chunks_batch_size` to `8`, the mode already proven to complete batches, even though it is slow.

### 19:12 CEST

- `page=9924`
- `page_ready=2708`
- `page_crawler=7216`
- `page_errors=434`
- `chunk=21626`
- `chunk_with_embedding=40`
- `chunk_without_embedding=21586`
- `celery=0`
- `embeddings=31`
- `crawl_run_active=2`

Finding: the initial monitoring hour confirms that crawling and chunk materialization are progressing smoothly. The default Celery queue is drained. Embedder is alive and committing work again with batch size 8.

Observed embedder throughput remains very low:

- 8 chunks / 916 chars completed in 53 seconds after cold model load.
- The next batch selected 8 chunks / 46,165 chars, so runtime will vary heavily based on chunk length.

Assessment: the process is healthy but embedding completion time is not acceptable without a code/runtime fix. The current production-safe choice is to keep batch size 8 because it commits, and avoid further restarts during monitoring.

### 20:13 CEST - One-Hour Control Cycle 1

- `page=10734`
- `page_ready=3989`
- `page_crawler=6744`
- `page_parsing=1`
- `page_errors=451`
- `chunk=31361`
- `chunk_with_embedding=48`
- `chunk_without_embedding=31313`
- `page_shingle=13609171`
- `page_link=432098`
- `crawl_run_active=1`
- `celery=0`
- `embeddings=95`

Delta since 19:12:

- `page`: +810
- `page_ready`: +1281
- `chunk`: +9735
- `chunk_with_embedding`: +8
- `chunk_without_embedding`: +9727

Finding: crawling and materialization are still moving. Embeddings are not moving at an acceptable rate: only 8 chunks were embedded in the hour.

Embedder log:

- one batch of 8 chunks took about 3002 seconds
- next active batch is 8 chunks / 42,081 chars

Incident: `vchat-celery.service` was OOM-killed around 19:55 CEST and restarted by systemd. It is currently active again. After restart, memory is stable enough for continued monitoring:

- `vchat-celery.service`: about 1.6GB
- `vchat-embedder.service`: about 1.8GB of its 2GB limit
- active embedder child is using CPU and is not idle

Active crawl run:

- source `18`
- age about 1h49m
- `pages_changed=1083`
- `pages_errors=0`

Current largest pending embedding pages are mostly source `4` book pages. They are legitimate long pages, commonly around 69 chunks and about 126k chunk chars per page.

Assessment: this is not smooth enough to move to two-hour forecast-only monitoring yet. The run is progressing, but embedder throughput is effectively blocked by long text batches on CPU.

### 21:14 CEST - One-Hour Control Cycle 2

- `page=10734`
- `page_ready=3989`
- `page_crawler=6744`
- `page_parsing=1`
- `page_errors=451`
- `chunk=31361`
- `chunk_with_embedding=128`
- `chunk_without_embedding=31233`
- `page_shingle=13609171`
- `page_link=432098`
- `crawl_run_active=1`
- `celery=0`
- `embeddings=95`

Delta since 20:13:

- `page`: no change
- `page_ready`: no change
- `chunk`: no change
- `chunk_with_embedding`: +80
- `chunk_without_embedding`: -80

Finding: embedder progressed more predictably in this window, but still very slowly. At +80 chunks/hour against about 31k pending chunks, completion would take roughly 390 hours, or about 16 days, if no code/runtime changes are made.

Recent batch durations varied heavily:

- 8 chunks / 42,081 chars: active for a long interval
- 8 chunks / 3,820 chars: 180s
- 8 chunks / 1,330 chars: 35s
- 8 chunks / 19,024 chars: 399s
- 8 chunks / 27,208 chars: 1323s

Finding: `crawl_run_active=1` was stale. Source `18` had an open run but there was no active crawler task or process. This was likely left behind by the earlier OOM restart.

Action:

- marked stale active crawl run for source `18` as `interrupted`
- requeued crawl for source `4` and source `18`, the two largest crawler backlogs

### 21:17 CEST - Post-Stale-Run Recovery Check

- `page=10734`
- `page_ready=10441`
- `page_crawler=293`
- `chunk=31601`
- `chunk_with_embedding=128`
- `chunk_without_embedding=31473`
- active crawl run: source `18`, age about 1m23s

Finding: crawler recovery worked. The source `4` backlog collapsed from thousands of `crawler` pages to mostly `ready`; source `18` is actively crawling again.

Assessment: crawling is now mostly recovered. Embeddings remain the gating issue.

### 22:18 CEST - Post-Recovery One-Hour Control

- `page=11193`
- `page_ready=11106`
- `page_crawler=87`
- `page_parsing=0`
- `page_errors=6876`
- `chunk=37962`
- `chunk_with_embedding=176`
- `chunk_without_embedding=37786`
- `page_shingle=14988572`
- `page_link=461361`
- `crawl_run_active=0`
- `celery=0`
- `embeddings=95`

Delta since 21:17:

- `page`: +459
- `page_ready`: +665
- `page_crawler`: -206
- `chunk`: +6361
- `chunk_with_embedding`: +48
- `chunk_without_embedding`: +6313

Finding: crawler has effectively completed active work. Only 87 pages remain in `crawler` status, and no crawl run is active.

The large `page_errors` count is mainly policy-related:

- `excluded_robots=6450`, including 6425 pages from source `4`
- `too_big=211`
- `low_content=182`
- `no_content=31`
- `extraction_failed=2`

Finding: no new embedder errors, but throughput remains poor. Latest completed batches ranged from about 30 seconds to 24 minutes for 8 chunks, depending on text length. Current active batch is 8 chunks / 27,297 chars.

Assessment: crawling is now stable enough for periodic observation. Embeddings are progressing predictably but far too slowly for the corpus size.

## Current Assessment

Crawling and chunk materialization are mostly healthy after clearing a stale crawl run and requeuing the two largest backlogs. The default Celery queue drains normally.

The embedder is functional but too slow for this corpus on the current CPU-only runtime. The primary causes are:

- embedding model reload on every task due to `--max-tasks-per-child=1`
- long chunks from book/document pages
- batch selection ordered by `page_id, chunk_ix`, which can group multiple long chunks together
- no adaptive batch cap by total characters
- at least one OOM restart happened in the crawler/default worker during the heavy run

## Recommendations

1. Add an adaptive total-character cap to pending embedding batch selection.
   Keep `PENDING_CHUNKS_BATCH_SIZE` as a max count, but stop adding chunks once total chars exceed a configurable threshold.

2. Add a chunk explosion guard.
   If one page materializes hundreds of chunks or chunk text total greatly exceeds page content length, mark it as `too_big` or `embedder_failed` and delete its chunks.

3. Avoid routing `ensure_pending_chunks` behind a saturated default queue.
   Either schedule it more directly or separate scheduler control tasks from crawler/index heavy work.

4. Revisit `--max-tasks-per-child=1`.
   It prevents long-lived memory growth, but causes model reload per batch. A safer compromise may be higher max tasks per child plus stricter batch character caps and memory monitoring.

5. Add monitoring queries/alerts for:
   - pending chunks age
   - `chunk_with_embedding` rate
   - pages with abnormal `count(chunk)` or `sum(length(chunk.text))`
   - Redis `embeddings` queue length and app Redis pending counter

6. Consider a temporary operational policy for very long book pages.
   If search quality can tolerate it, mark oversized book pages as too large for embeddings until adaptive batching lands. This would let the rest of the site corpus complete.

7. Add stale crawl-run recovery.
   If a crawl run is open but no matching Celery task or crawler process exists, mark it `interrupted` and requeue the source under controlled concurrency.

## Current Forecast

The crawler side now looks recoverable and should finish much earlier than embeddings. The embedding side is not acceptable for a full corpus run:

- observed second-cycle throughput: about 80 chunks/hour
- observed post-recovery throughput: about 48 chunks/hour
- current pending chunks after crawler recovery: about 37.8k
- rough completion time at this rate range: about 20-33 days

This is predictable enough to keep observing every two hours, but not healthy enough to treat as acceptable. Embedder batching requires a code/runtime fix.

## Two-Hour Checks

### 00:19 CEST, 2026-06-06

Baseline: 22:18 CEST, `chunk_with_embedding=176`, `chunk_without_embedding=37786`.

- `page=11193`
- `page_ready=11106`
- `page_crawler=87`
- `page_errors=6876`
- `chunk=37962`
- `chunk_with_embedding=320`
- `chunk_without_embedding=37642`
- `crawl_run_active=0`
- `celery=0`
- `embeddings=95`
- services: active

Delta over 2 hours:

- `chunk_with_embedding`: +144
- `chunk_without_embedding`: -144
- embedding throughput: about 72 chunks/hour

Embedder batch durations are still highly variable:

- short batches: about 29-39s for small text batches
- long batches: 807s, 936s, 1155s, 1319s for long batches

No new service-level errors were reported in this two-hour window.

Forecast at this rate:

- pending chunks: 37,642
- estimated completion: about 523 hours, or about 22 days

Assessment: stable but too slow. Continue observation, but the recommended path remains a code/runtime fix.

### 02:20 CEST, 2026-06-06

Baseline: 00:19 CEST, `chunk_with_embedding=320`, `chunk_without_embedding=37642`.

- `page=11193`
- `page_ready=11106`
- `page_crawler=87`
- `page_errors=6876`
- `chunk=37962`
- `chunk_with_embedding=392`
- `chunk_without_embedding=37570`
- `crawl_run_active=0`
- `celery=0`
- `embeddings=95`
- services: active

Delta over 2 hours:

- `chunk_with_embedding`: +72
- `chunk_without_embedding`: -72
- embedding throughput: about 36 chunks/hour

Long batches dominated this interval:

- 8 chunks / 16,247 chars: 1469s
- 8 chunks / 32,517 chars: 1198s
- 8 chunks / 38,115 chars: 746s
- 8 chunks / 51,499 chars: 1269s
- next active batch: 8 chunks / 67,787 chars

No new service-level errors were reported in this two-hour window.

Forecast at this rate:

- pending chunks: 37,570
- estimated completion: about 1044 hours, or about 43 days

Assessment: still stable, but throughput is degrading as the queue reaches longer book chunks. The need for adaptive character-capped batching is stronger.

### 04:21 CEST, 2026-06-06

Baseline: 02:20 CEST, `chunk_with_embedding=392`, `chunk_without_embedding=37570`.

- `page=11193`
- `page_ready=11106`
- `page_crawler=87`
- `page_errors=6876`
- `chunk=37962`
- `chunk_with_embedding=608`
- `chunk_without_embedding=37354`
- `crawl_run_active=0`
- `celery=0`
- `embeddings=95`
- services: active

Delta over 2 hours:

- `chunk_with_embedding`: +216
- `chunk_without_embedding`: -216
- embedding throughput: about 108 chunks/hour

This interval had a better mix of shorter batches, but long batches still appear:

- short batches: 11-55s for small text
- long batches: 858s, 1026s, 1103s, 1182s
- next active batch: 8 chunks / 19,706 chars

No new service-level errors were reported.

Forecast at this rate:

- pending chunks: 37,354
- estimated completion: about 346 hours, or about 14-15 days

Assessment: stable and moving, but still not operationally acceptable for full completion without batching/runtime fixes.

### 06:21 CEST, 2026-06-06 - Final 12-Hour Window Check

Baseline: 04:21 CEST, `chunk_with_embedding=608`, `chunk_without_embedding=37354`.

- `page=11193`
- `page_ready=11106`
- `page_crawler=87`
- `page_errors=6876`
- `chunk=37962`
- `chunk_with_embedding=896`
- `chunk_without_embedding=37066`
- `crawl_run_active=0`
- `celery=0`
- `embeddings=95`
- services: active

Delta over 2 hours:

- `chunk_with_embedding`: +288
- `chunk_without_embedding`: -288
- embedding throughput: about 144 chunks/hour

This was the best observed two-hour interval. The queue hit a run of shorter batches, with several batches completing in 25-80 seconds. Long batches still appeared:

- 8 chunks / 19,763 chars: 943s
- 8 chunks / 17,854 chars: 1327s
- active batch at final check: 8 chunks / 29,588 chars

No new service-level errors were reported.

Forecast at this interval's rate:

- pending chunks: 37,066
- estimated completion: about 257 hours, or about 10-11 days

Conservative forecast using the observed two-hour intervals:

- 00:19 interval: 72 chunks/hour
- 02:20 interval: 36 chunks/hour
- 04:21 interval: 108 chunks/hour
- 06:21 interval: 144 chunks/hour
- average: about 90 chunks/hour
- estimated completion at average rate: about 412 hours, or about 17 days

## 12-Hour Summary

The reindex run is no longer blocked.

Crawler status:

- Clean reindex successfully repopulated pages, shingles, links, and chunks.
- Active crawler work is complete: `crawl_run_active=0`.
- Only `87` pages remain in `crawler` status.
- The large error count is mostly robots/rule policy, not infrastructure failure: `excluded_robots=6450`, mostly source `4`.

Embedder status:

- Embedder remains active and continues committing embeddings.
- `chunk_with_embedding` reached `896`.
- `chunk_without_embedding` remains high at `37066`.
- Queue stays around `embeddings=95` because scheduler/inflight slots keep work available.
- The process is predictable enough to leave running, but far too slow to be operationally acceptable.

Final assessment:

- System health: acceptable after recovery; services active and no recent service-level errors.
- Data pipeline health: crawler/materializer healthy; embedder functional.
- Performance: unacceptable for full corpus completion on current CPU-only settings.

Immediate next engineering work:

1. Implement adaptive char/token-capped embedding batches.
2. Add chunk explosion guard at page materialization time.
3. Add stale crawl-run watchdog and controlled requeue.
4. Revisit embedder worker lifetime after batching is safe; `--max-tasks-per-child=1` reloads the model per task and dominates runtime.
5. Add dashboard/alerts for embedding throughput and abnormal chunk distribution.

## Next Monitoring Plan

1. Initial one-hour control window: completed at 19:12 CEST.
2. Run two additional one-hour waits with control measurements after each.
3. If growth is predictable, estimate completion time from observed embedding throughput.
4. Continue checking status every two hours for 12 hours total.

## Follow-Up Checkpoint

Timestamp: `Sat Jun 6 11:04:35 CEST 2026`

Fresh status:

- Services: backend, celery, and embedder are active.
- Redis queues: `crawler=0`, `embeddings=95`, `celery=0`.
- App Redis embed counter: `32`.
- Pages: `11193` total, `11106` ready, `87` still marked `crawler`, `0` parsing.
- Errors: `6876`, unchanged from the final 12h checkpoint.
- Chunks: `37962` total, `1568` with embeddings, `36394` without embeddings.
- Derived data volume: `14988572` page shingles, `461361` page links.
- Active crawl runs: `0`.
- Active Celery tasks: one active `jobs.embedder.tasks.pending_chunks` task on the embedder; main celery worker has no active tasks.

Progress since `06:21:51 CEST`:

- Embedded chunks increased from `896` to `1568`: `+672` chunks.
- Elapsed time: about `4.7` hours.
- Observed rate: about `143` chunks/hour.

Updated forecast:

- At the current observed rate, `36394` remaining chunks need about `255` hours, or about `10.6` days.
- Practical forecast is `10-12` days if the latest rate holds.
- Risk range remains around `10-17` days because long pages still create slow batches; recent 8-chunk batches ranged from seconds to about 20 minutes.

Operational assessment:

- Crawler/materialization side is stable: no active crawl runs and crawler queue is empty.
- Embedder is healthy but slow. It is CPU-bound and still affected by large total-character batches even with batch size `8`.
- No immediate Redis cleanup is needed now; the queues are consistent with one active embedder and scheduled follow-up embedding tasks.

## Two-Embedder Trial

Timestamp: `Sat Jun 6 14:49:32 CEST 2026`

Change:

- Started a second transient user-systemd unit: `vchat-embedder-2.service`.
- Command matches the main embedder queue/config but uses a unique Celery hostname: `vchat-embedder-2@cdn-okumy`.
- Both workers use `--autoscale=1,1`, `--max-tasks-per-child=1`, `OMP_NUM_THREADS=1`, `MKL_NUM_THREADS=1`, and `MemoryMax=2G`.

Initial verification:

- `vchat-embedder.service`: active.
- `vchat-embedder-2.service`: active.
- Redis `embeddings` queue: `94`.
- Chunks: `1832` with embeddings, `36130` without embeddings.
- Two CPU-bound embedder child processes were visible at the same time:
  - main embedder child: about `94%` CPU, `1155372` KB RSS.
  - second embedder child: about `89%` CPU, `814832` KB RSS.
- Memory after starting the second worker: `4717 MB` available, swap used `3382 MB`.

Early impact:

- At `14:39`, chunks with embeddings were `1784`.
- At `14:49`, chunks with embeddings were `1832`.
- Early two-worker delta: `+48` chunks in about `10` minutes.
- This short interval is noisy because the first worker was already on a long batch and both workers reload the model after each task, but the second worker is confirmed to consume the `embeddings` queue in parallel.

Updated short-window forecast:

- If the early two-worker rate held exactly, throughput would be about `288` chunks/hour and the remaining `36130` chunks would take about `125` hours, or about `5.2` days.
- This is too optimistic for a committed forecast until at least a 1-2 hour two-worker window is measured.
- Practical forecast after enabling the second worker: `5-8` days if memory remains stable and both workers continue receiving work.
- Risk range remains `5-12` days because long total-character batches can still dominate individual worker time.

Operational assessment:

- The second worker successfully occupies another physical core.
- No immediate OOM/restart was visible in the first control window.
- Keep the second transient unit running for now, but watch memory pressure and embedding throughput over a longer window.

## Two-Embedder Follow-Up

Timestamp: `Sat Jun 6 21:59:43 CEST 2026`

Fresh status:

- Services: backend, celery, main embedder, and second embedder are active.
- Redis queues: `crawler=0`, `embeddings=94`, `celery=0`.
- Pages: `11193` total, `11106` ready, `87` still marked `crawler`, `0` parsing.
- Page status errors: `6876` total:
  - `excluded_robots=6450`
  - `too_big=211`
  - `low_content=182`
  - `no_content=31`
  - `extraction_failed=2`
- Chunks: `37962` total, `2808` with embeddings, `35154` without embeddings.
- Derived data volume remains stable: `14988572` page shingles, `461361` page links.
- Active crawl runs: `0`.

Embedder runtime:

- Both embedder units remain active.
- Two CPU-bound embedder children were visible:
  - second embedder child: about `100%` CPU, `1378652` KB RSS.
  - main embedder child: about `97%` CPU, `728528` KB RSS.
- Memory remains acceptable: about `4445 MB` available, swap used `3358 MB`.
- Recent logs show ongoing successful batches from both workers. No OOM/restart was visible in this checkpoint.

Progress since the two-embedder trial checkpoint:

- At `14:49`, chunks with embeddings were `1832`.
- At `21:59`, chunks with embeddings were `2808`.
- Delta: `+976` chunks over about `7.17` hours.
- Observed two-worker rate over this longer window: about `136` chunks/hour.

Updated forecast:

- Remaining chunks: `35154`.
- At the observed two-worker rate of about `136` chunks/hour, completion needs about `258` hours, or about `10.8` days.
- The early `5-8` day projection did not hold over the longer window.
- Practical forecast is back to about `10-12` days unless batching/model reload behavior is fixed.

Assessment:

- The second worker is technically working and occupying another CPU core.
- End-to-end throughput did not double. Long total-character batches and `--max-tasks-per-child=1` model reloads still dominate runtime.
- Keeping the second worker is still useful, but the main fix remains adaptive char/token-capped batching plus revisiting worker lifetime after memory behavior is controlled.
