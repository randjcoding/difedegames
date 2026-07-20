import os
import secrets

class Config:
    # Flask Configuration - Ensure SECRET_KEY is always a string
    _secret_key = os.environ.get('SECRET_KEY') or 'difedeapp-static-secret-key-for-development-only'
    SECRET_KEY = str(_secret_key) if isinstance(_secret_key, bytes) else _secret_key
    
    # Session Configuration
    SESSION_TYPE = 'filesystem'
    SESSION_PERMANENT = False
    SESSION_USE_SIGNER = True
    SESSION_KEY_PREFIX = 'difedeappv2'
    SESSION_COOKIE_NAME = 'difedeappv2_session'
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SECURE = False  # Set to True in production with HTTPS
    SESSION_COOKIE_SAMESITE = 'Lax'
    PERMANENT_SESSION_LIFETIME = 86400  # 24 hours in seconds
    
    # PostgreSQL Database Configuration
    # Prefer environment variables; fall back to local dev defaults so existing
    # deployments keep working unchanged. Set PG_PASSWORD in the environment for production.
    PG_HOST = os.environ.get('PG_HOST', 'localhost')
    PG_DATABASE = os.environ.get('PG_DATABASE', 'difedeappv2')
    PG_USER = os.environ.get('PG_USER', 'difedeapp')
    PG_PASSWORD = os.environ.get('PG_PASSWORD', 'Password')
    PG_PORT = int(os.environ.get('PG_PORT', 5432))
    
    # Authentication Configuration
    BCRYPT_LOG_ROUNDS = 12
    EMAIL_VERIFICATION_EXPIRY = 86400  # 24 hours
    PASSWORD_RESET_EXPIRY = 3600  # 1 hour
    MAX_LOGIN_ATTEMPTS = 5
    LOCKOUT_DURATION = 900  # 15 minutes
    
    # Email Configuration (for future email verification)
    MAIL_SERVER = os.environ.get('MAIL_SERVER')
    MAIL_PORT = os.environ.get('MAIL_PORT') or 587
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'true').lower() in ['true', 'on', '1']
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER')
    
    # Application Configuration
    APP_NAME = 'DiFede Games'
    ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'admin@difedeapp.com')
    
    # Security Configuration
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = None 