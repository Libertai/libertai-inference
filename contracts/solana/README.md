# LibertAI Payment Processor - Solana CLI

A command-line interface for managing the LibertAI Payment Processor Solana program. This CLI provides comprehensive functionality for initializing the program, processing payments, managing admins, and withdrawing tokens.

## Table of Contents

- [Installation](#installation)
- [Global Options](#global-options)
- [Commands](#commands)
  - [initialize](#initialize)
  - [create-program-token-account](#create-program-token-account)
  - [process-payment](#process-payment)
  - [add-admin](#add-admin)
  - [remove-admin](#remove-admin)
  - [get-admins](#get-admins)
  - [change-owner](#change-owner)
  - [withdraw](#withdraw)
- [Examples](#examples)
- [Environment Setup](#environment-setup)

## Installation

```bash
# Install dependencies
npm install
# or
yarn install
```

## Global Options

These options are available for all commands:

| Option | Description | Default |
|--------|-------------|---------|
| `--payer-key-filepath <path>` | Path to payer private key JSON file | - |
| `--payer-private-key <key>` | Payer private key as JSON string | - |
| `--json-rpc-endpoint <url>` | Solana RPC endpoint | `https://api.testnet.solana.com` |
| `--token-mint <address>` | Token mint address | `mntpN8z1d29f3MWhMD7VqZFpeYmbD88MgwS3Bkz8y7u` |
| `--amount <amount>` | Amount of tokens (human-readable format) | - |
| `--admin <address>` | Admin public key address | - |
| `--new-owner <address>` | New owner public key address | - |
| `--destination <address>` | Destination wallet address | - |

**Note:** You must provide either `--payer-key-filepath` OR `--payer-private-key`, but not both.

## Commands

### initialize

Initialize the LibertAI Payment Processor program.

**Usage:**
```bash
npm run cli initialize [options]
```

**Required Options:**
- `--payer-key-filepath <path>` OR `--payer-private-key <key>`

**Examples:**
```bash
# Initialize with key file
npm run cli initialize --payer-key-filepath ./keys/payer.json

# Initialize with private key string  
npm run cli initialize --payer-private-key '[123,45,67,...]'

# Initialize on mainnet
npm run cli initialize --payer-key-filepath ./keys/payer.json --json-rpc-endpoint https://api.mainnet-beta.solana.com
```

### create-program-token-account

Create a program token account for holding tokens. This must be done before processing payments.

**Usage:**
```bash
npm run cli create-program-token-account [options]
```

**Required Options:**
- `--payer-key-filepath <path>` OR `--payer-private-key <key>`

**Examples:**
```bash
# Create program token account
npm run cli create-program-token-account --payer-key-filepath ./keys/payer.json

# Create with custom token mint
npm run cli create-program-token-account --payer-key-filepath ./keys/payer.json --token-mint 9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM
```

### process-payment

Process a payment and emit a payment event.

**Usage:**
```bash
npm run cli process-payment [options]
```

**Required Options:**
- `--payer-key-filepath <path>` OR `--payer-private-key <key>`
- `--amount <amount>`

**Examples:**
```bash
# Process payment of 100 tokens
npm run cli process-payment --payer-key-filepath ./keys/user.json --amount 100

# Process payment with custom token mint
npm run cli process-payment --payer-key-filepath ./keys/user.json --amount 50 --token-mint 9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM

# Process payment on mainnet
npm run cli process-payment --payer-key-filepath ./keys/user.json --amount 25 --json-rpc-endpoint https://api.mainnet-beta.solana.com
```

### add-admin

Add a new admin to the program. Only existing owners/admins can add new admins.

**Usage:**
```bash
npm run cli add-admin [options]
```

**Required Options:**
- `--payer-key-filepath <path>` OR `--payer-private-key <key>`
- `--admin <address>`

**Examples:**
```bash
# Add new admin
npm run cli add-admin --payer-key-filepath ./keys/owner.json --admin 9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM

# Add admin on mainnet
npm run cli add-admin --payer-key-filepath ./keys/owner.json --admin 9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM --json-rpc-endpoint https://api.mainnet-beta.solana.com
```

### remove-admin

Remove an admin from the program. Only existing owners/admins can remove admins.

**Usage:**
```bash
npm run cli remove-admin [options]
```

**Required Options:**
- `--payer-key-filepath <path>` OR `--payer-private-key <key>`
- `--admin <address>`

**Examples:**
```bash
# Remove admin
npm run cli remove-admin --payer-key-filepath ./keys/owner.json --admin 9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM

# Remove admin on mainnet
npm run cli remove-admin --payer-key-filepath ./keys/owner.json --admin 9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM --json-rpc-endpoint https://api.mainnet-beta.solana.com
```

### get-admins

Get all current program admins. This is a read-only operation that doesn't require payer keys.

**Usage:**
```bash
npm run cli get-admins [options]
```

**Required Options:**
None

**Examples:**
```bash
# Get admins on testnet
npm run cli get-admins

# Get admins on mainnet
npm run cli get-admins --json-rpc-endpoint https://api.mainnet-beta.solana.com
```

### change-owner

Change the owner of the program. Only the current owner can change ownership.

**Usage:**
```bash
npm run cli change-owner [options]
```

**Required Options:**
- `--payer-key-filepath <path>` OR `--payer-private-key <key>`
- `--new-owner <address>`

**Examples:**
```bash
# Change owner
npm run cli change-owner --payer-key-filepath ./keys/current-owner.json --new-owner 9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM

# Change owner on mainnet
npm run cli change-owner --payer-key-filepath ./keys/current-owner.json --new-owner 9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM --json-rpc-endpoint https://api.mainnet-beta.solana.com
```

### withdraw

Withdraw LTAI tokens from the program account. Only admins/owners can withdraw tokens.

**Usage:**
```bash
npm run cli withdraw [options]
```

**Required Options:**
- `--payer-key-filepath <path>` OR `--payer-private-key <key>`
- `--destination <address>`
- `--amount <amount>`

**Examples:**
```bash
# Withdraw 500 tokens to a wallet
npm run cli withdraw --payer-key-filepath ./keys/admin.json --destination 9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM --amount 500

# Withdraw with custom token mint
npm run cli withdraw --payer-key-filepath ./keys/admin.json --destination 9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM --amount 250 --token-mint CustomTokenMintAddress

# Withdraw on mainnet
npm run cli withdraw --payer-key-filepath ./keys/admin.json --destination 9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM --amount 100 --json-rpc-endpoint https://api.mainnet-beta.solana.com
```

## Examples

### Complete Setup Flow

```bash
# 1. Initialize the program
npm run cli initialize --payer-key-filepath ./keys/owner.json

# 2. Create program token account
npm run cli create-program-token-account --payer-key-filepath ./keys/owner.json

# 3. Add an admin
npm run cli add-admin --payer-key-filepath ./keys/owner.json --admin AdminPublicKeyHere

# 4. Process a payment
npm run cli process-payment --payer-key-filepath ./keys/user.json --amount 100

# 5. Check current admins
npm run cli get-admins

# 6. Withdraw tokens (admin only)
npm run cli withdraw --payer-key-filepath ./keys/admin.json --destination DestinationWalletAddress --amount 50
```

### Working with Different Networks

```bash
# Testnet (default)
npm run cli get-admins

# Devnet
npm run cli get-admins --json-rpc-endpoint https://api.devnet.solana.com

# Mainnet
npm run cli get-admins --json-rpc-endpoint https://api.mainnet-beta.solana.com

# Custom RPC
npm run cli get-admins --json-rpc-endpoint https://your-custom-rpc.com
```

## Environment Setup

### Creating Key Files

Key files should be JSON arrays containing the private key bytes:

```json
[123,45,67,89,12,34,56,78,90,12,34,56,78,90,12,34,56,78,90,12,34,56,78,90,12,34,56,78,90,12,34,56]
```

### Using Environment Variables

You can also use private keys directly as environment variables:

```bash
export PAYER_KEY='[123,45,67,...]'
npm run cli initialize --payer-private-key "$PAYER_KEY"
```

### Default Token Information

- **Default Token Mint:** `mntpN8z1d29f3MWhMD7VqZFpeYmbD88MgwS3Bkz8y7u` (LTAI token)
- **Default RPC Endpoint:** `https://api.testnet.solana.com`

## Development

```bash
# Check code formatting
npm run lint

# Fix code formatting
npm run lint:fix

# Build the project
anchor build

# Run tests
anchor test
```
