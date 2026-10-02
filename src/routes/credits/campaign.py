from fastapi import Depends

from src.interfaces.campaigns import (
    CampaignAttachAlephRequest,
    CampaignClaimRequest,
    CampaignClaimResponse,
    CampaignPublicResponse,
)
from src.models.user import User
from src.routes.credits import router
from src.services import campaign as campaign_service
from src.services.auth import get_current_user
from src.utils.cron import scheduler
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


@router.get(  # type: ignore
    "/campaigns/{code}",
    description="Public: what a claim code offers and whether it accepts claims right now",
)
async def get_campaign(code: str) -> CampaignPublicResponse:
    return await campaign_service.get_public_campaign(code)


@router.get("/campaigns/{code}/claim", description="The signed-in user's claim on this code, 404 if none")  # type: ignore
async def get_campaign_claim(code: str, user: User = Depends(get_current_user)) -> CampaignClaimResponse:
    return await campaign_service.get_claim(code, user)


@router.post(  # type: ignore
    "/campaigns/{code}/claim",
    description="Claim this code's credits: LibertAI right away, Aleph Cloud too if a wallet is given",
)
async def claim_campaign(
    code: str, request: CampaignClaimRequest, user: User = Depends(get_current_user)
) -> CampaignClaimResponse:
    return await campaign_service.claim_campaign(code, user, request.aleph_address)


@router.post(  # type: ignore
    "/campaigns/{code}/claim/aleph",
    description="Add the Aleph Cloud wallet to an existing claim that has none yet",
)
async def attach_campaign_aleph_address(
    code: str, request: CampaignAttachAlephRequest, user: User = Depends(get_current_user)
) -> CampaignClaimResponse:
    return await campaign_service.attach_aleph_address(code, user, request.aleph_address)


@scheduler.scheduled_job("interval", minutes=2)
async def retry_pending_aleph_grants() -> None:
    try:
        tried = await campaign_service.process_pending_aleph_grants()
        if tried:
            logger.info(f"Retried {tried} pending Aleph campaign grant(s)")
    except Exception as e:
        logger.error(f"Error retrying Aleph campaign grants: {e!s}", exc_info=True)
