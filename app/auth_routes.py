from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, abort, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from .database import get_db_connection
from .auth import (
    validate_email, validate_password, 
    create_verification_token, verify_email_token, resend_verification_email,
    login_required, admin_required, load_current_user
)
from .email_utils import send_verification_email, send_registration_notification, APP_BASE_URL
from .identity import unique_family_slug, audit
import logging
from datetime import datetime

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')
logger = logging.getLogger(__name__)

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        try:
            email = request.form.get('email', '').strip().lower()
            password = request.form.get('password', '')
            
            if not email or not password:
                flash('Email and password are required.', 'error')
                return render_template('auth/login.html')

            conn = get_db_connection()
            cursor = conn.cursor()
            
            # Get user information (simplified for existing schema)
            cursor.execute('''
                SELECT id, first_name, last_name, family_name, password_hash, is_verified, is_approved
                FROM users WHERE email = %s
            ''', (email,))
            
            user = cursor.fetchone()
            
            if not user:
                logger.error(f"No user found for email: {email}")
                flash('Invalid email or password.', 'error')
                cursor.close()
                conn.close()
                return render_template('auth/login.html')

            # Handle RealDictCursor result (returns dict, not tuple)
            if isinstance(user, dict):
                user_id = user['id']
                first_name = user['first_name']
                last_name = user['last_name']
                family_name = user['family_name']
                password_hash = user['password_hash']
                is_verified = user['is_verified']
            else:
                user_id, first_name, last_name, family_name, password_hash, is_verified = user
                
            logger.info(f"User found: {first_name} {last_name}, Verified: {is_verified}, Hash type: {type(password_hash)}, Hash length: {len(password_hash) if password_hash else 'None'}")

            # Verify password - try both new and old hash methods
            password_valid = False
            
            # Try new Werkzeug method first
            try:
                password_valid = check_password_hash(password_hash, password)
                logger.info(f"Werkzeug password check result: {password_valid}")
            except Exception as e:
                logger.error(f"Werkzeug password check failed: {e}")
            
            # If that fails, try bcrypt (old method)
            if not password_valid and password_hash:
                try:
                    import bcrypt
                    password_valid = bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))
                    logger.info(f"Bcrypt password check result: {password_valid}")
                except Exception as e:
                    logger.info(f"Bcrypt password check failed: {e}")
            
            if not password_valid:
                logger.warning(f"Password verification failed for user: {email}")
                flash('Invalid email or password.', 'error')
                cursor.close()
                conn.close()
                return render_template('auth/login.html')

            # Check if email is verified
            if not is_verified:
                cursor.close()
                conn.close()
                flash('Please verify your email address before logging in. Check your email for the verification link, or request a new one below.', 'warning')
                return render_template('auth/login.html', show_resend=True, user_email=email)

            # Check if account is approved by admin
            if isinstance(user, dict):
                is_approved = user.get('is_approved', False)
            else:
                is_approved = False
            if not is_approved:
                cursor.close()
                conn.close()
                flash('Your account is pending admin approval. You will be notified when approved.', 'warning')
                return render_template('auth/login.html')

            # Update last login if column exists
            try:
                cursor.execute('''
                    UPDATE users SET last_login = NOW() WHERE id = %s
                ''', (user_id,))
                conn.commit()
            except Exception:
                # Ignore if last_login column doesn't exist
                pass
            
            cursor.close()
            conn.close()

            # Create session
            session.clear()  # Clear any existing session data
            session['user_id'] = int(user_id)  # Ensure user_id is an integer, not bytes
            session['user_name'] = f"{first_name} {last_name}"
            session['family_name'] = family_name
            session['is_authenticated'] = True
            session.permanent = True

            flash(f'Welcome back, {first_name}!', 'success')
            return redirect(url_for('main.dashboard'))

        except Exception as e:
            logger.error(f"Login error: {e}")
            flash('Login failed due to a system error. Please try again.', 'error')
            return render_template('auth/login.html')

    return render_template('auth/login.html')

def _register_context(conn):
    """Claim/invite context from session tokens: who is registering and where
    they land. Returns None for a plain signup."""
    from .auth import peek_action_token
    cursor = conn.cursor()
    try:
        claim_token = session.get('claim_token')
        if claim_token:
            row = peek_action_token(conn, claim_token, 'claim_profile')
            if row:
                cursor.execute('''
                    SELECT p.*, f.name AS home_family_name FROM players p
                    LEFT JOIN families f ON f.id = p.family_id
                    WHERE p.id = %s AND p.purged_at IS NULL
                      AND NOT EXISTS (SELECT 1 FROM users u WHERE u.player_id = p.id)
                ''', (row['player_id'],))
                player = cursor.fetchone()
                if player:
                    return {'kind': 'claim', 'token': claim_token, 'player': dict(player)}
            session.pop('claim_token', None)

        invite_token = session.get('invite_token')
        if invite_token:
            cursor.execute('''
                SELECT i.*, f.name AS invite_family_name FROM invitations i
                LEFT JOIN families f ON f.id = i.family_id
                WHERE i.token = %s AND i.status = 'sent' AND i.expires_at > CURRENT_TIMESTAMP
                  AND i.invite_type IN ('join_family', 'join_site')
            ''', (invite_token,))
            inv = cursor.fetchone()
            if inv:
                return {'kind': 'invite', 'token': invite_token, 'invitation': dict(inv)}
            session.pop('invite_token', None)
        return None
    finally:
        cursor.close()

def _register_prefill(ctx):
    if not ctx:
        return {}
    if ctx['kind'] == 'claim':
        p = ctx['player']
        return {
            'prefill_first_name': p.get('first_name'),
            'prefill_last_name': p.get('last_name'),
            'prefill_email': p.get('email'),
            'lock_family_name': p.get('home_family_name'),
            'invite_banner': f"You are claiming the player profile \"{p.get('display_name') or p.get('first_name')}\" in the {p.get('home_family_name')} family. Your game history is already attached to it.",
        }
    inv = ctx['invitation']
    out = {'prefill_email': inv.get('email')}
    if inv['invite_type'] == 'join_family' and inv.get('family_id'):
        out['lock_family_name'] = inv.get('invite_family_name')
        out['invite_banner'] = f"You were invited to join the {inv.get('invite_family_name')} family. Finish signing up and you are in."
    else:
        out['invite_banner'] = 'You were invited to DiFede Games. Sign up to start your own family group.'
    return out

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    """Registration page and handler. Supports three entries: plain signup
    (own new family, leads it), claim invite (binds to an existing player and
    their family), and email invite (joins the inviting family)."""
    if request.method == 'GET':
        if 'user_id' in session:
            return redirect(url_for('main.dashboard'))
        conn = get_db_connection()
        try:
            ctx = _register_context(conn)
        finally:
            conn.close()
        return render_template('auth/register.html', **_register_prefill(ctx))

    conn = None
    try:
        first_name = request.form.get('first_name', '').strip()
        last_name = request.form.get('last_name', '').strip()
        family_name = request.form.get('family_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        conn = get_db_connection()
        ctx = _register_context(conn)
        prefill = _register_prefill(ctx)
        joins_existing_family = ctx is not None and (
            ctx['kind'] == 'claim'
            or (ctx['kind'] == 'invite' and ctx['invitation']['invite_type'] == 'join_family'
                and ctx['invitation'].get('family_id')))

        required = [first_name, last_name, email, password]
        if not joins_existing_family:
            required.append(family_name)
        if not all(required):
            flash('All fields are required.', 'error')
            return render_template('auth/register.html', **prefill)
        if not validate_email(email):
            flash('Please enter a valid email address.', 'error')
            return render_template('auth/register.html', **prefill)
        if password != confirm_password:
            flash('Passwords do not match.', 'error')
            return render_template('auth/register.html', **prefill)
        if not validate_password(password):
            flash('Password must be at least 8 characters long and contain at least one uppercase letter, one lowercase letter, one digit, and one special character.', 'error')
            return render_template('auth/register.html', **prefill)

        cursor = conn.cursor()
        cursor.execute('SELECT id, archived_at FROM users WHERE lower(email) = %s', (email,))
        existing = cursor.fetchone()
        if existing:
            if existing.get('archived_at'):
                flash('An account with this email exists but was archived. Contact the administrator to reinstate it.', 'error')
            else:
                flash('An account with this email already exists.', 'error')
            cursor.close()
            return render_template('auth/register.html', **prefill)

        # Where does this person land?
        name_taken = False
        is_new_family = False
        invited_by_email = None
        if ctx and ctx['kind'] == 'claim':
            family_id = ctx['player']['family_id']
            family_name = ctx['player'].get('home_family_name') or family_name or 'Family'
            invited_by_email = ctx['player'].get('email')
        elif joins_existing_family:
            family_id = ctx['invitation']['family_id']
            family_name = ctx['invitation'].get('invite_family_name') or family_name or 'Family'
            invited_by_email = ctx['invitation'].get('email')
        else:
            # Every plain signup gets their OWN family and leads it. Nobody is
            # silently added to an existing family because the name matches;
            # joining someone else's family goes through the directory.
            if ctx and ctx['kind'] == 'invite':
                invited_by_email = ctx['invitation'].get('email')
            cursor.execute('SELECT COUNT(*) AS n FROM families WHERE lower(name) = lower(%s)', (family_name,))
            name_taken = cursor.fetchone()['n'] > 0
            cursor.execute(
                'INSERT INTO families (name, slug) VALUES (%s, %s) RETURNING id',
                (family_name, unique_family_slug(conn, family_name)))
            family_id = cursor.fetchone()['id']
            is_new_family = True

        # Possession of an emailed token to this same address proves the email,
        # and being invited by a lead removes the manual-approval step.
        auto_trust = ctx is not None and invited_by_email is not None \
            and invited_by_email.strip().lower() == email

        password_hash = generate_password_hash(password)
        cursor.execute('''
            INSERT INTO users (first_name, last_name, family_name, email, password_hash,
                               is_verified, is_approved, family_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            RETURNING id
        ''', (first_name, last_name, family_name, email, password_hash,
              auto_trust, auto_trust or ctx is not None, family_id))
        user_result = cursor.fetchone()
        if not user_result:
            flash('Registration failed. Please try again.', 'error')
            cursor.close()
            return render_template('auth/register.html', **prefill)
        user_id = user_result['id']

        if ctx and ctx['kind'] == 'claim':
            # Bind the existing person instead of creating a new one; their
            # entire game history comes with them.
            from .auth import consume_action_token
            player_id = ctx['player']['id']
            consume_action_token(conn, ctx['token'], 'claim_profile')
            cursor.execute('''
                UPDATE players SET email = %s, email_verified = %s,
                    archived_at = NULL, archived_by_user_id = NULL, archive_reason = NULL
                WHERE id = %s
            ''', (email, auto_trust, player_id))
            cursor.execute('UPDATE users SET player_id = %s WHERE id = %s', (player_id, user_id))
            cursor.execute('''
                INSERT INTO player_family_memberships (player_id, family_id, is_primary, status, role)
                VALUES (%s, %s, TRUE, 'active', 'member')
                ON CONFLICT (player_id, family_id) DO UPDATE SET status = 'active'
            ''', (player_id, family_id))
            cursor.execute('''
                UPDATE invitations SET status = 'accepted', accepted_at = CURRENT_TIMESTAMP
                WHERE token = %s AND status = 'sent'
            ''', (ctx['token'],))
            audit(conn, user_id, 'profile_claimed_at_registration', 'players', player_id)
            session.pop('claim_token', None)
        else:
            cursor.execute('''
                INSERT INTO players (first_name, last_name, display_name, created_by_user_id, family_id, email)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
            ''', (first_name, last_name, first_name, user_id, family_id, email))
            player_id = cursor.fetchone()['id']
            cursor.execute('UPDATE users SET player_id = %s WHERE id = %s', (player_id, user_id))
            cursor.execute('''
                INSERT INTO player_family_memberships (player_id, family_id, is_primary, status, role)
                VALUES (%s, %s, TRUE, 'active', %s)
                ON CONFLICT (player_id, family_id) DO NOTHING
            ''', (player_id, family_id, 'lead' if is_new_family else 'member'))
            if is_new_family:
                cursor.execute('UPDATE families SET lead_user_id = %s, created_by_user_id = %s WHERE id = %s',
                               (user_id, user_id, family_id))
            if ctx and ctx['kind'] == 'invite':
                cursor.execute('''
                    UPDATE invitations SET status = 'accepted', accepted_at = CURRENT_TIMESTAMP
                    WHERE token = %s AND status = 'sent'
                ''', (ctx['token'],))
                audit(conn, user_id, 'invite_accepted_at_registration', 'invitations', ctx['invitation']['id'])
                session.pop('invite_token', None)
        conn.commit()

        if auto_trust:
            try:
                send_registration_notification(first_name, last_name, email, family_name)
            except Exception as notify_error:
                logger.error(f"Admin notification error: {notify_error}")
            cursor.close()
            flash('Your account is ready. Sign in to get started.', 'success')
            return redirect(url_for('auth.login'))

        try:
            token = create_verification_token(user_id)
            verification_link = f"{APP_BASE_URL}/auth/verify/{token}"
            email_sent = False
            try:
                email_sent = send_verification_email(email, first_name, verification_link)
            except Exception as email_error:
                logger.error(f"Email sending error: {email_error}")
            try:
                send_registration_notification(first_name, last_name, email, family_name)
            except Exception as notify_error:
                logger.error(f"Admin notification error: {notify_error}")
            cursor.close()

            if name_taken:
                flash(f'Note: another family already uses the name "{family_name}". You now lead your own separate "{family_name}" family. If you meant to join the existing one, use the Directory after logging in to send a join request.', 'info')
            if email_sent:
                flash(f'Registration successful! A verification email has been sent to {email}. Please check your email and click the verification link to activate your account.', 'success')
            else:
                flash(f'Account created successfully, but we had trouble sending the verification email. Please contact the administrator at joe_71@yahoo.com to manually verify your account.', 'warning')
            return redirect(url_for('auth.login'))
        except Exception as token_error:
            logger.error(f"Token creation error: {token_error}")
            cursor.close()
            flash('Account created but verification setup failed. Please contact the administrator.', 'error')
            return redirect(url_for('auth.login'))

    except Exception as e:
        logger.error(f"Registration error: {e}")
        import traceback
        traceback.print_exc()
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        flash('Registration failed due to a system error. Please try again or contact the administrator.', 'error')
        return render_template('auth/register.html')
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

@auth_bp.route('/logout')
def logout():
    """Logout handler"""
    # Clear Flask session
    session.clear()
    flash('You have been logged out successfully.', 'info')
    return redirect(url_for('auth.login'))

@auth_bp.route('/profile')
def profile():
    """User profile page"""
    if 'user_id' not in session:
        flash('Please log in to access this page.', 'error')
        return redirect(url_for('auth.login'))
    
    user_id = session['user_id']
    
    conn = get_db_connection()
    if not conn:
        flash('Database connection error.', 'error')
        return redirect(url_for('main.dashboard'))
    
    try:
        cursor = conn.cursor()
        
        # Get user information with proper dictionary handling
        cursor.execute('''
            SELECT u.id, u.email, u.first_name, u.last_name, u.family_name,
                   u.address, u.city, u.state, u.zipcode, u.phone_number,
                   u.role, u.is_verified, u.last_login, u.created_at,
                   ud.achievements, ud.custom_settings
            FROM users u
            LEFT JOIN user_details ud ON u.id = ud.user_id
            WHERE u.id = %s
        ''', (user_id,))
        
        user_data = cursor.fetchone()
        
        if not user_data:
            flash('User not found.', 'error')
            cursor.close()
            conn.close()
            return redirect(url_for('main.dashboard'))
        
        # Handle RealDictCursor result properly
        if isinstance(user_data, dict):
            user = user_data
        else:
            # Convert tuple to dict if needed (shouldn't happen with RealDictCursor)
            columns = [desc[0] for desc in cursor.description]
            user = dict(zip(columns, user_data))
        
        cursor.close()
        conn.close()
        
        return render_template('auth/profile.html', user=user)
        
    except Exception as e:
        logger.error(f"Profile error: {e}")
        flash('Error loading profile.', 'error')
        if conn:
            conn.close()
        return redirect(url_for('main.dashboard'))

@auth_bp.route('/profile/update', methods=['POST'])
def update_profile():
    """Update user profile information"""
    if 'user_id' not in session:
        flash('Please log in to access this page.', 'error')
        return redirect(url_for('auth.login'))
    
    user_id = session['user_id']
    
    # Get form data
    first_name = request.form.get('first_name', '').strip()
    last_name = request.form.get('last_name', '').strip()
    family_name = request.form.get('family_name', '').strip()
    address = request.form.get('address', '').strip()
    city = request.form.get('city', '').strip()
    state = request.form.get('state', '').strip()
    zipcode = request.form.get('zipcode', '').strip()
    phone_number = request.form.get('phone_number', '').strip()
    
    # Validation
    if not all([first_name, last_name, family_name]):
        flash('First name, last name, and family name are required.', 'error')
        return redirect(url_for('auth.profile'))
    
    conn = get_db_connection()
    if not conn:
        flash('Database connection error.', 'error')
        return redirect(url_for('auth.profile'))
    
    try:
        cursor = conn.cursor()
        
        # Update user information
        cursor.execute('''
            UPDATE users 
            SET first_name = %s, last_name = %s, family_name = %s,
                address = %s, city = %s, state = %s, zipcode = %s,
                phone_number = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        ''', (first_name, last_name, family_name, address, city, state, 
              zipcode, phone_number, user_id))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        # Update session data
        session['user_name'] = f"{first_name} {last_name}"
        session['family_name'] = family_name
        
        flash('Profile updated successfully!', 'success')
        return redirect(url_for('auth.profile'))
        
    except Exception as e:
        logger.error(f"Profile update error: {e}")
        flash('Error updating profile.', 'error')
        if conn:
            conn.rollback()
            conn.close()
        return redirect(url_for('auth.profile'))

@auth_bp.route('/change-password', methods=['GET', 'POST'])
def change_password():
    """Change user password"""
    if 'user_id' not in session:
        flash('Please log in to access this page.', 'error')
        return redirect(url_for('auth.login'))
    
    if request.method == 'GET':
        return render_template('auth/change_password.html')
    
    user_id = session['user_id']
    current_password = request.form.get('current_password', '')
    new_password = request.form.get('new_password', '')
    confirm_password = request.form.get('confirm_password', '')
    
    # Validation
    if not all([current_password, new_password, confirm_password]):
        flash('All fields are required.', 'error')
        return render_template('auth/change_password.html')
    
    if new_password != confirm_password:
        flash('New passwords do not match.', 'error')
        return render_template('auth/change_password.html')
    
    if not validate_password(new_password):
        flash('Password must be at least 8 characters long and contain at least one uppercase letter, one lowercase letter, one digit, and one special character.', 'error')
        return render_template('auth/change_password.html')
    
    conn = get_db_connection()
    if not conn:
        flash('Database connection error.', 'error')
        return render_template('auth/change_password.html')
    
    try:
        cursor = conn.cursor()
        
        # Get current password hash
        cursor.execute('SELECT password_hash FROM users WHERE id = %s', (user_id,))
        result = cursor.fetchone()
        
        if not result:
            flash('User not found.', 'error')
            cursor.close()
            conn.close()
            return render_template('auth/change_password.html')
        
        # Handle RealDictCursor result
        if isinstance(result, dict):
            current_hash = result['password_hash']
        else:
            current_hash = result[0]
        
        # Verify current password
        if not check_password_hash(current_hash, current_password):
            flash('Current password is incorrect.', 'error')
            cursor.close()
            conn.close()
            return render_template('auth/change_password.html')
        
        # Generate new password hash
        new_hash = generate_password_hash(new_password)
        
        # Update password
        cursor.execute('''
            UPDATE users 
            SET password_hash = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        ''', (new_hash, user_id))
        
        conn.commit()
        cursor.close()
        conn.close()
        
        flash('Password changed successfully!', 'success')
        return redirect(url_for('auth.profile'))
        
    except Exception as e:
        logger.error(f"Password change error: {e}")
        flash('Error changing password.', 'error')
        if conn:
            conn.rollback()
            conn.close()
        return render_template('auth/change_password.html')

@auth_bp.route('/verify/<token>')
def verify_email(token):
    """Email verification handler"""
    try:
        success, result = verify_email_token(token)
        
        if success:
            if result == "already_verified":
                flash('Your account is already verified. You can log in now.', 'info')
            else:
                from app.email_utils import send_welcome_email, send_approval_needed_notification
                
                send_welcome_email(result['email'], result['first_name'])
                
                try:
                    send_approval_needed_notification(
                        result.get('first_name', ''),
                        result.get('last_name', ''),
                        result.get('email', ''),
                        result.get('family_name', ''))
                except Exception as notify_err:
                    logger.error(f"Failed to send approval-needed notification: {notify_err}")
                
                try:
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute('''
                        INSERT INTO notifications (user_id, type, title, message, data)
                        SELECT id, 'user_pending_approval',
                               'New User Needs Approval',
                               %s,
                               jsonb_build_object('user_email', %s, 'user_name', %s)
                        FROM users WHERE role = 'super_admin'
                    ''', (
                        f"{result.get('first_name', '')} {result.get('last_name', '')} ({result.get('email', '')}) has verified their email and needs approval.",
                        result.get('email', ''),
                        f"{result.get('first_name', '')} {result.get('last_name', '')}"
                    ))
                    conn.commit()
                    cursor.close()
                    conn.close()
                except Exception as db_err:
                    logger.error(f"Failed to create admin notification: {db_err}")
                
                flash(f'Email verified successfully! Welcome {result["first_name"]}. Your account is pending admin approval. You will receive an email once approved.', 'success')
        else:
            if result == "expired":
                flash('This verification link has expired. Please contact support for a new verification email.', 'error')
            elif result == "invalid":
                flash('Invalid verification link. Please check your email or contact support.', 'error')
            else:
                flash('Email verification failed. Please try again or contact support.', 'error')
                
    except Exception as e:
        print(f"Email verification error: {e}")
        flash('An error occurred during verification. Please contact support.', 'error')
    
    return redirect(url_for('auth.login'))

@auth_bp.route('/resend-verification', methods=['GET', 'POST'])
def resend_verification():
    """Resend verification email"""
    if request.method == 'GET':
        return render_template('auth/resend_verification.html')
    
    email = request.form.get('email', '').strip().lower()
    if not email:
        flash('Please enter your email address.', 'error')
        return render_template('auth/resend_verification.html')
    
    try:
        success, result = resend_verification_email(email)
        
        if success:
            # Send new verification email
            verification_link = f"{APP_BASE_URL}/auth/verify/{result['token']}"
            
            email_sent = send_verification_email(result['email'], result['first_name'], verification_link)
            
            if email_sent:
                flash('Verification email sent! Please check your email and click the verification link.', 'success')
            else:
                flash('Failed to send verification email. Please contact support.', 'error')
        else:
            if result == "User not found":
                flash('No account found with that email address.', 'error')
            elif result == "User already verified":
                flash('Your account is already verified. You can log in now.', 'info')
            else:
                flash('Failed to resend verification email. Please contact support.', 'error')
                
    except Exception as e:
        logger.error(f"Resend verification error: {e}")
        flash('An error occurred. Please try again or contact support.', 'error')
    
    return render_template('auth/resend_verification.html')

@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    """Forgot password page and handler"""
    if request.method == 'GET':
        return render_template('auth/forgot_password.html')
    
    email = request.form.get('email', '').strip().lower()
    if not email:
        flash('Please enter your email address.', 'error')
        return render_template('auth/forgot_password.html')
    
    # TODO: Implement password reset logic
    flash('Password reset functionality is not yet implemented. Please contact administrator.', 'info')
    return redirect(url_for('auth.login'))

@auth_bp.route('/admin/verify-user/<int:user_id>')
@login_required
def admin_verify_user(user_id):
    """Admin function to verify a user"""
    current_user = load_current_user()
    if not current_user or current_user.get('role') != 'super_admin':
        flash('Admin privileges required.', 'error')
        return redirect(url_for('main.dashboard'))

    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE users 
                SET is_verified = TRUE 
                WHERE id = %s
            ''', (user_id,))
            conn.commit()
            cursor.close()
            flash('User verified successfully.', 'success')
        except Exception as e:
            conn.rollback()
            flash('Error verifying user.', 'error')
            logger.error(f"Error verifying user: {e}")
        finally:
            conn.close()
    
    return redirect(url_for('auth.admin_users'))

@auth_bp.route('/admin/approve-user/<int:user_id>', methods=['POST'])
@login_required
def admin_approve_user(user_id):
    """Admin function to approve a user"""
    current_user = load_current_user()
    if not current_user or current_user.get('role') != 'super_admin':
        return jsonify({'error': 'Admin privileges required'}), 403

    conn = get_db_connection()
    if conn:
        try:
            import psycopg2.extras
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute('SELECT email, first_name FROM users WHERE id = %s', (user_id,))
            user = cursor.fetchone()
            if not user:
                return jsonify({'error': 'User not found'}), 404
            
            cursor.execute('UPDATE users SET is_approved = TRUE WHERE id = %s', (user_id,))
            conn.commit()
            cursor.close()
            
            try:
                from app.email_utils import send_approval_email
                send_approval_email(user['email'], user['first_name'])
            except Exception as e:
                logger.error(f"Failed to send approval email: {e}")
            
            return jsonify({'success': True})
        except Exception as e:
            conn.rollback()
            logger.error(f"Error approving user: {e}")
            return jsonify({'error': 'Failed to approve user'}), 500
        finally:
            conn.close()
    return jsonify({'error': 'Database connection failed'}), 500

@auth_bp.route('/admin/users')
@login_required
def admin_users():
    """Admin page to manage users"""
    current_user = load_current_user()
    if not current_user or current_user.get('role') != 'super_admin':
        flash('Admin privileges required.', 'error')
        return redirect(url_for('main.dashboard'))

    try:
        import psycopg2.extras
    except ImportError:
        psycopg2 = None
    
    conn = get_db_connection()
    users = []
    
    if conn:
        try:
            cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cursor.execute('''
                SELECT u.id, u.email, u.first_name, u.last_name, u.family_name,
                       u.role, u.is_verified, u.is_active, u.is_approved, u.created_at, u.last_login,
                       u.family_id,
                       (SELECT COUNT(*) FROM players p WHERE p.family_id = u.family_id) as family_member_count
                FROM users u
                ORDER BY u.created_at DESC
            ''')
            users = cursor.fetchall()
            cursor.close()
        except Exception as e:
            flash('Error loading users.', 'error')
            logger.error(f"Error loading users: {e}")
        finally:
            conn.close()
    
    return render_template('auth/admin_users.html', users=users)
