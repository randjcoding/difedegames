import psycopg2
from psycopg2.extras import RealDictCursor
from config import Config

def get_db_connection():
    """Get PostgreSQL database connection"""
    try:
        conn = psycopg2.connect(
            host=Config.PG_HOST,
            database=Config.PG_DATABASE,
            user=Config.PG_USER,
            password=Config.PG_PASSWORD,
            port=Config.PG_PORT,
            cursor_factory=RealDictCursor
        )
        return conn
    except Exception as e:
        print(f"PostgreSQL connection error: {e}")
        return None 