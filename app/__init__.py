import os

from flask import Flask, g, session
from config import Config
from flask_socketio import SocketIO
from flask_session import Session
from .database import get_db_connection

# Redis message queue lets SocketIO work across scaled web replicas.
_redis_url = os.environ.get("REDIS_URL") or None

# Initialize SocketIO with cors_allowed_origins to allow all origins
socketio = SocketIO(
    cors_allowed_origins="*",
    async_mode="eventlet",
    logger=True,
    engineio_logger=True,
    message_queue=_redis_url,
)

def init_db():
    conn = get_db_connection()
    if conn is not None:
        cursor = conn.cursor()
        # Check if database is already initialized
        cursor.execute('''SELECT EXISTS (
                           SELECT FROM information_schema.tables 
                           WHERE table_name = 'games'
                       )''')
        result = cursor.fetchone()
        table_exists = result['exists'] if result else False
        if not table_exists:  # Only initialize if tables don't exist
            print("Tables don't exist yet - they should have been created by migration")
        cursor.close()
        conn.close()

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    
    # Fix for Werkzeug 3.x compatibility - patch response.set_cookie to handle bytes
    from werkzeug.sansio.response import Response
    original_set_cookie = Response.set_cookie
    
    def patched_set_cookie(self, key, value='', **kwargs):
        # Ensure value is always a string, not bytes
        if isinstance(value, bytes):
            value = value.decode('utf-8')
        return original_set_cookie(self, key, value, **kwargs)
    
    Response.set_cookie = patched_set_cookie
    
    # Initialize Flask-Session
    Session(app)
    
    # Initialize database
    init_db()
    
    # Register blueprints
    from app.routes import main
    from app.events import events
    from app.auth_routes import auth_bp
    
    app.register_blueprint(main)
    app.register_blueprint(events)
    app.register_blueprint(auth_bp)
    
    # Initialize SocketIO with the app (Redis queue when REDIS_URL is set)
    socketio.init_app(
        app,
        cors_allowed_origins="*",
        message_queue=_redis_url,
    )
    
    # Add context processor for authentication
    @app.context_processor
    def inject_auth():
        from app.auth import get_current_user
        return dict(current_user=get_current_user())
    
    # Add before_request handler for session management
    @app.before_request
    def before_request():
        from app.auth import load_current_user
        g.user = load_current_user()
        
        # Clean up expired sessions periodically
        from app.auth import SessionManager
        import random
        if random.randint(1, 100) == 1:  # 1% chance to run cleanup
            SessionManager.cleanup_expired_sessions()
    
    return app 