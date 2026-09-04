from fastapi import APIRouter

router = APIRouter(prefix="/liberclaw", tags=["Liberclaw"])

from src.routes.liberclaw.liberclaw import (  # noqa
    get_or_create_api_key,
    update_tier,
    get_user,
    issue_invoice,
    list_invoices,
    download_invoice_pdf,
    get_billing_details,
    update_billing_details,
    delete_billing_details,
    liberclaw_checkout,
    liberclaw_upgrade,
    liberclaw_cancel,
    liberclaw_resume,
    liberclaw_downgrade,
    liberclaw_start_trial,
    liberclaw_trial_eligibility,
    liberclaw_subscription_state,
    liberclaw_admin_grant_trial,
    liberclaw_admin_override_tier,
    liberclaw_admin_force_cancel,
    liberclaw_admin_extend,
)
