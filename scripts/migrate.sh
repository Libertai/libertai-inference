# Create new migration
source .env
alembic revision --autogenerate -m "$1"