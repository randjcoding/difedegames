from datetime import datetime
from app import db

class Player(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(50), nullable=False)
    last_name = db.Column(db.String(50), nullable=False)
    display_name = db.Column(db.String(50))
    
    def get_display_name(self):
        return self.display_name if self.display_name else f"{self.first_name} {self.last_name}"

class Game(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)
    min_players = db.Column(db.Integer, nullable=False)
    max_players = db.Column(db.Integer, nullable=False)
    
class ActiveGame(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    game_id = db.Column(db.Integer, db.ForeignKey('game.id'), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    is_complete = db.Column(db.Boolean, default=False)
    
    game = db.relationship('Game', backref='active_games')
    
class ActiveGamePlayer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    active_game_id = db.Column(db.Integer, db.ForeignKey('active_game.id'), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey('player.id'), nullable=False)
    
    active_game = db.relationship('ActiveGame', backref='players')
    player = db.relationship('Player', backref='games')

class FiveCrownsScore(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    active_game_id = db.Column(db.Integer, db.ForeignKey('active_game.id'), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey('player.id'), nullable=False)
    round_number = db.Column(db.Integer, nullable=False)  # 3-13 (representing 3 through King)
    score = db.Column(db.Integer, nullable=False)
    
    active_game = db.relationship('ActiveGame', backref='scores')
    player = db.relationship('Player', backref='five_crowns_scores') 