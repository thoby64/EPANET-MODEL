# MajiScope Hydraulic Model Render Deployment

This service is deployed as a Docker web service. It must be able to receive launch requests from the MajiScope backend and send completed simulation snapshots back to MajiScope.

## Required Render Service

- Service type: Web Service
- Runtime: Docker
- Health check path: `/health`
- Service name: `epanet-model`
- Public URL: `https://epanet-model.onrender.com`
- Persistent disk:
  - Mount path: `/data`
  - Size: `1 GB` or larger

## Required Environment Variables

Set these in Render for the hydraulic model service:

```env
API_KEYS=<same value as Backend HYDRAULIC_MODEL_API_KEY>
GPKG_DIR=/data/gpkg
DATABASE_URL=sqlite+aiosqlite:////data/db/epanet_service.db
MAJISCOPE_BACKEND_URL=https://full-nfjr.onrender.com
MAJISCOPE_RETURN_URL=https://majiscope-2wzv.onrender.com/dashboard
MAJISCOPE_LAUNCH_SECRET=<same value as Backend HYDRAULIC_MODEL_LAUNCH_SECRET>
MAJISCOPE_CALLBACK_SECRET=<same value as Backend HYDRAULIC_MODEL_CALLBACK_SECRET>
DEFAULT_DURATION_HRS=24
DEFAULT_TIMESTEP_MIN=60
DEFAULT_BASE_DEMAND=0.001
MIN_PRESSURE_M=7.0
MAX_VELOCITY_MS=3.0
DEBUG=false
```

## MajiScope Backend Variables

After the hydraulic model Render URL is live, set this in the MajiScope backend Render service:

```env
HYDRAULIC_MODEL_BASE_URL=https://epanet-model.onrender.com
```

The following backend variables must match the hydraulic model service:

```env
HYDRAULIC_MODEL_API_KEY=<same as API_KEYS>
HYDRAULIC_MODEL_LAUNCH_SECRET=<same as MAJISCOPE_LAUNCH_SECRET>
HYDRAULIC_MODEL_CALLBACK_SECRET=<same as MAJISCOPE_CALLBACK_SECRET>
```

## Deployment Steps

1. Push this hydraulic model repository to GitHub.
2. In Render, create a new Web Service from the repository.
3. Choose Docker as the runtime.
4. Add the persistent disk mounted at `/data`.
5. Add the environment variables above.
6. Deploy and verify `https://<service-url>/health`.
7. Update the MajiScope backend `HYDRAULIC_MODEL_BASE_URL` to the service URL.
8. Redeploy the MajiScope backend.
9. Open MajiScope and test `/dashboard/hydraulic-model`.

The service stores temporary GPKG files and its SQLite data under `/data`, so they survive Render restarts.
