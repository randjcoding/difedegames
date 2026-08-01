import bcrypt
import secrets
import uuid
from datetime import datetime, timedelta
from functools import wraps
from flask import session, request, redirect, url_for, flash, g, jsonify
from email_validator import validate_email as validate_email_format, EmailNotValidError
import psycopg2.extras
from .database import get_db_connection
from config import Config


def _wants_json_auth_error():
    """API / XHR callers need 401 JSON, not an HTML login redirect."""
    if request.path.startswith('/api/'):
        return True
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return True
    ctype = (request.content_type or '').lower()
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and 'application/json' in ctype:
        return True
    accept = (request.headers.get('Accept') or '').lower()
    if 'application/json' in accept and 'text/html' not in accept:
        return True
    return False

class AuthenticationError(Exception):
    """Custom exception for authentication errors"""
    pass

class UserManager:
    """Handles all user-related database operations"""
    
    @staticmethod
    def hash_password(password):
        """Hash a password using bcrypt"""
        password_bytes = password.encode('utf-8')
        salt = bcrypt.gensalt(rounds=Config.BCRYPT_LOG_ROUNDS)
        return bcrypt.hashpw(password_bytes, salt).decode('utf-8')
    
    @staticmethod
    def verify_password(password, password_hash):
        """Verify a password against its hash"""
        try:
            password_bytes = password.encode('utf-8')
            hash_bytes = password_hash.encode('utf-8')
            return bcrypt.checkpw(password_bytes, hash_bytes)
        except Exception:
            return False
    
    @staticmethod
    def validate_email_format(email):
        """Validate email format (syntax only, no DNS deliverability check)"""
        try:
            validate_email_format(email, check_deliverability=False)
            return True
        except EmailNotValidError:
            return False
    
    @staticmethod
    def get_user_by_email(email):
        """Get user by email address"""
        conn = get_db_connection()
        if not conn:
            return None
        
        try:
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute('''
                SELECT * FROM users 
                WHERE email = %s AND is_active = TRUE
            ''', (email,))
            user = cursor.fetchone()
            cursor.close()
            return dict(user) if user else None
        except Exception as e:
            print(f"Error getting user by email: {e}")
            return None
        finally:
            conn.close()
    
    @staticmethod
    def get_user_by_id(user_id):
        """Get user by ID"""
        conn = get_db_connection()
        if not conn:
            return None
        
        try:
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute('''
                SELECT * FROM users 
                WHERE id = %s AND is_active = TRUE
            ''', (user_id,))
            user = cursor.fetchone()
            cursor.close()
            return dict(user) if user else None
        except Exception as e:
            print(f"Error getting user by ID: {e}")
            return None
        finally:
            conn.close()
    
    @staticmethod
    def create_user(email, password, first_name, last_name, family_name, **kwargs):
        """Create a new user"""
        conn = get_db_connection()
        if not conn:
            raise AuthenticationError("Database connection failed")
        
        try:
            # Validate email format
            if not UserManager.validate_email_format(email):
                raise AuthenticationError("Invalid email format")
            
            # Check if email already exists
            if UserManager.get_user_by_email(email):
                raise AuthenticationError("Email already exists")
            
            # Hash password
            password_hash = UserManager.hash_password(password)
            
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute('''
                INSERT INTO users (
                    email, password_hash, first_name, last_name, family_name,
                    address, city, state, zipcode, phone_number, role
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, email, first_name, last_name, family_name, role, created_at
            ''', (
                email, password_hash, first_name, last_name, family_name,
                kwargs.get('address'), kwargs.get('city'), kwargs.get('state'),
                kwargs.get('zipcode'), kwargs.get('phone_number'), 
                kwargs.get('role', 'family_admin')
            ))
            
            user = cursor.fetchone()
            conn.commit()
            cursor.close()
            
            # Create user_details record
            UserManager.create_user_details(user['id'])
            
            return dict(user)
            
        except psycopg2.IntegrityError as e:
            conn.rollback()
            if 'email' in str(e):
                raise AuthenticationError("Email already exists")
            raise AuthenticationError("User creation failed")
        except Exception as e:
            conn.rollback()
            print(f"Error creating user: {e}")
            raise AuthenticationError("User creation failed")
        finally:
            conn.close()
    
    @staticmethod
    def create_user_details(user_id):
        """Create user_details record for a user"""
        conn = get_db_connection()
        if not conn:
            return False
        
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO user_details (user_id) VALUES (%s)
            ''', (user_id,))
            conn.commit()
            cursor.close()
            return True
        except Exception as e:
            conn.rollback()
            print(f"Error creating user details: {e}")
            return False
        finally:
            conn.close()
    
    @staticmethod
    def authenticate_user(email, password):
        """Authenticate user with email and password"""
        user = UserManager.get_user_by_email(email)
        if not user:
            return None
        
        if not user['is_verified']:
            raise AuthenticationError("Email not verified")
        
        if not user.get('is_approved', False):
            raise AuthenticationError("Account pending approval")
        
        if UserManager.verify_password(password, user['password_hash']):
            # Update last login
            UserManager.update_last_login(user['id'])
            return user
        
        return None
    
    @staticmethod
    def update_last_login(user_id):
        """Update user's last login timestamp"""
        conn = get_db_connection()
        if not conn:
            return
        
        try:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE users 
                SET last_login = CURRENT_TIMESTAMP 
                WHERE id = %s
            ''', (user_id,))
            conn.commit()
            cursor.close()
        except Exception as e:
            print(f"Error updating last login: {e}")
        finally:
            conn.close()
    
    @staticmethod
    def create_verification_token(user_id):
        """Create email verification token for user"""
        conn = get_db_connection()
        if not conn:
            print("❌ Database connection failed")
            return None
        
        cursor = None
        try:
            # Generate secure token
            token = secrets.token_urlsafe(32)
            expires_at = datetime.utcnow() + timedelta(hours=24)  # 24 hour expiry
            
            print(f"🔧 Creating verification token for user {user_id}")
            
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO email_verification_tokens (user_id, token, expires_at)
                VALUES (%s, %s, %s)
                RETURNING token
            ''', (user_id, token, expires_at))
            
            result = cursor.fetchone()
            print(f"🔍 Debug - Raw result: {result}, type: {type(result)}")
            
            conn.commit()
            
            if result:
                # Handle different result types
                if isinstance(result, (tuple, list)):
                    token_returned = result[0]
                elif isinstance(result, dict):
                    token_returned = result.get('token', result.get(0))
                else:
                    token_returned = result
                    
                print(f"✅ Verification token created successfully for user {user_id}: {token_returned}")
                return token_returned
            else:
                print(f"❌ No token returned from database for user {user_id}")
                return None
            
        except Exception as e:
            if conn:
                conn.rollback()
            print(f"❌ Error creating verification token for user {user_id}: {e}")
            import traceback
            traceback.print_exc()
            return None
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
    
    @staticmethod
    def verify_email_token(token):
        """Verify email token and activate user account"""
        conn = get_db_connection()
        if not conn:
            return False, "Database connection failed"
        
        try:
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            
            # Get token info and check if valid
            cursor.execute('''
                SELECT vt.*, u.first_name, u.email, u.is_verified
                FROM email_verification_tokens vt
                JOIN users u ON vt.user_id = u.id
                WHERE vt.token = %s
                AND vt.expires_at > CURRENT_TIMESTAMP
            ''', (token,))
            
            token_data = cursor.fetchone()
            
            if not token_data:
                # Check if token exists but expired
                cursor.execute('''
                    SELECT vt.*, u.first_name, u.email
                    FROM email_verification_tokens vt
                    JOIN users u ON vt.user_id = u.id
                    WHERE vt.token = %s
                ''', (token,))
                
                expired_token = cursor.fetchone()
                if expired_token:
                    cursor.close()
                    conn.close()
                    return False, "expired"
                else:
                    cursor.close()
                    conn.close()
                    return False, "invalid"
            
            # Check if already verified
            if token_data['is_verified']:
                cursor.close()
                conn.close()
                return True, "already_verified"
            
            # Verify the user
            cursor.execute('''
                UPDATE users 
                SET is_verified = TRUE, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            ''', (token_data['user_id'],))
            
            # Delete the verification token
            cursor.execute('''
                DELETE FROM email_verification_tokens 
                WHERE token = %s
            ''', (token,))
            
            conn.commit()
            cursor.close()
            
            return True, {
                'user_id': token_data['user_id'],
                'first_name': token_data['first_name'],
                'email': token_data['email']
            }
            
        except Exception as e:
            conn.rollback()
            print(f"Error verifying email token: {e}")
            return False, "error"
        finally:
            conn.close()
    
    @staticmethod
    def resend_verification_email(email):
        """Resend verification email for unverified user"""
        conn = get_db_connection()
        if not conn:
            return False, "Database connection failed"
        
        try:
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            
            # Get unverified user
            cursor.execute('''
                SELECT id, first_name, email, is_verified
                FROM users 
                WHERE email = %s AND is_active = TRUE
            ''', (email,))
            
            user = cursor.fetchone()
            
            if not user:
                cursor.close()
                conn.close()
                return False, "User not found"
            
            if user['is_verified']:
                cursor.close()
                conn.close()
                return False, "User already verified"
            
            # Delete any existing verification tokens for this user
            cursor.execute('''
                DELETE FROM email_verification_tokens 
                WHERE user_id = %s
            ''', (user['id'],))
            
            conn.commit()
            cursor.close()
            
            # Create new verification token
            token = UserManager.create_verification_token(user['id'])
            if token:
                return True, {
                    'user_id': user['id'],
                    'first_name': user['first_name'],
                    'email': user['email'],
                    'token': token
                }
            else:
                return False, "Failed to create verification token"
                
        except Exception as e:
            conn.rollback()
            print(f"Error resending verification email: {e}")
            return False, "Error occurred"
        finally:
            conn.close()

class SessionManager:
    """Handles session management"""
    
    @staticmethod
    def create_session(user_id):
        """Create a new session for user"""
        session_token = str(uuid.uuid4())
        expires_at = datetime.utcnow() + timedelta(seconds=Config.PERMANENT_SESSION_LIFETIME)
        
        conn = get_db_connection()
        if not conn:
            return None
        
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO user_sessions (
                    user_id, session_token, expires_at, ip_address, user_agent
                ) VALUES (%s, %s, %s, %s, %s)
            ''', (
                user_id, session_token, expires_at,
                request.environ.get('REMOTE_ADDR'),
                request.environ.get('HTTP_USER_AGENT')
            ))
            conn.commit()
            cursor.close()
            return session_token
        except Exception as e:
            conn.rollback()
            print(f"Error creating session: {e}")
            return None
        finally:
            conn.close()
    
    @staticmethod
    def validate_session(session_token):
        """Validate session token and return user"""
        conn = get_db_connection()
        if not conn:
            return None
        
        try:
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute('''
                SELECT u.*, s.expires_at 
                FROM users u
                JOIN user_sessions s ON u.id = s.user_id
                WHERE s.session_token = %s 
                AND s.expires_at > CURRENT_TIMESTAMP
                AND u.is_active = TRUE
            ''', (session_token,))
            result = cursor.fetchone()
            cursor.close()
            
            if result:
                return dict(result)
            return None
            
        except Exception as e:
            print(f"Error validating session: {e}")
            return None
        finally:
            conn.close()
    
    @staticmethod
    def destroy_session(session_token):
        """Destroy a session"""
        conn = get_db_connection()
        if not conn:
            return
        
        try:
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM user_sessions 
                WHERE session_token = %s
            ''', (session_token,))
            conn.commit()
            cursor.close()
        except Exception as e:
            print(f"Error destroying session: {e}")
        finally:
            conn.close()
    
    @staticmethod
    def cleanup_expired_sessions():
        """Clean up expired sessions"""
        conn = get_db_connection()
        if not conn:
            return
        
        try:
            cursor = conn.cursor()
            cursor.execute('''
                DELETE FROM user_sessions 
                WHERE expires_at < CURRENT_TIMESTAMP
            ''')
            conn.commit()
            cursor.close()
        except Exception as e:
            print(f"Error cleaning up sessions: {e}")
        finally:
            conn.close()

def login_required(f):
    """Decorator to require login for routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            if _wants_json_auth_error():
                return jsonify({
                    'error': 'Please log in again.',
                    'login_required': True,
                }), 401
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('auth.login'))
        
        # Validate session and load user
        user = load_current_user()
        if not user:
            session.clear()
            if _wants_json_auth_error():
                return jsonify({
                    'error': 'Your session has expired. Please log in again.',
                    'login_required': True,
                }), 401
            flash('Your session has expired. Please log in again.', 'error')
            return redirect(url_for('auth.login'))
        
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """Decorator to require admin privileges"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            if _wants_json_auth_error():
                return jsonify({
                    'error': 'Please log in again.',
                    'login_required': True,
                }), 401
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('auth.login'))
        
        user = load_current_user()
        if not user or user['role'] != 'super_admin':
            if _wants_json_auth_error():
                return jsonify({'error': 'Admin privileges required.'}), 403
            flash('Admin privileges required.', 'error')
            return redirect(url_for('main.dashboard'))
        
        return f(*args, **kwargs)
    return decorated_function

def load_current_user():
    """Load current user from session"""
    if 'user_id' not in session:
        return None
    
    user = UserManager.get_user_by_id(session['user_id'])
    if user:
        g.current_user = user
        return user
    
    return None

def get_current_user():
    """Get current user (use in templates)"""
    if hasattr(g, 'current_user'):
        return g.current_user
    return load_current_user()

def validate_password_strength(password):
    """Validate password strength"""
    if len(password) < 8:
        return False, "Password must be at least 8 characters long"
    
    if not any(c.isupper() for c in password):
        return False, "Password must contain at least one uppercase letter"
    
    if not any(c.islower() for c in password):
        return False, "Password must contain at least one lowercase letter"
    
    if not any(c.isdigit() for c in password):
        return False, "Password must contain at least one number"
    
    return True, "Password is valid"

def create_action_token(purpose, player_id=None, user_id=None, family_id=None,
                        payload=None, ttl_hours=72):
    """Single-use token for email approve/claim links. Returns the token string
    or None. The token identifies the REQUEST; the executing route must still
    verify the logged-in user is authorized for the action."""
    import json as _json
    conn = get_db_connection()
    if not conn:
        return None
    try:
        token = secrets.token_urlsafe(32)
        expires_at = datetime.utcnow() + timedelta(hours=ttl_hours)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO action_tokens (token, purpose, player_id, user_id, family_id, payload, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING token
        ''', (token, purpose, player_id, user_id, family_id,
              _json.dumps(payload or {}), expires_at))
        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        return row['token'] if row else None
    except Exception as e:
        conn.rollback()
        print(f"Error creating action token: {e}")
        return None
    finally:
        conn.close()

def peek_action_token(conn, token, expected_purpose=None):
    """Read a token without consuming it. Returns the row or None. Accepts a
    single purpose or a tuple of purposes."""
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM action_tokens WHERE token = %s', (token,))
    row = cursor.fetchone()
    cursor.close()
    if not row:
        return None
    if row['used_at'] is not None or row['expires_at'] < datetime.utcnow():
        return None
    if expected_purpose:
        allowed = expected_purpose if isinstance(expected_purpose, (tuple, list)) else (expected_purpose,)
        if row['purpose'] not in allowed:
            return None
    return dict(row)

def consume_action_token(conn, token, expected_purpose=None):
    """Atomically mark a token used and return its row, or None if invalid,
    expired, already used, or the wrong purpose. Caller commits."""
    allowed = None
    if expected_purpose:
        allowed = list(expected_purpose) if isinstance(expected_purpose, (tuple, list)) else [expected_purpose]
    cursor = conn.cursor()
    if allowed:
        cursor.execute('''
            UPDATE action_tokens SET used_at = CURRENT_TIMESTAMP
            WHERE token = %s AND used_at IS NULL AND expires_at > CURRENT_TIMESTAMP
              AND purpose = ANY(%s)
            RETURNING *
        ''', (token, allowed))
    else:
        cursor.execute('''
            UPDATE action_tokens SET used_at = CURRENT_TIMESTAMP
            WHERE token = %s AND used_at IS NULL AND expires_at > CURRENT_TIMESTAMP
            RETURNING *
        ''', (token,))
    row = cursor.fetchone()
    cursor.close()
    return dict(row) if row else None

# Simple wrapper functions for auth_routes.py
def validate_email(email):
    """Simple email validation wrapper"""
    return UserManager.validate_email_format(email)

def validate_password(password):
    """Simple password validation wrapper"""
    valid, _ = validate_password_strength(password)
    return valid


def create_password_reset_token(user_id, ttl_hours=1):
    """Issue a single-use password reset token. Returns token string or None."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        token = secrets.token_urlsafe(32)
        expires_at = datetime.utcnow() + timedelta(hours=ttl_hours)
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE password_reset_tokens SET used = TRUE
            WHERE user_id = %s AND used = FALSE AND expires_at > CURRENT_TIMESTAMP
        ''', (user_id,))
        cursor.execute('''
            INSERT INTO password_reset_tokens (user_id, token, expires_at, used)
            VALUES (%s, %s, %s, FALSE) RETURNING token
        ''', (user_id, token, expires_at))
        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        return row['token'] if isinstance(row, dict) else (row[0] if row else None)
    except Exception as e:
        conn.rollback()
        print(f"Error creating password reset token: {e}")
        return None
    finally:
        conn.close()


def peek_password_reset_token(conn, token):
    """Return reset token row if valid/unused/unexpired, else None."""
    cursor = conn.cursor()
    cursor.execute('''
        SELECT * FROM password_reset_tokens
        WHERE token = %s AND used = FALSE AND expires_at > CURRENT_TIMESTAMP
    ''', (token,))
    row = cursor.fetchone()
    cursor.close()
    return dict(row) if row else None


def consume_password_reset_token(conn, token):
    """Mark reset token used. Returns row or None. Caller commits."""
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE password_reset_tokens SET used = TRUE
        WHERE token = %s AND used = FALSE AND expires_at > CURRENT_TIMESTAMP
        RETURNING *
    ''', (token,))
    row = cursor.fetchone()
    cursor.close()
    return dict(row) if row else None

def create_verification_token(user_id):
    """Simple wrapper for creating verification tokens"""
    return UserManager.create_verification_token(user_id)

def verify_email_token(token):
    """Simple wrapper for verifying email tokens"""
    return UserManager.verify_email_token(token)

def resend_verification_email(email):
    """Simple wrapper for resending verification emails"""
    return UserManager.resend_verification_email(email) 