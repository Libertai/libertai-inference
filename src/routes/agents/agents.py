import uuid
from datetime import datetime
from http import HTTPStatus

from aleph.sdk import AuthenticatedAlephHttpClient
from aleph.sdk.chains.ethereum import ETHAccount
from aleph.sdk.conf import settings
from aleph_message.models import Chain, Payment, PaymentType, StoreMessage
from aleph_message.models.execution.environment import HypervisorType
from fastapi import HTTPException, Depends

from src.config import config
from src.interfaces.agent import (
    CreateAgentRequest,
    AgentResponse,
    GetAgentResponse,
    GetAgentSecretResponse,
    ResubscribeAgentResponse,
    ResubscribeAgentRequest,
)
from src.models.agent import Agent
from src.models.base import SessionLocal
from src.models.subscription import SubscriptionStatus
from src.routes.agents import router
from src.services.auth import get_current_address
from src.services.credit import CreditService
from src.services.subscription import SubscriptionService
from src.utils.aleph import fetch_instance_ip
from src.utils.cron import scheduler
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


AGENT_MONTHLY_COST = 10  # Monthly cost in credits


@router.post("/", description="Create a new agent", response_model=AgentResponse)
async def create_agent(
    body: CreateAgentRequest,
    user_address: str = Depends(get_current_address),
) -> AgentResponse:
    agent_id = uuid.uuid4()

    # Create Aleph instance
    rootfs = settings.UBUNTU_22_QEMU_ROOTFS_ID
    aleph_account = ETHAccount(config.ALEPH_SENDER_SK)

    user_balance = CreditService.get_balance(user_address)
    if user_balance < AGENT_MONTHLY_COST * body.subscription_months:
        raise HTTPException(
            status_code=HTTPStatus.PAYMENT_REQUIRED,
            detail="Not enough credits to create an agent.",
        )

    with SessionLocal() as db:
        async with AuthenticatedAlephHttpClient(account=aleph_account, api_server=config.ALEPH_API_URL) as client:
            rootfs_message: StoreMessage = await client.get_message(item_hash=rootfs, message_type=StoreMessage)
            rootfs_size = (
                rootfs_message.content.size
                if rootfs_message.content.size is not None
                else settings.DEFAULT_ROOTFS_SIZE
            )

            instance_message, _status = await client.create_instance(
                rootfs=rootfs,
                rootfs_size=rootfs_size,
                hypervisor=HypervisorType.qemu,
                payment=Payment(chain=Chain.ETH, type=PaymentType.hold, receiver=None),
                channel=config.ALEPH_AGENT_CHANNEL,
                address=config.ALEPH_OWNER,
                ssh_keys=[body.ssh_public_key],
                metadata={"name": f"agent-{agent_id}"},
                vcpus=settings.DEFAULT_VM_VCPUS,
                memory=settings.DEFAULT_INSTANCE_MEMORY,
                sync=True,
            )

        # Create agent in the database
        agent = Agent(
            agent_id=agent_id,
            instance_hash=instance_message.item_hash,
            name=body.name,
            user_address=user_address,
            ssh_public_key=body.ssh_public_key,
            is_active=True,
        )

        # Create a subscription for the agent
        from src.models.subscription import SubscriptionType
        from src.services.subscription import SubscriptionService

        # Create subscription and handle initial payment
        subscription = SubscriptionService.create_subscription(
            user_address=user_address,
            subscription_type=SubscriptionType.agent,
            amount=AGENT_MONTHLY_COST,
            related_id=agent_id,
            months=body.subscription_months,
        )

        # Update paid_until with the next charge date
        agent.subscription_id = subscription.id

        db.add(agent)
        db.commit()
        db.refresh(agent)

        return AgentResponse.model_validate(agent)


@router.get("/", description="List all agents for the current user")
async def list_agents(user_address: str = Depends(get_current_address)) -> list[GetAgentResponse]:
    with SessionLocal() as db:
        agents = db.query(Agent).filter(Agent.user_address == user_address).all()

        agent_response = []
        for agent in agents:
            try:
                ip_address = await fetch_instance_ip(agent.instance_hash)
            except ValueError:
                ip_address = None

            agent_response.append(
                GetAgentResponse(
                    id=agent.id,
                    instance_hash=agent.instance_hash,
                    name=agent.name,
                    user_address=agent.user_address,
                    monthly_cost=agent.subscription.amount,
                    paid_until=agent.subscription.next_charge_at,
                    instance_ip=ip_address,
                    is_active=agent.is_active,
                    subscription_id=agent.subscription_id,
                )
            )

        return agent_response


@router.get("/{agent_id}", description="Get an agent's public information")
async def get_agent_public_info(agent_id: uuid.UUID) -> GetAgentResponse:
    with SessionLocal() as db:
        agent = db.query(Agent).filter(Agent.id == agent_id).first()

        if not agent:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"Agent with ID {agent_id} not found.",
            )

        try:
            ip_address = await fetch_instance_ip(agent.instance_hash)
        except ValueError:
            ip_address = None

        return GetAgentResponse(
            id=agent.id,
            instance_hash=agent.instance_hash,
            name=agent.name,
            user_address=agent.user_address,
            monthly_cost=agent.subscription.amount,
            paid_until=agent.subscription.next_charge_at,
            instance_ip=ip_address,
            is_active=agent.is_active,
            subscription_id=agent.subscription_id,
        )


@router.get("/{agent_id}/secret", description="Get an agent secret")
async def get_agent_secret(
    agent_id: uuid.UUID, user_address: str = Depends(get_current_address)
) -> GetAgentSecretResponse:
    with SessionLocal() as db:
        agent = db.query(Agent).filter(Agent.id == agent_id, Agent.user_address == user_address).first()

        if not agent:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"Agent with ID {agent_id} not found.",
            )

        return GetAgentSecretResponse(secret=agent.secret)


@router.post(
    "/{agent_id}/resubscribe", description="Resubscribe to an inactive agent", response_model=ResubscribeAgentResponse
)
async def resubscribe_agent(
    agent_id: uuid.UUID,
    body: ResubscribeAgentRequest,
    user_address: str = Depends(get_current_address),
) -> ResubscribeAgentResponse:
    """
    Resubscribe to an agent that was deactivated due to payment failure or cancellation.
    """

    with SessionLocal() as db:
        agent = db.query(Agent).filter(Agent.id == agent_id, Agent.user_address == user_address).first()

        if not agent:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"Agent with ID {agent_id} not found.",
            )

        # Check if agent is already active
        if agent.is_active:
            return ResubscribeAgentResponse(
                success=True, paid_until=agent.subscription.next_charge_at, error="Agent is already active"
            )

        # Resume existing subscription
        success = SubscriptionService.resume_subscription(agent.subscription_id, body.subscription_months)

        if not success:
            return ResubscribeAgentResponse(
                success=False, error="Could not reactivate subscription. Please ensure you have enough credits."
            )

        # Update agent
        # TODO: recreate instance
        agent.is_active = True

        db.commit()
        db.refresh(agent)
        return ResubscribeAgentResponse(success=True, paid_until=agent.subscription.next_charge_at)


@router.delete("/{agent_id}", description="Delete an agent")
async def delete_agent(
    agent_id: uuid.UUID,
    user_address: str = Depends(get_current_address),
):
    from src.services.subscription import SubscriptionService

    with SessionLocal() as db:
        agent = db.query(Agent).filter(Agent.id == agent_id, Agent.user_address == user_address).first()

        if not agent:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"Agent with ID {agent_id} not found.",
            )

        # Delete the Aleph instance
        aleph_account = ETHAccount(config.ALEPH_SENDER_SK)
        async with AuthenticatedAlephHttpClient(account=aleph_account, api_server=config.ALEPH_API_URL) as client:
            await client.forget(
                address=config.ALEPH_OWNER,
                hashes=[agent.instance_hash],
                channel=config.ALEPH_AGENT_CHANNEL,
                reason="Agent deleted by user",
            )

        # Deactivate the agent instead of deleting it
        agent.is_active = False

        # Cancel the associated subscription if it exists
        if agent.subscription_id:
            SubscriptionService.cancel_subscription(agent.subscription_id)

        db.commit()

        return {"detail": "Agent deleted successfully"}


@scheduler.scheduled_job("interval", hours=1)
async def process_subscriptions():
    """
    Scheduled job to process all subscriptions.
    This includes:
    1. Processing renewals for active subscriptions
    2. Activating/deactivating agents based on subscription status
    """
    logger.info("Running scheduled subscription processing")

    from src.models.subscription import Subscription
    from src.services.subscription import SubscriptionService

    with SessionLocal() as db:
        # Find all active subscriptions
        active_subscriptions = db.query(Subscription).filter(Subscription.status == SubscriptionStatus.active).all()

        if not active_subscriptions:
            logger.info("No active subscriptions found")
            return

        logger.info(f"Processing {len(active_subscriptions)} active subscriptions")

        # Process each subscription
        for subscription in active_subscriptions:
            # Skip if not due yet
            if datetime.now() < subscription.next_charge_at:
                continue

            try:
                # Process renewal
                success = SubscriptionService.process_renewal(subscription.id)

                # If subscription is for an agent, update agent status if needed
                if subscription.subscription_type == "agent":
                    # Find the associated agent
                    agent = db.query(Agent).filter(Agent.subscription_id == subscription.id).first()

                    if agent:
                        # Update agent status based on subscription status
                        if success:
                            # Update paid_until
                            agent.paid_until = subscription.next_charge_at
                            agent.is_active = True
                            agent.add_renew_transaction(subscription.amount)
                            logger.info(f"Renewed agent {agent.id} until {agent.paid_until}")
                        elif not success and subscription.status == SubscriptionStatus.paused:
                            # Deactivate agent if payment failed and subscription is paused
                            agent.is_active = False
                            logger.warning(f"Deactivated agent {agent.id} due to payment failure")
                    else:
                        logger.warning(f"Agent for subscription {subscription.id} not found")

            except Exception as e:
                logger.error(f"Error processing subscription {subscription.id}: {str(e)}", exc_info=True)

        # Process paused subscriptions with agents - agents should be deactivated
        paused_agent_subscriptions = (
            db.query(Subscription)
            .filter(Subscription.status == SubscriptionStatus.paused, Subscription.subscription_type == "agent")
            .all()
        )

        for subscription in paused_agent_subscriptions:
            agent = db.query(Agent).filter(Agent.subscription_id == subscription.id).first()
            if agent and agent.is_active:
                agent.is_active = False
                logger.info(f"Deactivated agent {agent.id} due to paused subscription")

        # Process cancelled subscriptions with agents - agents should be deactivated
        cancelled_agent_subscriptions = (
            db.query(Subscription)
            .filter(Subscription.status == SubscriptionStatus.cancelled, Subscription.subscription_type == "agent")
            .all()
        )

        for subscription in cancelled_agent_subscriptions:
            agent = db.query(Agent).filter(Agent.subscription_id == subscription.id).first()
            if agent and agent.is_active:
                agent.is_active = False
                logger.info(f"Deactivated agent {agent.id} due to cancelled subscription")

        db.commit()

        logger.info("Subscription processing completed")
