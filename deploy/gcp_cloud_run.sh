#!/usr/bin/env bash
# Deploy Fanvaaz to Google Cloud: Cloud Run (FastAPI) + Cloud SQL for PostgreSQL (pgvector)
# + Secret Manager + Cloud Scheduler + a budget alert.
#
#   gcloud auth login
#   gcloud config set project <your-project-id>
#   ADMIN_EMAIL=you@example.com bash deploy/gcp_cloud_run.sh
#
# Secrets are typed at the prompts (or passed as env vars for automation) and go straight
# into Secret Manager: never into a file, the image, or shell history. Safe to re-run:
# existing resources are reused; a secret gets a new version only if you type one.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-asia-south1}"                       # Mumbai
SERVICE="${SERVICE:-fanvaaz}"
SQL_INSTANCE="${SQL_INSTANCE:-fanvaaz-pg}"
SQL_TIER="${SQL_TIER:-db-f1-micro}"                   # smallest shared-core tier; check pricing first
DB_NAME="${DB_NAME:-fanvaaz}"
DB_USER="${DB_USER:-fanvaaz}"
SA_NAME="fanvaaz-run"
ADMIN_EMAIL="${ADMIN_EMAIL:-}"
PROFILE="${PROFILE:-smartphones-india}"               # what to track until an admin changes it
DAILY_CRON="${DAILY_CRON:-25 6 * * *}"                # 06:25 IST
BUDGET_INR="${BUDGET_INR:-2500}"
LLM_BASE_URL="${LLM_BASE_URL:-https://generativelanguage.googleapis.com/v1beta/openai}"
LLM_MODEL="${LLM_MODEL:-gemini-flash-latest}"         # verify: GET $LLM_BASE_URL/models
EMBED_MODEL="${EMBED_MODEL:-gemini-embedding-001}"
VERTEX_RERANK="${VERTEX_RERANK:-false}"               # optional Vertex AI Ranking API stage

[[ -n "$PROJECT_ID" ]] || { echo "Set a project: gcloud config set project <id>" >&2; exit 1; }
[[ -n "$ADMIN_EMAIL" ]] || read -rp "Email for the first admin account: " ADMIN_EMAIL
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
CONN="${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"
gcloud config set project "$PROJECT_ID" >/dev/null
gcloud config set run/region "$REGION" >/dev/null
echo "Project $PROJECT_ID, region $REGION, admin $ADMIN_EMAIL"
gcloud projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null ||
  { echo "The signed-in account cannot open $PROJECT_ID (gcloud auth list)." >&2; exit 1; }

echo "== 1. Enable the APIs Fanvaaz uses"
APIS=(youtube.googleapis.com run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
  secretmanager.googleapis.com cloudscheduler.googleapis.com sqladmin.googleapis.com iam.googleapis.com
  billingbudgets.googleapis.com generativelanguage.googleapis.com logging.googleapis.com monitoring.googleapis.com)
[[ "$VERTEX_RERANK" == "true" ]] && APIS+=(discoveryengine.googleapis.com)
gcloud services enable "${APIS[@]}"

echo "== 2. Runtime service account (least privilege)"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 ||
  gcloud iam service-accounts create "$SA_NAME" --display-name="Fanvaaz Cloud Run"
for role in roles/cloudsql.client roles/logging.logWriter roles/monitoring.metricWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$SA" --role="$role" \
    --condition=None >/dev/null
done
[[ "$VERTEX_RERANK" == "true" ]] && gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA" --role=roles/discoveryengine.viewer --condition=None >/dev/null

echo "== 3. Secrets"
grant() { gcloud secrets add-iam-policy-binding "$1" --member="serviceAccount:$SA" \
  --role=roles/secretmanager.secretAccessor >/dev/null; }
put_secret() {  # name, prompt, [env var holding the value]
  local name="$1" prompt="$2" value="${!3:-}"
  gcloud secrets describe "$name" >/dev/null 2>&1 || gcloud secrets create "$name" --replication-policy=automatic
  [[ -z "$value" ]] && { read -rsp "$prompt (Enter to keep the current value): " value; echo; }
  if [[ "$name" == "fanvaaz-admin-password" && -n "$value" && ${#value} -lt 12 ]]; then
    echo "  The admin password must be at least 12 characters (you typed ${#value}). Run the script again." >&2
    exit 1
  fi
  if [[ -n "$value" ]]; then
    printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- >/dev/null
  elif ! gcloud secrets versions list "$name" --filter=state=enabled --format='value(name)' | grep -q .; then
    echo "  $name has no value yet; type one." >&2; exit 1
  fi
  grant "$name"
}
gen_secret() {  # name: random value created once, never shown
  if ! gcloud secrets describe "$1" >/dev/null 2>&1; then
    gcloud secrets create "$1" --replication-policy=automatic
    python3 -c "import secrets; print(secrets.token_urlsafe(32), end='')" | gcloud secrets versions add "$1" --data-file=- >/dev/null
  fi
  grant "$1"
}
put_secret fanvaaz-youtube-api-key "YouTube Data API v3 key" YOUTUBE_API_KEY
put_secret fanvaaz-admin-password  "Password for $ADMIN_EMAIL (12+ characters)" ADMIN_PASSWORD
gen_secret fanvaaz-api-token     # admin service token (Cloud Scheduler, CI, admin MCP scope)
gen_secret fanvaaz-mcp-token     # read-only MCP token for Claude Desktop / Claude Code

LLM_ENV="" LLM_SECRET=""
GEMINI="${GEMINI_API_KEY:-}"
[[ -z "$GEMINI" ]] && { read -rsp "Gemini API key from aistudio.google.com/apikey (Enter to skip or keep): " GEMINI; echo; }
if [[ -n "$GEMINI" ]] || gcloud secrets describe fanvaaz-llm-api-key >/dev/null 2>&1; then
  gcloud secrets describe fanvaaz-llm-api-key >/dev/null 2>&1 ||
    gcloud secrets create fanvaaz-llm-api-key --replication-policy=automatic
  [[ -n "$GEMINI" ]] && printf '%s' "$GEMINI" | gcloud secrets versions add fanvaaz-llm-api-key --data-file=- >/dev/null
  grant fanvaaz-llm-api-key
  LLM_ENV="FANVAAZ_LLM_BASE_URL=$LLM_BASE_URL,FANVAAZ_LLM_MODEL=$LLM_MODEL,FANVAAZ_EMBED_MODEL=$EMBED_MODEL,"
  LLM_SECRET=",FANVAAZ_LLM_API_KEY=fanvaaz-llm-api-key:latest"
fi
unset GEMINI

echo "== 4. Cloud SQL for PostgreSQL 16 (pgvector) — billed while it runs"
if ! gcloud sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1; then
  gcloud sql instances create "$SQL_INSTANCE" --database-version=POSTGRES_16 --edition=ENTERPRISE \
    --tier="$SQL_TIER" --region="$REGION" --storage-type=SSD --storage-size=10 --availability-type=zonal \
    --backup-start-time=21:30 --ssl-mode=ENCRYPTED_ONLY
fi
gcloud sql databases describe "$DB_NAME" --instance="$SQL_INSTANCE" >/dev/null 2>&1 ||
  gcloud sql databases create "$DB_NAME" --instance="$SQL_INSTANCE"
if ! gcloud secrets describe fanvaaz-database-url >/dev/null 2>&1; then
  DB_PASS="$(python3 -c "import secrets; print(secrets.token_urlsafe(24), end='')")"
  gcloud sql users create "$DB_USER" --instance="$SQL_INSTANCE" --password="$DB_PASS" >/dev/null 2>&1 ||
    gcloud sql users set-password "$DB_USER" --instance="$SQL_INSTANCE" --password="$DB_PASS" >/dev/null
  gcloud secrets create fanvaaz-database-url --replication-policy=automatic
  # Cloud Run's built-in Cloud SQL connector exposes the instance as a Unix socket.
  printf '%s' "postgresql://${DB_USER}:${DB_PASS}@/${DB_NAME}?host=/cloudsql/${CONN}" |
    gcloud secrets versions add fanvaaz-database-url --data-file=- >/dev/null
  unset DB_PASS
fi
grant fanvaaz-database-url
# Migrations (including CREATE EXTENSION vector) run when the app starts.

echo "== 5. Build and deploy the FastAPI service"
RERANK_ENV=""
[[ "$VERTEX_RERANK" == "true" ]] && RERANK_ENV="FANVAAZ_VERTEX_RERANK=true,FANVAAZ_GCP_PROJECT=$PROJECT_ID,"
gcloud run deploy "$SERVICE" \
  --source . \
  --service-account="$SA" \
  --no-invoker-iam-check \
  --min-instances=0 --max-instances=3 \
  --cpu=1 --memory=1Gi --timeout=1800 --concurrency=40 --no-cpu-throttling \
  --execution-environment=gen2 \
  --add-cloudsql-instances="$CONN" \
  --set-env-vars="${LLM_ENV}${RERANK_ENV}FANVAAZ_PROFILE=$PROFILE,FANVAAZ_COOKIE_SECURE=true,FANVAAZ_ADMIN_EMAIL=$ADMIN_EMAIL,FANVAAZ_TIMEZONE=Asia/Kolkata,FANVAAZ_SAVE_RAW=false,FANVAAZ_LOG_JSON=true,FANVAAZ_PUBLIC_DOCS=false,FANVAAZ_CHECKPOINT_PATH=/tmp/checkpoints.db" \
  --set-secrets="FANVAAZ_DATABASE_URL=fanvaaz-database-url:latest,FANVAAZ_YOUTUBE_API_KEY=fanvaaz-youtube-api-key:latest,FANVAAZ_ADMIN_PASSWORD=fanvaaz-admin-password:latest,FANVAAZ_API_TOKEN=fanvaaz-api-token:latest,FANVAAZ_MCP_TOKEN=fanvaaz-mcp-token:latest$LLM_SECRET"

URL="$(gcloud run services describe "$SERVICE" --format='value(status.url)')"
echo "Service URL: $URL"
for i in $(seq 1 20); do curl -fsS "$URL/ready" && echo && break; sleep 3; done

echo "== 6. Daily live collection with Cloud Scheduler"
TOKEN="$(gcloud secrets versions access latest --secret=fanvaaz-api-token)"
JOB_ARGS=(--location="$REGION" --schedule="$DAILY_CRON" --time-zone=Asia/Kolkata
  --uri="$URL/api/v1/runs" --http-method=POST --attempt-deadline=1800s
  --headers="Authorization=Bearer $TOKEN,Content-Type=application/json"
  --message-body='{"source":"youtube"}')
if gcloud scheduler jobs describe fanvaaz-daily --location="$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http fanvaaz-daily "${JOB_ARGS[@]}" >/dev/null
else
  gcloud scheduler jobs create http fanvaaz-daily "${JOB_ARGS[@]}" >/dev/null
fi
unset TOKEN

echo "== 7. Alerts: failed runs (log-based metric) and a monthly budget"
gcloud logging metrics describe fanvaaz-run-failed >/dev/null 2>&1 ||
  gcloud logging metrics create fanvaaz-run-failed \
    --description="Fanvaaz collection runs that failed" \
    --log-filter='resource.type="cloud_run_revision" AND jsonPayload.logger="fanvaaz.api" AND jsonPayload.message:"run job"'
BILLING_ACCOUNT="${BILLING_ACCOUNT:-$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingAccountName)' | sed 's#billingAccounts/##')}"
if [[ -n "$BILLING_ACCOUNT" ]]; then
  gcloud billing budgets list --billing-account="$BILLING_ACCOUNT" --format='value(displayName)' |
    grep -qx fanvaaz-monthly ||
    gcloud billing budgets create --billing-account="$BILLING_ACCOUNT" --display-name=fanvaaz-monthly \
      --budget-amount="${BUDGET_INR}INR" --filter-projects="projects/$PROJECT_ID" \
      --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
fi

echo
echo "Done. Sign in at $URL/login as $ADMIN_EMAIL."
echo "First live collection now:  gcloud scheduler jobs run fanvaaz-daily --location=$REGION"
echo "MCP (read-only) token:      gcloud secrets versions access latest --secret=fanvaaz-mcp-token"
