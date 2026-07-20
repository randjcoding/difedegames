import os
from email.message import EmailMessage
import ssl
import smtplib
from typing import List
import logging
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Set up simple logger for email operations
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Email configuration for DiFede Games
EMAIL_SENDER = 'randjcoding@gmail.com'
EMAIL_DISPLAY = 'TheDiFedeApp_games@gmail.com'
EMAIL_PASSWORD = os.getenv('EMAIL_PASSWORD')
EMAIL_RECEIVERS = ['joe_71@yahoo.com']  # Only joe_71@yahoo.com for notifications

def send_verification_email(user_email: str, first_name: str, verification_link: str) -> bool:
    """
    Send email verification link to new users.
    
    Args:
        user_email (str): User's email address  
        first_name (str): User's first name
        verification_link (str): Full verification URL
        
    Returns:
        bool: True if email sent successfully, False otherwise
    """
    subject = 'Verify Your DiFede Games Account'
    body = f"""
    Hi {first_name},

    Welcome to the DiFede Games! We're excited to have you join our family gaming community.

    To complete your registration and start tracking your family's games, please verify your email address by clicking the link below:

    {verification_link}

    This verification link will expire in 24 hours for security purposes.

    Once verified, you'll be able to:
    • Track your family's Five Crowns games
    • View game statistics and leaderboards
    • Manage your family's gaming history
    • And much more!

    If you didn't create this account, please ignore this email.

    Best regards,
    The DiFede Games Team

    ---
    Need help? Contact us at {EMAIL_DISPLAY}
    """

    # Create the email message
    em = EmailMessage()
    em['From'] = f'DiFede Games <{EMAIL_DISPLAY}>'
    em['Reply-To'] = EMAIL_DISPLAY
    em['To'] = user_email
    em['Subject'] = subject
    em.set_content(body)

    # Create SSL context for secure connection
    context = ssl.create_default_context()

    try:
        # Connect to Gmail's SMTP server and send the email
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=context) as smtp:
            smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
            smtp.sendmail(EMAIL_SENDER, [user_email], em.as_string())
            logger.info(f'Verification email sent to {user_email}')
            return True
    except Exception as e:
        logger.error(f'Failed to send verification email to {user_email}: {str(e)}', exc_info=True)
        return False

def send_registration_notification(first_name: str, last_name: str, email: str, family_name: str) -> None:
    """
    Send a notification email to admins when a new user registers.
    
    Args:
        first_name (str): User's first name
        last_name (str): User's last name
        email (str): User's email address
        family_name (str): User's family name
    """
    subject = f'(DO NOT REPLY) New User Registration - {first_name} {last_name}'
    body = f"""
    New user registration for DiFede Games:

    Name: {first_name} {last_name}
    Email: {email}
    Family: {family_name}

    The user has been sent a verification email and will need to verify their account before they can log in.

    Administrator Dashboard: http://192.168.68.72:5002/auth/admin/users
    """

    # Create the email message
    em = EmailMessage()
    em['From'] = f'DiFede Games <{EMAIL_DISPLAY}>'
    em['Reply-To'] = EMAIL_DISPLAY
    em['To'] = ', '.join(EMAIL_RECEIVERS)
    em['Subject'] = subject
    em.set_content(body)

    # Create SSL context for secure connection
    context = ssl.create_default_context()

    try:
        # Connect to Gmail's SMTP server and send the email
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=context) as smtp:
            smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
            smtp.sendmail(EMAIL_SENDER, EMAIL_RECEIVERS, em.as_string())
            logger.info(f'Registration notification sent for {first_name} {last_name} ({email})')
    except Exception as e:
        logger.error(f'Failed to send registration notification for {email}: {str(e)}', exc_info=True) 

 

def send_welcome_email(user_email: str, first_name: str) -> bool:
    """
    Send welcome email after successful verification.
    
    Args:
        user_email (str): User's email address
        first_name (str): User's first name
        
    Returns:
        bool: True if email sent successfully, False otherwise
    """
    subject = "Welcome to DiFede Games!"
    body = f"""
    Hi {first_name},

    Congratulations! Your email has been verified and your DiFede Games account is now active.

    You can now log in and start tracking your family's games:
    http://192.168.68.72:5002/auth/login

    What you can do now:
    • Start tracking Five Crowns games
    • View family game statistics
    • Manage your game history
    • Compete on the family leaderboard

    Happy gaming!

    Best regards,
    The DiFede Games Team
    """
    return send_email(user_email, subject, body)

def send_verification_expired_email(user_email: str, first_name: str) -> bool:
    """
    Send email when verification link has expired.
    
    Args:
        user_email (str): User's email address
        first_name (str): User's first name
        
    Returns:
        bool: True if email sent successfully, False otherwise
    """
    subject = "Your Verification Link Has Expired"
    body = f"""
    Hi {first_name},

    Your email verification link for DiFede Games has expired.

    For security reasons, verification links are only valid for 24 hours. To complete your registration, please contact the administrator to resend a verification email.

    Contact: {EMAIL_DISPLAY}

    We apologize for any inconvenience.

    Best regards,
    The DiFede Games Team
    """
    return send_email(user_email, subject, body)

def send_approval_email(user_email: str, first_name: str) -> bool:
    subject = "Your DiFede Account Has Been Approved!"
    body = f"""
    Hi {first_name},

    Great news! Your DiFede Games account has been approved by the administrator.

    You can now log in and start playing:
    http://192.168.68.72:5002/auth/login

    Happy gaming!

    Best regards,
    The DiFede Games Team
    """
    return send_email(user_email, subject, body)


def send_approval_needed_notification(first_name: str, last_name: str, email: str, family_name: str) -> None:
    subject = f'[ACTION REQUIRED] New User Needs Approval - {first_name} {last_name}'
    body = f"""
    A new user has registered and verified their email. They need admin approval to log in:

    Name: {first_name} {last_name}
    Email: {email}
    Family: {family_name}

    Please log in to approve or deny this user:
    http://192.168.68.72:5002/auth/admin/users

    - DiFede Games Admin System
    """

    em = EmailMessage()
    em['From'] = f'DiFede Games <{EMAIL_DISPLAY}>'
    em['Reply-To'] = EMAIL_DISPLAY
    em['To'] = ', '.join(EMAIL_RECEIVERS)
    em['Subject'] = subject
    em.set_content(body)

    context = ssl.create_default_context()

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=context) as smtp:
            smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
            smtp.sendmail(EMAIL_SENDER, EMAIL_RECEIVERS, em.as_string())
            logger.info(f'Approval needed notification sent for {first_name} {last_name} ({email})')
    except Exception as e:
        logger.error(f'Failed to send approval notification for {email}: {str(e)}', exc_info=True)


def send_alliance_request_email(user_email: str, first_name: str, from_family: str, from_user: str) -> bool:
    subject = f"Crew Up Request from the {from_family} Family!"
    body = f"""
    Hey {first_name}!

    {from_user} from the {from_family} family wants to crew up for game nights!

    When you accept, both families can invite each other's players to games
    and compete on shared leaderboards.

    Log in to review and accept the request:
    http://192.168.68.72:5002/dashboard

    Game on!

    - The DiFede Games Team
    """
    return send_email(user_email, subject, body)


def send_alliance_accepted_email(user_email: str, first_name: str, accepted_family: str) -> bool:
    subject = f"The {accepted_family} Family Accepted Your Crew Up!"
    body = f"""
    Hey {first_name}!

    Great news! The {accepted_family} family has accepted your Crew Up request!

    You can now invite their players to your games and vice versa. Time to shuffle up and deal!

    Log in to start playing:
    http://192.168.68.72:5002/dashboard

    Game on!

    - The DiFede Games Team
    """
    return send_email(user_email, subject, body)


def send_email(to_email: str, subject: str, body: str) -> bool:
    """
    Generic function to send emails for DiFede Games
    
    Args:
        to_email (str): Recipient email address
        subject (str): Email subject
        body (str): Email body content
        
    Returns:
        bool: True if email sent successfully, False otherwise
    """
    # Create the email message
    em = EmailMessage()
    em['From'] = f'DiFede Games <{EMAIL_DISPLAY}>'
    em['Reply-To'] = EMAIL_DISPLAY
    em['To'] = to_email
    em['Subject'] = subject
    em.set_content(body)

    # Create SSL context for secure connection
    context = ssl.create_default_context()

    try:
        # Connect to Gmail's SMTP server and send the email
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, context=context) as smtp:
            smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
            smtp.sendmail(EMAIL_SENDER, [to_email], em.as_string())
            logger.info(f'Email sent to {to_email}: {subject}')
            return True
    except Exception as e:
        logger.error(f'Failed to send email to {to_email}: {str(e)}', exc_info=True)
        return False 