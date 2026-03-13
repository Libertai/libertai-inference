import uuid
from http import HTTPStatus

from aleph.sdk import AuthenticatedAlephHttpClient
from aleph.sdk.chains.ethereum import ETHAccount
from aleph.sdk.conf import settings
from aleph_message.models import Chain, Payment, PaymentType, StoreMessage
from aleph_message.models.execution.environment import HypervisorType
from fastapi import HTTPException, Depends
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.config import config
from src.interfaces.agent import (
    CreateAgentRequest,
    AgentResponse,
    GetAgentResponse,
)
from src.models import SubscriptionStatus
from src.models.agent import Agent
from src.models.base import AsyncSessionLocal
from src.models.subscription import Subscription
from src.routes.agents import router
from src.services.auth import get_current_address
from src.services.credit import CreditService
from src.utils.aleph import fetch_instance_ip
from src.utils.cron import scheduler
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


AGENT_MONTHLY_COST = 10  # Monthly cost in credits


async def _create_agent_instance(
    client: AuthenticatedAlephHttpClient, agent_id: uuid.UUID, ssh_public_key: str
) -> str:
    rootfs = settings.UBUNTU_24_QEMU_ROOTFS_ID
    rootfs_message: StoreMessage = await client.get_message(item_hash=rootfs, message_type=StoreMessage)
    rootfs_size = (
        rootfs_message.content.size if rootfs_message.content.size is not None else settings.DEFAULT_ROOTFS_SIZE
    )

    instance_message, _status = await client.create_instance(
        rootfs=rootfs,
        rootfs_size=rootfs_size,
        hypervisor=HypervisorType.qemu,
        payment=Payment(chain=Chain.ETH, type=PaymentType.hold, receiver=None),
        channel=config.ALEPH_AGENT_CHANNEL,
        address=config.ALEPH_OWNER,
        ssh_keys=[ssh_public_key],
        metadata={"name": f"agent-{agent_id}"},
        vcpus=settings.DEFAULT_VM_VCPUS,
        memory=settings.DEFAULT_INSTANCE_MEMORY,
        sync=True,
    )

    return instance_message.item_hash


@router.post("/", description="Create a new agent", response_model=AgentResponse)  # type: ignore
async def create_agent(
    body: CreateAgentRequest,
    user_address: str = Depends(get_current_address),
) -> AgentResponse:
    agent_id = uuid.uuid4()

    aleph_account = ETHAccount(config.ALEPH_SENDER_SK)

    user_balance = await CreditService.get_balance(user_address)
    if user_balance < AGENT_MONTHLY_COST * body.subscription_months:
        raise HTTPException(
            status_code=HTTPStatus.PAYMENT_REQUIRED,
            detail="Not enough credits to create an agent.",
        )

    async with AsyncSessionLocal() as db:
        async with AuthenticatedAlephHttpClient(account=aleph_account, api_server=config.ALEPH_API_URL) as client:
            instance_hash = await _create_agent_instance(client, agent_id, body.ssh_public_key)

        agent = Agent(
            agent_id=agent_id,
            instance_hash=instance_hash,
            name=body.name,
            user_address=user_address,
            ssh_public_key=body.ssh_public_key,
        )

        from src.models.subscription import SubscriptionType
        from src.services.subscription import SubscriptionService

        subscription = await SubscriptionService.create_subscription(
            user_address=user_address,
            subscription_type=SubscriptionType.agent,
            amount=AGENT_MONTHLY_COST,
            related_id=agent_id,
            months=body.subscription_months,
            db_session=db,
        )

        agent.subscription_id = subscription.id

        db.add(agent)
        await db.commit()
        await db.refresh(agent)

        return AgentResponse(
            id=agent.id,
            instance_hash=agent.instance_hash,
            name=agent.name,
            user_address=agent.user_address,
            created_at=agent.created_at,
            monthly_cost=AGENT_MONTHLY_COST,
            paid_until=subscription.next_charge_at,
            renew_history=[],
            subscription_status=subscription.status,
            subscription_id=agent.subscription_id,
        )


@router.get("/", description="List all agents for the current user")  # type: ignore
async def list_agents(user_address: str = Depends(get_current_address)) -> list[GetAgentResponse]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Agent).where(Agent.user_address == user_address).options(selectinload(Agent.subscription))
        )
        agents = result.scalars().all()

        agent_response = []
        for agent in agents:
            try:
                if agent.instance_hash is None:
                    ip_address = None
                else:
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
                    subscription_status=agent.subscription.status,
                    subscription_id=agent.subscription_id,
                )
            )

        return agent_response


@router.get("/{agent_id}", description="Get an agent's public information")  # type: ignore
async def get_agent_public_info(agent_id: uuid.UUID) -> GetAgentResponse:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Agent).where(Agent.id == agent_id).options(selectinload(Agent.subscription)))
        agent = result.scalars().first()

        if not agent:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"Agent with ID {agent_id} not found.",
            )

        try:
            if agent.instance_hash is None:
                ip_address = None
            else:
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
            subscription_status=agent.subscription.status,
            subscription_id=agent.subscription_id,
        )


@router.post("/{agent_id}/reallocate", description="Reallocate an agent instance")  # type: ignore
async def reallocate_agent(
    agent_id: uuid.UUID,
    user_address: str = Depends(get_current_address),
) -> GetAgentResponse:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Agent)
            .join(Agent.subscription)
            .where(Agent.id == agent_id, Agent.user_address == user_address)
            .options(selectinload(Agent.subscription))
        )
        agent = result.scalars().first()

        if not agent:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"Agent with ID {agent_id} not found.",
            )

        if agent.subscription.status == SubscriptionStatus.inactive:
            raise HTTPException(
                status_code=HTTPStatus.FORBIDDEN,
                detail="Cannot reallocate agent with inactive subscription.",
            )

        if agent.instance_hash is None:
            raise HTTPException(
                status_code=HTTPStatus.BAD_REQUEST,
                detail="Agent has no active instance to reallocate.",
            )

        aleph_account = ETHAccount(config.ALEPH_SENDER_SK)

        async with AuthenticatedAlephHttpClient(account=aleph_account, api_server=config.ALEPH_API_URL) as client:
            try:
                await client.forget(
                    address=config.ALEPH_OWNER,
                    hashes=[agent.instance_hash],
                    channel=config.ALEPH_AGENT_CHANNEL,
                    reason="Agent reallocation requested",
                )
            except Exception as e:
                logger.warning(f"Failed to forget previous instance {agent.instance_hash}: {str(e)}")

            new_instance_hash = await _create_agent_instance(client, agent_id, agent.ssh_public_key)

            agent.instance_hash = new_instance_hash
            await db.commit()

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
            subscription_status=agent.subscription.status,
            subscription_id=agent.subscription_id,
        )


@scheduler.scheduled_job("interval", hours=6)
async def remove_expired_agents():
    logger.info("Running scheduled agents cleanup job")

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Agent)
            .join(Agent.subscription)
            .where(Subscription.status == SubscriptionStatus.inactive, Agent.instance_hash.is_not(None))
        )
        expired_agents = result.scalars().all()
        logger.info(f"Processing {len(expired_agents)} expired agents")

        for agent in expired_agents:
            try:
                aleph_account = ETHAccount(config.ALEPH_SENDER_SK)
                async with AuthenticatedAlephHttpClient(
                    account=aleph_account, api_server=config.ALEPH_API_URL
                ) as client:
                    await client.forget(
                        address=config.ALEPH_OWNER,
                        hashes=[agent.instance_hash],
                        channel=config.ALEPH_AGENT_CHANNEL,
                        reason="Agent subscription expired",
                    )
                agent.instance_hash = None
            except Exception as e:
                logger.error(f"Error processing agent {agent.id} cleanup: {str(e)}", exc_info=True)
        await db.commit()

        logger.info("Agents cleanup job completed")
