import os
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import NullPool
from google.cloud.sql.connector import Connector
import sqlalchemy
from contextlib import contextmanager
from models import Base, User

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class DatabaseManager:
    def __init__(self):
        self.engine = None
        self.SessionLocal = None
        self.connector = None
        self._initialize_database()
    
    def _get_connection_string(self):
        """Get database connection string based on environment"""
        # Check if running in Cloud Run (production)
        if os.getenv('CLOUD_SQL_CONNECTION_NAME'):
            # Cloud SQL connection using Cloud SQL Connector
            return self._get_cloud_sql_connection()
        else:
            # Local development connection
            return self._get_local_connection_string()
    
    def _get_local_connection_string(self):
        """Create connection string for local MySQL"""
        db_host = os.getenv('DB_HOST', 'localhost')
        db_port = os.getenv('DB_PORT', '3306')
        db_name = os.getenv('DB_NAME', 'opencraft')
        db_user = os.getenv('DB_USER', 'root')
        db_password = os.getenv('DB_PASSWORD', '')
        
        return f"mysql+pymysql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"
    
    def _get_cloud_sql_connection(self):
        """Create connection using Cloud SQL Connector for production"""
        def getconn():
            if not self.connector:
                self.connector = Connector()
            
            conn = self.connector.connect(
                os.getenv('CLOUD_SQL_CONNECTION_NAME'),
                "pymysql",
                user=os.getenv('DB_USER'),
                password=os.getenv('DB_PASSWORD'),
                db=os.getenv('DB_NAME')
            )
            return conn
        
        # Return engine created with Cloud SQL connector
        return create_engine(
            "mysql+pymysql://",
            creator=getconn,
            poolclass=NullPool,
        )
    
    def _initialize_database(self):
        """Initialize database connection"""
        try:
            if os.getenv('CLOUD_SQL_CONNECTION_NAME'):
                # Use Cloud SQL connector
                self.engine = self._get_cloud_sql_connection()
            else:
                # Use standard connection string
                connection_string = self._get_connection_string()
                self.engine = create_engine(
                    connection_string,
                    pool_pre_ping=True,
                    pool_recycle=300,
                    echo=False  # Set to True for SQL debugging
                )
            
            self.SessionLocal = sessionmaker(
                autocommit=False,
                autoflush=False,
                bind=self.engine
            )
            
            # Test the connection
            with self.engine.connect() as connection:
                connection.execute(sqlalchemy.text("SELECT 1"))
            
            logger.info("Database connection established successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize database connection: {e}")
            raise
    
    def create_tables(self):
        """Create all database tables"""
        try:
            Base.metadata.create_all(bind=self.engine)
            logger.info("Database tables created successfully")
        except Exception as e:
            logger.error(f"Failed to create tables: {e}")
            raise
    
    @contextmanager
    def get_db_session(self):
        """Get database session with automatic cleanup"""
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Database session error: {e}")
            raise
        finally:
            session.close()
    
    def get_session(self) -> Session:
        """Get a new database session (manual management)"""
        return self.SessionLocal()
    
    def close(self):
        """Close database connections"""
        if self.connector:
            self.connector.close()
        if self.engine:
            self.engine.dispose()

# Global database manager instance
db_manager = DatabaseManager()

# Dependency function for FastAPI
def get_db():
    """FastAPI dependency for database sessions"""
    session = db_manager.get_session()
    try:
        yield session
    finally:
        session.close()