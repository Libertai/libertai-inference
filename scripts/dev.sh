# Start the development DB
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d postgres

# Run pending migrations
source .env
alembic upgrade head