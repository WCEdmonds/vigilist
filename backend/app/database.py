from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings

# Neon (serverless Postgres) closes idle connections, so a pooled connection
# can be dead by the time SQLAlchemy hands it out — surfacing as an intermittent
# "asyncpg ... connection is closed" InterfaceError 500 on the first query of a
# request. pool_pre_ping health-checks (and transparently replaces) each
# connection before use; pool_recycle discards connections older than the window
# so they never age past Neon's idle timeout.
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=300,
)
async_session = async_sessionmaker(engine, expire_on_commit=False)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db():
    async with async_session() as session:
        yield session
