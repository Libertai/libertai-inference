from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from src.config import config

Base = declarative_base()
engine = create_engine(config.DATABASE_URL, pool_size=20, max_overflow=5, pool_timeout=10, pool_recycle=1800)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
