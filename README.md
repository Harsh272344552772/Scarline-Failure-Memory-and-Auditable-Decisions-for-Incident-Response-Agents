# Scarline: failure memory for incident response

Scarline receives monitoring alerts, records a live six-stage investigation and checks each proposed remedy against reviewed failure evidence. The dashboard updates through server-sent events. All infrastructure actions, health checks and rollbacks remain **simulated**. Alert intake, database persistence and network integrations are implemented; they do not turn the target simulator into a production executor.

This upgrade adds grouped Grafana and Alertmanager alerts, correlated telemetry, durable retries, visible agent activity, a bounded live demo, Meko runbook upload/retrieval, audit artifacts and optional named access roles. Fresh workspaces contain five services, five scars and 15 decisions. Existing SQLite ledgers migrate without replacing historical decisions.

## Start on Windows, macOS or Linux

Extract the ZIP and open the `Scarline` folder. Use Python 3.11 or newer. Node and Docker are unnecessary.

| Platform | Local demo, no dependencies | Full backend with integrations |
| --- | --- | --- |
| Windows | `start.cmd` | `start-full.cmd` |
| macOS / Linux | `make dev` | `make full` |

Open the URL printed by the launcher, usually **http://127.0.0.1:8000**. `make dev PORT=8080` changes the local port. For manual installation:

```sh
python -m venv .venv
# Activate .venv using your shell, then:
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

1. Open **Live activity → Start 10-incident demo**. A synthetic incident is queued approximately every 15 seconds while the worker is available. Stop it at any time. The default run stops after ten incidents; the API caps a run at 40.
2. Select a received incident to follow its activity. New incidents entered in the dashboard also return immediately and run through this queue. Choose **Open decision** for the explanation, evidence and six-stage trace.
3. Under **Services**, change `payments-api` from `2.14` to `2.15`. Replay its blocked worker restart. The new verdict is Caution and waits for a supervised retest. The original decision remains Blocked.
4. Approve the retest with a reviewer and reason. Inspect the simulated health result and the HTML **Audit report**.

The local server binds to loopback. The full backend supports PostgreSQL through `DATABASE_URL` and uses SQLite otherwise. Keep one application process and one instance: an incident worker and a separate outgoing-cloud worker run inside that process. This is a small-team prototype, not a horizontally scaled queue service.

## Decision policy and roles

| Stage | Responsibility |
| --- | --- |
| Observer | Read recent service telemetry and identify missing or stale observations |
| Investigator | Propose an action using the Responder module, incident context and retrieved runbooks |
| Skeptic | Compare the action with the current local ledger and Meko memory references |
| Risk Evaluator | Require supervision for high-impact changes; prohibit destructive operations |
| Executor | Simulate an allowed action, a health check and rollback on failure |
| Scribe | Store the decision, failure scar or successful outcome and queue cloud writes |

These are separate cooperating code modules, not six independent LLMs. With `OPENAI_API_KEY`, the Investigator calls a model through `agents/responder.py`; otherwise it uses a disclosed scenario policy. Operator-entered actions do not call a model. The other stages use explicit policies and evidence.

- **Proceed:** no matching scar in the available evidence; record a knowledge gap.
- **Caution:** pending/disputed evidence, a related or semantic-only match, an older service version, an incomplete configured search, or a high-impact action. Wait for review.
- **Blocked:** an equivalent action has a confirmed failure on the current version. The simulator allows a documented human override of an evidence block, but never an override of its destructive-action prohibition.

`Recycle workers` is equivalent to `Restart workers`; `Drain workers` is related and requires review. Retrieved text cannot grant permission to run tools. The local ledger controls authoritative review state and versions. The evidence score is an explainable rule score, not an estimated probability. Success on a newer version does not erase earlier evidence or automatically declare every scar healed.

## Connect your Meko project and datapacks

Run `start-live.cmd` on Windows or `make live` on macOS/Linux. The launcher prompts for a hidden API key and targets your existing **Scarline** datapack:

`3985023e-ece5-4b81-9833-2132c3b7ce24`

It verifies the datapack, finds your owned Scarline project, files an unfiled pack into it, then runs a marked synthetic write/search/readback check. It preserves existing project placement and stops if the target is ambiguous or belongs elsewhere. It does not modify `meko_default_datapack` or create unnecessary datapacks.

To use a key file outside the project and upload the bundled runbook:

```sh
python live.py --key-file "/path/to/your-key-file.txt" --with-runbook
```

The key stays in process memory. Do not put it into source code, a ZIP or a public repository. `--verify-only` exits before serving. If project tools are unavailable, file the pack through the Meko portal, set the variables below, run `python verify_meko.py`, then start the full server.

| Server environment variable | Purpose |
| --- | --- |
| `MEKO_API_KEY` | Account key with access to the selected packs |
| `MEKO_DATAPACK_ID` | Operational memory and decision conversations |
| `MEKO_RAW_DATAPACK_ID` | Optional separate pack for normalized incoming alert turns |
| `MEKO_KNOWLEDGE_DATAPACK_ID` | Optional runbook pack; defaults to the operational pack |
| `MEKO_AUDIT_DATAPACK_ID` | Optional artifact pack; defaults to the operational pack |
| `OPENAI_API_KEY` | Optional model proposals; separate from Meko credentials |
| `OPENAI_MODEL` | Optional Chat Completions-compatible model, default `gpt-4o-mini` |
| `DATABASE_URL` | Dedicated PostgreSQL database with table-creation permission |
| `SCARLINE_DB` | Local SQLite path, default `data/scarline.sqlite3` |

Environment variables must be set in your shell or hosting settings. The app does not load `.env` files automatically. Extra pack UUIDs are optional: use packs you actually own. The launcher can file configured Knowledge and Audit packs into the same project.

### How each Meko feature is used

| Meko feature | Scarline operation | How to verify |
| --- | --- | --- |
| Datapacks | Explicit routing for operations, intake, knowledge and audit | Launcher confirms project placement |
| Add conversations | Incident summary, stage plan and outcome | `verify_meko.py` reads the stored turn back |
| Memory adds | Failure, independent review and successful simulated remedy | Acknowledgement plus recall in the smoke check |
| Memory searches | Direct `memory_search` and cross-source `context_search` before a decision | Decision records source status and retrieved scar IDs |
| KB file storage | Upload `runbooks/scarline-operations.md` using Meko's presigned form | Indexing checkpoint and retrieval of its unique marker |
| KB searches | `knowledgebase_search` retrieves passages for the Investigator | Runbook citations appear in decision details |
| Workbench tokens | Ask questions about the indexed runbook in Meko WorkbenchLM | Use the portal's own Usage page |
| Token observations | `track_token_usage` for actual model-reported usage | Trace reports provider input/output totals |
| Artifacts | Save audit JSON with `artifact_put`, retrieve with `artifact_get` | Readback SHA-256 must match uploaded bytes |

Open **Meko connection → Upload and verify runbook** after configuring the server. This can take a few minutes. The upload checkpoint avoids resending identical completed content. **Save verified audit artifact** uploads a snapshot and verifies its bytes. Large exports remain available as a local JSON download.

For WorkbenchLM, open the Knowledge datapack link, choose its gear menu → Open Workbench, select the indexed document and use the supplied question. Workbench requires a signed-in portal session; it is not an API for the app's Responder. Scarline's external model accounting is not proof of Workbench consumption.

The dashboard's counters are calls observed by this installation, not a live account quota balance. The usage values supplied in chat are not copied into a fake live meter. One incident normally performs three searches plus conversation operations; only observed outcomes generate memories. The demo is bounded to limit incidental consumption.

Cloud writes are committed locally with their destination before delivery. A missing acknowledgement becomes **unverified** and is not blindly retried, because memory additions can duplicate. Inspect the target before reconciling an uncertain write. Seed data stays local. Private memories are shared by app roles using one Meko account; no claim is made of automatic cross-user memory promotion.

## Connect monitoring and telemetry

Sources are disabled until their secret is configured. Bodies are limited to 64 KiB. Grouped batches are validated atomically. Accepted alerts have stable trace IDs; duplicate delivery does not create another incident. Grafana/Alertmanager resolved updates stop queued work and prevent execution if resolution arrives during retrieval. Completed decisions remain immutable. The worker retries failed processing up to three times, then exposes a manual retry.

| Source | Endpoint | Authentication and payload |
| --- | --- | --- |
| Grafana | `POST /api/webhooks/grafana` | `SCARLINE_GRAFANA_SECRET`; HMAC-SHA256 in `X-Grafana-Alerting-Signature`; native grouped alerts |
| Alertmanager | `POST /api/webhooks/alertmanager` | `SCARLINE_ALERTMANAGER_SECRET`; Bearer authorization; native grouped alerts |
| Datadog | `POST /api/webhooks/datadog` | `SCARLINE_DATADOG_SECRET`; custom `X-Scarline-Token`; custom JSON below |
| PagerDuty | `POST /api/webhooks/pagerduty` | `SCARLINE_PAGERDUTY_SECRET`; v3 signature; `incident.triggered` only |
| Checkout proof | `POST /api/webhooks/checkout-demo` | `SCARLINE_CHECKOUT_DEMO_SECRET`; `X-Scarline-Token` |

For Grafana timestamp verification, set `SCARLINE_GRAFANA_TIMESTAMP_HEADER` to the header configured in Grafana. Scarline validates a five-minute window and signs `timestamp:body`. Service labels must match a tracked service. Alert deduplication uses fingerprint plus `startsAt`; firing again after recovery must have a new start time.

Example grouped body:

```json
{"alerts":[{"status":"firing","fingerprint":"payments-latency","startsAt":"2026-11-24T00:00:00Z","labels":{"service":"payments-api","alertname":"HighLatency"},"annotations":{"summary":"Checkout latency elevated"}}]}
```

Datadog custom payload:

```json
{"id":"$ID","service":"payments-api","symptom":"$EVENT_TITLE"}
```

Authenticated `POST /api/telemetry` accepts:

```json
{"id":"sample-unique-001","service":"payments-api","metrics":{"latency_ms":950,"replication_lag_s":12,"error_rate":0.08}}
```

Telemetry uses thresholds of 800 ms, 10 seconds and 5%. Two or more breached thresholds mark elevated risk and queue an incident when entering that state. Repeated samples while elevated do not flood the queue. Samples older than five minutes are stale. This is rule-based correlation of received measurements, not a trained predictive model or a claim that an incident will occur. No production monitoring account is connected by default.

`python demo_live_checkout.py` runs a separate loopback HTTP fault-injection proof. To send measured synthetic checkout failures to an existing Scarline instance, set matching checkout-demo secrets and use `python demo_live_checkout.py --port 8000`.

## Access and independent reviewers

Set `SCARLINE_OPERATOR_TOKEN` before exposing the full server. **Workspace access** establishes an HTTP-only, SameSite cookie. Read APIs, SSE and audit exports require access as well as mutations; webhook endpoints use their separate source secrets. Scripts can pass `Authorization: Bearer <access-token>`.

For named users, set `SCARLINE_USERS_JSON` to a JSON array of `{ "name": "...", "role": "...", "token": "..." }` objects using unique randomly generated secrets. This setting takes precedence over the shared token.

| Role | Permission |
| --- | --- |
| viewer | Read the dashboard, live feed and audit |
| operator | Submit incidents, telemetry, failures and simulator controls |
| reviewer | Operator actions plus scar review, approval and override |
| admin | Reviewer actions plus version updates and cloud uploads |

The server replaces submitted author/reviewer names with the authenticated identity. An author cannot review their own scar. In local no-token mode and shared-token mode, reviewer names remain self-reported. Named roles are a small access-control layer, not organization SSO. Use HTTPS on an external host and distinct source secrets.

## Benchmark and verification

```sh
python -m unittest discover -v
python benchmark.py --runs 10 > benchmark-results.json
```

The packaged report contains ten paired repetitions of 40 incidents per arm. Five cold-start services encounter repeated failing remedies, then upgrade after 75% of the workload. A simulated independent reviewer confirms observed failures. Both arms have the same successful fallback remedy. Arm order alternates between repetitions.

Each pair observed 25 repeated failures without Scarline and zero with it; all 40 final simulated health checks passed in both arms. Local latency is measured. Model tokens remain unmeasured. These deliberately constructed scenarios test policy mechanics; they do not estimate production savings or semantic-search quality.

`python benchmark.py --model --runs 1` explicitly enables paid/model-metered proposals, up to 80 requests for the default pair. Model variability can change outcomes. Do not claim token savings without inspecting measured counts and comparable successful outcomes.

Local verification covers lifecycle/version healing, independent reviews, grouped-alert atomicity and resolution, signatures, telemetry validation, stale data, queue retries, live activity, access controls, Meko contracts and readback checks. Meko tests use test doubles. In this editing workspace the real Meko endpoint timed out; live cloud connection, real model calls, hosted PostgreSQL and real monitoring accounts remain unverified. No account quota consumption is claimed.

The existing `submission` video, slides, research and checkout proof are retained from the earlier build. They are historical presentation assets, not recordings of this upgrade.

## Prepare for Render

1. Put the contents of the extracted Scarline folder at your repository root, with `render.yaml` next to `app.py`.
2. Create a Render Blueprint from that repository. Provide a dedicated persistent `DATABASE_URL` and the Meko key as server secrets. The Blueprint generates a workspace access token and sets the known operational datapack ID.
3. Run the Meko acceptance check locally first, then enter the Render-generated token through **Workspace access** after deployment. Add optional pack UUIDs, model credentials and monitoring secrets in Render settings.
4. Verify a new incident, SSE updates, webhook delivery and Meko readback on the deployed URL. Keep one instance and one Uvicorn process.

The Blueprint remains on the Free demo plan. Free services spin down after 15 idle minutes and lose local filesystem changes; PostgreSQL is required for durable data. Select an appropriate paid instance for continuous monitoring. No hosting purchase or deployment was performed here.

Primary references: [Meko tools](https://docs.mekodata.ai/reference/mcp-server/), [Meko projects](https://docs.mekodata.ai/use-meko/projects/), [Render Blueprint](https://render.com/docs/blueprint-spec), [Render Free limitations](https://render.com/docs/free), [Grafana webhook](https://grafana.com/docs/grafana/latest/alerting/configure-notifications/manage-contact-points/integrations/webhook-notifier/), [Alertmanager configuration](https://prometheus.io/docs/alerting/latest/configuration/).







trail link :https://scarline.onrender.com
