// Railway Infrastructure as Code for the hourly snapshot service.
//
// This file is a NAMED PARTIAL: it owns only the resources declared here and leaves
// every other service in the project alone (Railway's ownership rules). It is not read
// at deploy time; apply it from a terminal:
//
//   npm install            # installs the `railway` SDK the CLI evaluates this file with
//   railway login && railway link            # pick the project + environment
//   railway config plan                      # must show only this service + its volume
//   railway config apply
//
// Settings the IaC DSL does not cover (as of the docs read on 2026-10-27) stay in the
// dashboard, service -> Settings:
//   - Cron Schedule: 7 * * * *          (runs hourly; the job exits when done)
//   - Restart policy: Never             (a failed run should not loop)
// See docs/deploy.md.

import { defineRailway, preserve, project, service, volume } from "railway/iac";

export const partial = "trading-bot-snapshot";

// Set these to your Railway project and preferred names.
const PROJECT = "CHANGE_ME";
const SERVICE = "trading-bot-snapshot";
// Region identifiers from docs.railway.com/deployments/regions; pick one near your RPC.
const REGION = "us-east4-eqdc4a"; // or "europe-west4-drams3a"

export default defineRailway(() => {
  // Optional: keeps raw JSON and snapshots.csv on disk. With DATABASE_URL set the
  // same data also lands in Postgres, which is the convenient place to read it from.
  const data = volume(`${SERVICE}-data`, { region: REGION, sizeMB: 1024 });

  const snapshot = service(SERVICE, {
    // No `source` here: attach the GitHub repo (or `railway up`) in the dashboard.
    env: {
      RAILWAY_DOCKERFILE_PATH: "deploy/railway/Dockerfile.snapshot",
      // Railway mounts volumes owned by root; this is its documented fix so the job
      // can write /data (the Dockerfile also runs as root).
      RAILWAY_RUN_UID: "0",
      BOT_DATA_DIR: "/data",
      // Reference to an existing Postgres service, set once as ${{Postgres.DATABASE_URL}}.
      // preserve() keeps that value; declaring postgres("...") here would claim
      // ownership of a database this partial must not own.
      DATABASE_URL: preserve(),
    },
    volumeMounts: { "/data": data },
  });

  return project(PROJECT, { resources: [snapshot, data] });
});
