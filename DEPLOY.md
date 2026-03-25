# Vigilist Deployment Guide

## Prerequisites

- `gcloud` CLI authenticated (`gcloud auth login`)
- `firebase-tools` installed (`npm install -g firebase-tools`)
- Firebase CLI authenticated (`firebase login`)
- Neon account at https://neon.tech (free tier)

## 1. Set up Neon Database (one-time)

1. Go to https://console.neon.tech
2. Create a new project (e.g., "vigilist")
3. Copy the connection string — it looks like:
   ```
   postgresql://vigilist_owner:PASSWORD@ep-something.us-east-2.aws.neon.tech/vigilist?sslmode=require
   ```
4. For asyncpg, change `postgresql://` to `postgresql+asyncpg://` and add `?ssl=require` if not present:
   ```
   postgresql+asyncpg://vigilist_owner:PASSWORD@ep-something.us-east-2.aws.neon.tech/vigilist?ssl=require
   ```

## 2. Run Alembic Migrations Against Neon

```powershell
cd backend
.\venv\Scripts\Activate.ps1
$env:VIGILIST_DATABASE_URL="postgresql+asyncpg://USER:PASS@HOST/DB?ssl=require"
alembic upgrade head
```

## 3. Deploy Backend to Cloud Run

```powershell
# Set project
gcloud config set project ediscover

# Enable required APIs (one-time)
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com

# Deploy (builds container in the cloud)
gcloud run deploy vigilist-api `
  --source backend `
  --region us-central1 `
  --allow-unauthenticated `
  --set-env-vars "VIGILIST_DATABASE_URL=postgresql+asyncpg://USER:PASS@HOST/DB?ssl=require" `
  --set-env-vars "VIGILIST_FIREBASE_PROJECT_ID=ediscover" `
  --set-env-vars "VIGILIST_FIREBASE_STORAGE_BUCKET=ediscover.firebasestorage.app" `
  --set-env-vars "VIGILIST_ANTHROPIC_API_KEY=your-key-here" `
  --set-env-vars "VIGILIST_CORS_ORIGINS=[\"https://ediscover.web.app\",\"https://ediscover.firebaseapp.com\"]" `
  --memory 1Gi `
  --cpu 1 `
  --min-instances 0 `
  --max-instances 2
```

Note: Cloud Run uses the default compute service account which already has Firebase Admin access. No separate `GOOGLE_APPLICATION_CREDENTIALS` needed.

After deploy, note the service URL (e.g., `https://vigilist-api-XXXXX-uc.a.run.app`).

## 4. Build and Deploy Frontend

```powershell
# Build the frontend
cd frontend
npm run build

# Deploy to Firebase Hosting
cd ..
firebase deploy --only hosting
```

The `firebase.json` rewrites `/api/**` requests to the Cloud Run `vigilist-api` service automatically.

Your app is live at: **https://ediscover.web.app**

## 5. Verify

1. Open https://ediscover.web.app
2. Register or sign in with Google
3. You should see the Welcome page (no productions yet)

## Updating

### Backend changes
```powershell
gcloud run deploy vigilist-api --source backend --region us-central1
```

### Frontend changes
```powershell
cd frontend && npm run build && cd .. && firebase deploy --only hosting
```

### Database migrations
```powershell
cd backend
.\venv\Scripts\Activate.ps1
$env:VIGILIST_DATABASE_URL="your-neon-connection-string"
alembic upgrade head
```
