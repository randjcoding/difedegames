from flask import Blueprint
from flask_socketio import emit, join_room, leave_room
from app import socketio, get_db_connection

events = Blueprint('events', __name__)

@socketio.on('connect')
def handle_connect():
    print('Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    print('Client disconnected')

@socketio.on('join_game')
def handle_join_game(data):
    game_id = data.get('game_id')
    if game_id:
        room = f'game_{game_id}'
        join_room(room)
        print(f'Client joined room {room}')

@socketio.on('leave_game')
def handle_leave_game(data):
    game_id = data.get('game_id')
    if game_id:
        room = f'game_{game_id}'
        leave_room(room)
        print(f'Client left room {room}')

def broadcast_score_update(game_id, player_id, round_number, score):
    socketio.emit('score_update', {
        'game_id': game_id,
        'player_id': player_id,
        'round_number': round_number,
        'score': score
    }, namespace='/', to=f'game_{game_id}')

def broadcast_game_completed(game_id, summary):
    socketio.emit('game_completed', {
        'game_id': game_id,
        'summary': summary
    }, namespace='/', to=f'game_{game_id}')

def broadcast_game_paused(game_id):
    socketio.emit('game_paused', {
        'game_id': game_id
    }, namespace='/', to=f'game_{game_id}')

def broadcast_game_resumed(game_id):
    socketio.emit('game_resumed', {
        'game_id': game_id
    }, namespace='/', to=f'game_{game_id}')
