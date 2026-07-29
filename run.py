# eventlet + Redis SocketIO message_queue requires monkey-patching BEFORE
# other imports (especially redis / socket libs). Without this, scaled web
# workers eventually reject socket connections and can wedge HTTP too.
import eventlet

eventlet.monkey_patch()

from app import create_app, socketio


app = create_app()

if __name__ == "__main__":
    import os

    # For local network access - listen on all interfaces
    # Disable debug mode when running as a service (set DEBUG=false in environment)
    debug_mode = os.environ.get("DEBUG", "false").lower() == "true"
    socketio.run(app, host="0.0.0.0", port=5002, debug=debug_mode, allow_unsafe_werkzeug=True)
