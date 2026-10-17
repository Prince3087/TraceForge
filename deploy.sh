#!/usr/bin/env bash
# Deploy TraceForge to Google Cloud Run via Artifact Registry.
#
#   PROJECT_ID=my-proj ./deploy.sh
#
# Optional env:  REGION (us-central1)  GEMINI_MODEL (gemini-3.8-flash)  LEDGER_BUCKET (durable audit ledger bucket)
#                SIGNING_SECRET (traceforge-signing-key)  TAG (git short sha, else timestamp)
# Idempotent: safe to re-run. Every step checks before it creates.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
[ -n "${PROJECT_ID}" ] || { echo "ERROR: set PROJECT_ID (or 'gcloud config set project <id>')" >&2; exit 1; }
REGION="${REGION:-us-central1}"
SERVICE="traceforge-engine"
REPO="traceforge-repo"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.8-flash}"
SIGNING_SECRET="${SIGNING_SECRET:-traceforge-signing-key}"
LEDGER_BUCKET="${LEDGER_BUCKET:-}"
TAG="${TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/traceforge:${TAG}"

say() { printf '\n==> %s\n' "$*"; }

# 1. Required APIs (enable only what is missing) ---------------------------------------------------
say "Checking required APIs"
REQUIRED_APIS=(run.googleapis.com aiplatform.googleapis.com artifactregistry.googleapis.com
               cloudbuild.googleapis.com secretmanager.googleapis.com storage.googleapis.com)
ENABLED="$(gcloud services list --enabled --project "$PROJECT_ID" --format='value(config.name)')"
MISSING=()
for api in "${REQUIRED_APIS[@]}"; do
  grep -qx "$api" <<<"$ENABLED" || MISSING+=("$api")
done
if [ "${#MISSING[@]}" -gt 0 ]; then
  echo "Enabling: ${MISSING[*]}"
  gcloud services enable "${MISSING[@]}" --project "$PROJECT_ID"
else
  echo "All required APIs already enabled."
fi

# 2. Artifact Registry repository ---------------------------------------------------------------------
say "Ensuring Artifact Registry repo ${REPO} (${REGION})"
if ! gcloud artifacts repositories describe "$REPO" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$REPO" --repository-format=docker --location "$REGION" \
    --description="TraceForge images" --project "$PROJECT_ID"
fi

# 3. Identities + secrets ------------------------------------------------------------------------------
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
RUNTIME_SA="${RUNTIME_SA:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"

say "Ensuring HMAC signing secret ${SIGNING_SECRET}"
if ! gcloud secrets describe "$SIGNING_SECRET" --project "$PROJECT_ID" >/dev/null 2>&1; then
  SIGNING_KEY_VALUE="$(openssl rand -hex 32)"   # generate first: no producer|consumer pipe race under pipefail
  printf '%s' "$SIGNING_KEY_VALUE" | gcloud secrets create "$SIGNING_SECRET" --data-file=- --replication-policy=automatic --project "$PROJECT_ID"
  unset SIGNING_KEY_VALUE
fi
gcloud secrets add-iam-policy-binding "$SIGNING_SECRET" --project "$PROJECT_ID" \
  --member "serviceAccount:${RUNTIME_SA}" --role roles/secretmanager.secretAccessor >/dev/null
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:${RUNTIME_SA}" --role roles/aiplatform.user --condition=None >/dev/null

ENV_VARS="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},GEMINI_MODEL=${GEMINI_MODEL}"
if [ -n "$LEDGER_BUCKET" ]; then
  say "Ensuring durable ledger bucket gs://${LEDGER_BUCKET}"
  if ! gcloud storage buckets describe "gs://${LEDGER_BUCKET}" --project "$PROJECT_ID" >/dev/null 2>&1; then
    gcloud storage buckets create "gs://${LEDGER_BUCKET}" --location "$REGION" --uniform-bucket-level-access \
      --project "$PROJECT_ID"
  fi
  # Create + read only: the runtime identity can append ledger objects but never overwrite or delete them.
  for role in roles/storage.objectCreator roles/storage.objectViewer; do
    gcloud storage buckets add-iam-policy-binding "gs://${LEDGER_BUCKET}" \
      --member "serviceAccount:${RUNTIME_SA}" --role "$role" >/dev/null
  done
  ENV_VARS="${ENV_VARS},LEDGER_BUCKET_NAME=${LEDGER_BUCKET}"
else
  echo "NOTE: LEDGER_BUCKET not set - the audit ledger will be LOCAL ONLY and is lost on container restart."
fi

# 4. Build + push with Cloud Build -> Artifact Registry --------------------------------------------------
say "Building ${IMAGE}"
gcloud builds submit --project "$PROJECT_ID" --config cloudbuild.yaml \
  --substitutions "_REGION=${REGION},SHORT_SHA=${TAG}" .

# 5. Deploy ------------------------------------------------------------------------------------------
say "Deploying ${SERVICE} to Cloud Run"
gcloud run deploy "$SERVICE" --image "$IMAGE" --project "$PROJECT_ID" --region "$REGION" \
  --platform managed --allow-unauthenticated --port 8080 --memory 1Gi --cpu 1 \
  --min-instances 0 --max-instances 1 --timeout 300 --service-account "$RUNTIME_SA" \
  --set-env-vars "$ENV_VARS" --set-secrets "TRACEFORGE_SIGNING_KEY=${SIGNING_SECRET}:latest"

say "Service URL"
gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format 'value(status.url)'
