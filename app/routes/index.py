from fastapi import APIRouter

from app.routes.compliance_routes import router as compliance_routes
from app.routes.dashboard_routes import router as dashboard_routes
from app.routes.health_routes import router as health_routes
from app.routes.intake_routes import router as intake_routes
from app.routes.job_routes import router as job_routes
from app.routes.lead_routes import router as lead_routes
from app.routes.observability_routes import router as observability_routes

console_router = APIRouter()
for router in (
    health_routes,
    dashboard_routes,
    intake_routes,
    job_routes,
    lead_routes,
    observability_routes,
    compliance_routes,
):
    console_router.include_router(router)
