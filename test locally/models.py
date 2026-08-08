from sqlalchemy.orm import declarative_base
from sqlalchemy import Column,Integer,String,DateTime
from datetime import datetime


Base = declarative_base()


class Message(Base):

    __tablename__="messages"


    id = Column(
        Integer,
        primary_key=True
    )


    user_id = Column(
        String
    )


    text = Column(
        String
    )


    created_at = Column(
        DateTime,
        default=datetime.utcnow
    )
