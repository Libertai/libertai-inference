#!/usr/bin/env bash
# Bootstrap a Revolut merchant environment (sandbox or production) for LibertAI:
#   1. creates the Go/Plus/Max subscription plans, each with a USD and a EUR monthly variation
#   2. registers the payments webhook and prints its signing secret
#   3. prints the REVOLUT_PLAN_IDS env JSON to drop into Dokploy
#
# Usage:
#   REVOLUT_SECRET_KEY=sk_... \
#   REVOLUT_API_URL=https://sandbox-merchant.revolut.com \
#   WEBHOOK_URL=https://beta.inference.api.libertai.io/payments/webhook/revolut \
#   ./scripts/setup_revolut.sh
#
# Notes:
#   - Amounts are minor units, EUR net = same number as USD (Go 800 / Plus 2000 / Max 10000).
#   - VAT on the EUR variations is a dashboard/merchant setting — fine to skip on sandbox.
#   - Variations are matched by currency in the response (order is not guaranteed).

set -euo pipefail

: "${REVOLUT_SECRET_KEY:?set REVOLUT_SECRET_KEY}"
: "${REVOLUT_API_URL:?set REVOLUT_API_URL (e.g. https://sandbox-merchant.revolut.com)}"
API_VERSION="${REVOLUT_API_VERSION:-2026-04-20}"

req() { # method path [json-body]
	local method=$1 path=$2 body=${3:-}
	curl -sf -X "$method" "${REVOLUT_API_URL}${path}" \
		-H "Authorization: Bearer ${REVOLUT_SECRET_KEY}" \
		-H "Revolut-Api-Version: ${API_VERSION}" \
		-H "Content-Type: application/json" \
		${body:+--data-raw "$body"}
}

create_plan() { # name amount_minor -> {plan_id, USD variation, EUR variation} on stdout
	local name=$1 amount=$2
	req POST /api/subscription-plans "{
		\"name\": \"${name}\",
		\"variations\": [
			{\"phases\": [{\"ordinal\": 1, \"cycle_duration\": \"P1M\", \"amount\": ${amount}, \"currency\": \"USD\"}]},
			{\"phases\": [{\"ordinal\": 1, \"cycle_duration\": \"P1M\", \"amount\": ${amount}, \"currency\": \"EUR\"}]}
		]
	}" | jq '{
		plan_id: .id,
		USD: (.variations[] | select(.phases[0].currency == "USD") | .id),
		EUR: (.variations[] | select(.phases[0].currency == "EUR") | .id)
	}'
}

echo "Creating plans on ${REVOLUT_API_URL} ..." >&2
GO=$(create_plan "LibertAI Go" 800)
PLUS=$(create_plan "LibertAI Plus" 2000)
MAX=$(create_plan "LibertAI Max" 10000)

PLAN_IDS=$(jq -cn --argjson go "$GO" --argjson plus "$PLUS" --argjson max "$MAX" '{
	go:   {USD: {plan_id: $go.plan_id,   variation_id: $go.USD},   EUR: {plan_id: $go.plan_id,   variation_id: $go.EUR}},
	plus: {USD: {plan_id: $plus.plan_id, variation_id: $plus.USD}, EUR: {plan_id: $plus.plan_id, variation_id: $plus.EUR}},
	max:  {USD: {plan_id: $max.plan_id,  variation_id: $max.USD},  EUR: {plan_id: $max.plan_id,  variation_id: $max.EUR}}
}')

echo >&2
echo "# Dokploy env:" >&2
echo "REVOLUT_PLAN_IDS=${PLAN_IDS}"

if [ -n "${WEBHOOK_URL:-}" ]; then
	echo >&2
	echo "Registering webhook ${WEBHOOK_URL} ..." >&2
	WEBHOOK=$(req POST /api/webhooks "{
		\"url\": \"${WEBHOOK_URL}\",
		\"events\": [\"ORDER_COMPLETED\", \"ORDER_PAYMENT_FAILED\", \"ORDER_PAYMENT_DECLINED\", \"ORDER_FAILED\",
		             \"SUBSCRIPTION_INITIATED\", \"SUBSCRIPTION_CANCELLED\", \"SUBSCRIPTION_OVERDUE\", \"SUBSCRIPTION_FINISHED\"]
	}")
	echo "REVOLUT_WEBHOOK_SECRET=$(echo "$WEBHOOK" | jq -r .signing_secret)"
fi
