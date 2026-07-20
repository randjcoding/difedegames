#!/usr/bin/env python3
"""
Emergency fix script to restore super user login access
"""
import sys
import os
from werkzeug.security import generate_password_hash

# Add the project directory to Python path
sys.path.insert(0, os.path.dirname(__file__))

from app.database import get_db_connection

def fix_super_user():
    """Fix the super user account to allow login"""
    
    # Get the email to fix (change this to your super user email)
    super_user_email = input("Enter your super user email: ").strip().lower()
    if not super_user_email:
        print("No email provided, using default: joe_71@yahoo.com")
        super_user_email = "joe_71@yahoo.com"
    
    # Get new password
    new_password = input("Enter new password for super user: ").strip()
    if not new_password:
        print("No password provided, using default: admin123")
        new_password = "admin123"
    
    conn = get_db_connection()
    if not conn:
        print("❌ Could not connect to database!")
        return False
    
    try:
        cursor = conn.cursor()
        
        # Check if user exists
        cursor.execute("SELECT id, email, first_name, is_verified FROM users WHERE email = %s", (super_user_email,))
        user = cursor.fetchone()
        
        if not user:
            print(f"❌ No user found with email: {super_user_email}")
            return False
        
        # Handle RealDictCursor result (returns dict, not tuple)
        if isinstance(user, dict):
            user_id = user['id']
            email = user['email']
            first_name = user['first_name']
            is_verified = user['is_verified']
        else:
            user_id, email, first_name, is_verified = user
            
        print(f"✅ Found user: {first_name} ({email})")
        print(f"   Current verification status: {is_verified}")
        print(f"   User ID: {user_id}")
        
        # Generate new password hash using Werkzeug (current method)
        password_hash = generate_password_hash(new_password)
        
        # Update user: set verified=True and update password
        cursor.execute("""
            UPDATE users 
            SET is_verified = TRUE, 
                password_hash = %s,
                updated_at = NOW()
            WHERE id = %s
        """, (password_hash, user_id))
        
        conn.commit()
        print(f"✅ Successfully updated user {first_name}!")
        print(f"   - Email verification: ✅ TRUE")
        print(f"   - Password: ✅ Updated")
        print(f"   - Hash method: Werkzeug")
        
        cursor.close()
        conn.close()
        
        print(f"\n🎉 SUCCESS! You can now login with:")
        print(f"   Email: {super_user_email}")
        print(f"   Password: {new_password}")
        
        return True
        
    except Exception as e:
        print(f"❌ Error fixing user: {e}")
        conn.rollback()
        return False
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    print("🔧 DiFede App Emergency Login Fix")
    print("=" * 40)
    
    success = fix_super_user()
    
    if success:
        print("\n✅ Login fix completed successfully!")
        print("You should now be able to log in to your app.")
    else:
        print("\n❌ Login fix failed!")
        print("Please check the error messages above.") 