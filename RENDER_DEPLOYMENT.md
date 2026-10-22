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
# Key handed to the standalone simulator UI (used when the simulator is run on
# its own, outside a MajiScope session). Set it to the same value as API_KEYS
# so the operator types the same key everywhere.
# Leave it unset to keep the key server-side only.
STANDALONE_API_KEY=<same value as API_KEYS>
GPKG_DIR=/data/gpkg
DATABASE_URL=sqlite+aiosqlite:////data/db/epanet_service.db
MAJISCOPE_BACKEND_URL=https://latest-majiscope-backend.onrender.com
MAJISCOPE_RETURN_URL=https://latest-majiscope.onrender.com/dashboard
MAJISCOPE_LAUNCH_SECRET=<same value as Backend HYDRAULIC_MODEL_LAUNCH_SECRET>
MAJISCOPE_CALLBACK_SECRET=<same value as Backend HYDRAULIC_MODEL_CALLBACK_SECRET>
DEFAULT_DURATION_HRS=24
DEFAULT_TIMESTEP_MIN=60
DEFAULT_BASE_DEMAND=0.001
MIN_PRESSURE_M=7.0
MAX_VELOCITY_MS=3.0
DEBUG=false
```

`MAJISCOPE_BACKEND_URL` must point at the backend that actually owns the MajiScope
data. It is used to deliver completed simulation snapshots
(`POST /api/hydraulic-model/snapshots`) and to confirm session cleanup
(`POST /api/hydraulic-model/sessions/{id}/cleanup`). A stale host here means
simulations run but their results never appear in MajiScope.

## MajiScope Backend Variables

For service-to-service calls, use the hydraulic model service's **Internal Address**
from Render Dashboard → hydraulic model service → Connect → Internal, for example
`http://<internal-hostname>:10000`. This requires both services to be in the same
Render workspace and region. It keeps validator traffic on Render's private network
instead of routing it through the public service URL.

Set the service root (without an `/api` suffix) in the MajiScope backend Render service.
`HYDRAULIC_MODEL_BASE_URL` accepts a comma-separated list that is tried in order, so the
private address is preferred and the public URL remains a fallback:

```env
HYDRAULIC_MODEL_BASE_URL=http://<internal-hostname>:10000,https://epanet-model.onrender.com
# Browser-facing URL. The launch link the user opens is built from this value, so it
# must stay on the public URL even when the backend calls the service privately.
HYDRAULIC_MODEL_PUBLIC_URL=https://epanet-model.onrender.com
```

If private networking is unavailable, use the public URL instead:
`https://epanet-model.onrender.com`. Public requests are rate limited by the platform
edge (HTTP 429 / 502 are returned before the application ever sees the request), so a
paid instance of the hydraulic service is strongly recommended — free instances are
stopped after 15 minutes idle and have to boot again on the next request.

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
8. In the MajiScope backend Render service, set `HYDRAULIC_MODEL_BASE_URL` to the
   hydraulic service's Internal Address first and the public URL second, set
   `HYDRAULIC_MODEL_PUBLIC_URL` to the public URL, and keep the API key configured.
9. Redeploy the MajiScope backend.
10. Open MajiScope and test `/dashboard/hydraulic-model`.

The service stores temporary GPKG files and its SQLite data under `/data`, so they survive Render restarts.
