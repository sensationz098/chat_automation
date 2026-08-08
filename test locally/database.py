from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


DATABASE_URL = (
    "postgresql://chatbot:password@localhost:5432/chatbotdb"
)


engine = create_engine(
    DATABASE_URL
)


SessionLocal = sessionmaker(
    bind=engine
)
