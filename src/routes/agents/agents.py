import io
import time
from http import HTTPStatus
from uuid import uuid4

import paramiko
from aleph.sdk import AuthenticatedAlephHttpClient
from aleph.sdk.chains.ethereum import ETHAccount
from aleph.sdk.conf import settings
from aleph_message.models import Chain, Payment, PaymentType, StoreMessage
from aleph_message.models.execution.environment import HypervisorType
from fastapi import File, Form, HTTPException, UploadFile, Depends
from libertai_utils.interfaces.agent import (
    AddSSHKeyAgentResponse,
    AgentPythonPackageManager,
    AgentUsageType,
    UpdateAgentResponse,
    AddSSHKeyAgentBody,
    BaseDeleteAgentBody,
)
from libertai_utils.utils.crypto import decrypt, encrypt

from src.config import config
from src.interfaces.agent import (
    Agent,
    GetAgentResponse,
    GetAgentSecretResponse,
)
from src.routes.agents import router
from src.services.auth import get_current_address
from src.utils.agent import fetch_agents
from src.utils.aleph import fetch_instance_ip
from src.utils.ssh import generate_ssh_key_pair


@router.post("/", description="Create a new agent")
async def create_agent(user_address: str = Depends(get_current_address)) -> None:
    agent_id = str(uuid4())

    secret = str(uuid4())
    encrypted_secret = encrypt(secret, config.ALEPH_SENDER_PK)

    private_key, public_key = generate_ssh_key_pair()
    encrypted_private_key = encrypt(private_key, config.ALEPH_SENDER_PK)

    rootfs = settings.UBUNTU_22_QEMU_ROOTFS_ID

    aleph_account = ETHAccount(config.ALEPH_SENDER_SK)
    async with AuthenticatedAlephHttpClient(account=aleph_account, api_server=config.ALEPH_API_URL) as client:
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
            ssh_keys=[public_key],
            metadata={"name": agent_id},
            vcpus=settings.DEFAULT_VM_VCPUS,
            memory=settings.DEFAULT_INSTANCE_MEMORY,
            sync=True,
        )

        agent = Agent(
            id=agent_id,
            subscription_id=body.subscription_id,
            instance_hash=instance_message.item_hash,
            encrypted_secret=encrypted_secret,
            encrypted_ssh_key=encrypted_private_key,
            last_update=int(time.time()),
            tags=[agent_id, body.subscription_id, body.account.address],
        )

        await client.create_post(
            address=config.ALEPH_OWNER,
            post_content=agent.dict(),
            post_type=config.ALEPH_AGENT_POST_TYPE,
            channel=config.ALEPH_AGENT_CHANNEL,
        )


@router.get("/{agent_id}", description="Get an agent public information")
async def get_agent_public_info(agent_id: str) -> GetAgentResponse:
    """Get an agent by an ID (either agent ID or subscription ID)"""
    agents = await fetch_agents([agent_id])

    if len(agents) != 1:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f"Agent with ID {agent_id} not found.",
        )
    agent = agents[0]

    try:
        ip_address = await fetch_instance_ip(agent.instance_hash)
    except ValueError:
        ip_address = None

    return GetAgentResponse(
        id=agent.id,
        instance_hash=agent.instance_hash,
        instance_ip=ip_address,
        last_update=agent.last_update,
        subscription_id=agent.subscription_id,
    )


@router.get("/{agent_id}/secret", description="Get an agent secret")
async def get_agent_secret(agent_id: str, user_address: str = Depends(get_current_address)) -> GetAgentSecretResponse:
    agents = await fetch_agents([agent_id])

    if len(agents) != 1:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f"Agent with ID {agent_id} not found.",
        )
    agent = agents[0]

    # TODO: check that it is the agent of the user

    decrypted_secret = decrypt(agent.encrypted_secret, config.ALEPH_SENDER_SK)

    return GetAgentSecretResponse(secret=decrypted_secret)


@router.put("/{agent_id}", description="Deploy an agent or update it")
async def update(
    agent_id: str,
    secret: str = Form(),
    deploy_script_url: str = Form(
        default="https://raw.githubusercontent.com/Libertai/libertai-agents/refs/heads/main/deployment/deploy.sh"
    ),
    python_version: str = Form(),
    usage_type: AgentUsageType = Form(),
    package_manager: AgentPythonPackageManager = Form(),
    code: UploadFile = File(...),
) -> UpdateAgentResponse:
    agents = await fetch_agents([agent_id])

    if len(agents) != 1:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f"Agent with ID {agent_id} not found.",
        )
    agent = agents[0]

    # Validating the secret
    decrypted_secret = decrypt(agent.encrypted_secret, config.ALEPH_SENDER_SK)
    if secret != decrypted_secret:
        raise HTTPException(
            status_code=HTTPStatus.UNAUTHORIZED,
            detail="The secret provided doesn't match the one of this agent.",
        )

    ssh_private_key = decrypt(agent.encrypted_ssh_key, config.ALEPH_SENDER_SK)

    try:
        hostname = await fetch_instance_ip(agent.instance_hash)
    except ValueError:
        raise HTTPException(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            detail="Instance IPv6 address not found, it probably isn't allocated yet. Please try again in a few minutes.",
        )

    # Create a Paramiko SSH client
    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    # Load private key from string
    rsa_key = paramiko.RSAKey(file_obj=io.StringIO(ssh_private_key))

    # Read the file content into memory
    content = await code.read()

    # Connect to the server
    ssh_client.connect(hostname=hostname, username="root", pkey=rsa_key)

    # Send the zip with the code
    sftp = ssh_client.open_sftp()
    remote_path = "/tmp/libertai-agent.zip"
    sftp.putfo(io.BytesIO(content), remote_path)
    sftp.close()

    script_path = "/tmp/deploy-agent.sh"

    # Execute the command
    _stdin, _stdout, stderr = ssh_client.exec_command(
        f"wget {deploy_script_url} -O {script_path} -q --no-cache && chmod +x {script_path} && {script_path} {python_version} {package_manager.value} {usage_type.value}"
    )
    # Waiting for the command to complete to get error logs
    stderr.channel.recv_exit_status()

    # Close the connection
    ssh_client.close()

    # Register the program
    aleph_account = ETHAccount(config.ALEPH_SENDER_SK)
    async with AuthenticatedAlephHttpClient(account=aleph_account, api_server=config.ALEPH_API_URL) as client:
        # Updating the related POST message
        await client.create_post(
            address=config.ALEPH_OWNER,
            post_content=Agent(
                **agent.dict(exclude={"last_update"}),
                last_update=int(time.time()),
            ),
            post_type="amend",
            ref=agent.post_hash,
            channel=config.ALEPH_AGENT_CHANNEL,
        )

    return UpdateAgentResponse(instance_ip=hostname, error_log=stderr.read())


@router.post("/{agent_id}/ssh-key", description="Add an SSH key to a deployed agent")
async def add_ssh_key(agent_id: str, body: AddSSHKeyAgentBody) -> AddSSHKeyAgentResponse:
    add_ssh_key_script_url = (
        "https://raw.githubusercontent.com/Libertai/libertai-agents/refs/heads/main/deployment/add_ssh_key.sh"
    )
    agents = await fetch_agents([agent_id])

    if len(agents) != 1:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f"Agent with ID {agent_id} not found.",
        )
    agent = agents[0]

    # Validating the secret
    decrypted_secret = decrypt(agent.encrypted_secret, config.ALEPH_SENDER_SK)
    if body.secret != decrypted_secret:
        raise HTTPException(
            status_code=HTTPStatus.UNAUTHORIZED,
            detail="The secret provided doesn't match the one of this agent.",
        )

    ssh_private_key = decrypt(agent.encrypted_ssh_key, config.ALEPH_SENDER_SK)

    try:
        hostname = await fetch_instance_ip(agent.instance_hash)
    except ValueError:
        raise HTTPException(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            detail="Instance IPv6 address not found, it probably isn't allocated yet. Please try again in a few minutes.",
        )

    # Create a Paramiko SSH client
    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    # Load private key from string
    rsa_key = paramiko.RSAKey(file_obj=io.StringIO(ssh_private_key))

    # Connect to the server
    ssh_client.connect(hostname=hostname, username="root", pkey=rsa_key)

    script_path = "/tmp/add-ssh-key.sh"

    # Execute the command
    _stdin, _stdout, stderr = ssh_client.exec_command(
        f"wget {add_ssh_key_script_url} -O {script_path} -q --no-cache && chmod +x {script_path} && {script_path} '{body.ssh_key}'"
    )

    # Waiting for the command to complete to get error logs
    stderr.channel.recv_exit_status()

    # Close the connection
    ssh_client.close()

    return AddSSHKeyAgentResponse(error_log=stderr.read())


# TODO: add a redeploy route to forget the previous instance and setup again the agent's instance (in case instance allocation is failed)
# accessible only with secret, or maybe admin ?


# TODO: move this route to a CRON checking subscription end
@router.delete("/", description="Remove an agent on subscription end")
async def delete(body: BaseDeleteAgentBody):
    agents = await fetch_agents([body.subscription_id])

    if len(agents) != 1:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f"Agent for subscription ID {body.subscription_id} not found.",
        )
    agent = agents[0]

    aleph_account = ETHAccount(config.ALEPH_SENDER_SK)
    async with AuthenticatedAlephHttpClient(account=aleph_account, api_server=config.ALEPH_API_URL) as client:
        await client.forget(
            address=config.ALEPH_OWNER,
            hashes=[agent.instance_hash],
            channel=config.ALEPH_AGENT_CHANNEL,
            reason="LibertAI Agent subscription ended",
        )
