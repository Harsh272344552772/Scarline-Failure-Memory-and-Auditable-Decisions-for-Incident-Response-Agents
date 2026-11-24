Verification marker: SCARLINE-RUNBOOK-20261124. Scarline payments-api v2.14 worker restart connection pool runbook.

# Scarline operational runbook

This document describes Scarline's synthetic five-service environment. Use measured telemetry and the current failure ledger when reviewing any proposed action.

## payments-api
The v2.14 worker-restart scenario reconnects workers together and exhausts the database connection pool. Inspect pool usage, collect diagnostics and use a supervised canary before a restart. A confirmed failure for the same action and version blocks the restart. Deployment to a new version requires a supervised retest.

## catalog-cache
A full cache flush on v3.2 causes cold reads to overload the catalog database. Inspect hit ratio and database latency. A supervised warm-up or bounded diagnostic is preferable to a full flush.

## mail-worker
Scaling v1.8 workers increases pressure on the mail provider and triggers its rate limit. Inspect the provider responses and queue age. Check the downstream quota before adding concurrency.

## search-index
Rebuilding the v4.0 index competes with queued writes. Inspect indexing lag and ongoing writes before approving a rebuild.

## orders-db
Promoting the v6.1 replica can lose acknowledged writes when it is behind. Inspect replication lag and consistency before any supervised promotion.

## Review and healing
Every new scar begins pending. A different reviewer confirms or disputes its evidence. Disputed or old-version scars call for supervision. Earlier decision snapshots retain their original verdict. Successful simulated retests are recorded as learnings and retain their service version.

## Signal correlation
Scarline reports an elevated state when at least two of three observed metrics exceed these demonstration thresholds: latency 800 milliseconds, replication lag 10 seconds, error rate 0.05. It also reports which metrics increased from the preceding sample. These thresholds are configuration for the demo and have no measured predictive accuracy.

## WorkbenchLM review
Select the Scarline operational datapack in Meko's WorkbenchLM after this file finishes indexing. Ask it to find the latest worker restart failure, cite its saved conversation, explain the current-version risk and suggest a supervised retest for a newer version. Compare the retrieved facts with Scarline's immutable audit snapshot before making an approval.
