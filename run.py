from app import create_app, socketio




app = create_app()

if __name__ == '__main__':
    import os
    # For local network access - listen on all interfaces
    # Disable debug mode when running as a service (set DEBUG=false in environment)
    debug_mode = os.environ.get('DEBUG', 'false').lower() == 'true'
    socketio.run(app, host='0.0.0.0', port=5002, debug=debug_mode, allow_unsafe_werkzeug=True)
    # For specific IP (current server IP: 192.168.68.72)
    # socketio.run(app, host='192.168.68.72', port=5001, debug=True, allow_unsafe_werkzeug=True)
    
    # For localhost/testing only (commented out)
    # socketio.run(app, host='127.0.0.1', port=5001, debug=True, allow_unsafe_werkzeug=True) 