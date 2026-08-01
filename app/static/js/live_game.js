/* Live score sync for active games: Socket.IO + poll backup.
   Keeps other screens updated so players do not overwrite each other. */
(function (window, $) {
  'use strict';

  var DEFAULT_POLL_MS = 5000;

  function cellSelector(playerId, roundNumber) {
    return '.score-cell[data-player="' + playerId + '"][data-round="' + roundNumber + '"]';
  }

  function applyScore(data) {
    if (!data || data.player_id == null || data.round_number == null) return false;
    if (!$ || !$.fn) return false;
    var $cell = $(cellSelector(data.player_id, data.round_number));
    if (!$cell.length) return false;
    if ($cell.hasClass('editing')) return false;
    var val = data.score;
    $cell.text(val === null || val === undefined || val === '' ? '' : val);
    return true;
  }

  function applyScoreList(scores, afterEach) {
    if (!scores || !scores.length) return;
    var changed = false;
    scores.forEach(function (row) {
      if (applyScore(row)) {
        changed = true;
        if (typeof afterEach === 'function') afterEach(row);
      }
    });
    return changed;
  }

  function pollLiveScores(gameId, handlers) {
    handlers = handlers || {};
    return fetch('/api/games/' + gameId + '/live-scores', {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' }
    }).then(function (r) {
      if (!r.ok) return null;
      return r.json();
    }).then(function (payload) {
      if (!payload || !payload.scores) return;
      var changed = false;
      if (typeof handlers.onScore === 'function') {
        payload.scores.forEach(function (row) {
          handlers.onScore(row);
          changed = true;
        });
      } else {
        changed = applyScoreList(payload.scores, handlers.afterScore);
      }
      if (changed && typeof handlers.onPollApplied === 'function') {
        handlers.onPollApplied(payload);
      } else if (changed && typeof handlers.afterApply === 'function') {
        handlers.afterApply(payload);
      }
      if (payload.is_complete && typeof handlers.onCompleted === 'function' && !handlers._completedFired) {
        // Only auto-fire from poll if the page has not already handled completion.
        // Avoid duplicate modals when socket already delivered game_completed.
      }
      return payload;
    }).catch(function () { return null; });
  }

  /**
   * Start live sync for an active game.
   * @param {number} gameId
   * @param {object} handlers
   *   onScore(data) optional override for score_update
   *   afterScore(data) called after a cell is updated from socket
   *   afterApply() called after socket/poll batch UI refresh (totals etc.)
   *   onCompleted / onPaused / onResumed
   *   pollMs (default 5000)
   */
  function connect(gameId, handlers) {
    handlers = handlers || {};
    if (!gameId) return null;
    if (typeof io === 'undefined') {
      console.warn('DiFedeLiveGame: Socket.IO not loaded yet');
      return null;
    }

    var socket = io({
      transports: ['websocket', 'polling'],
      reconnection: true,
      reconnectionAttempts: Infinity,
      reconnectionDelay: 800,
      reconnectionDelayMax: 5000,
      timeout: 20000,
      withCredentials: true
    });

    function join() {
      socket.emit('join_game', { game_id: gameId });
      pollLiveScores(gameId, handlers);
    }

    socket.on('connect', function () {
      join();
      if (window.DiFedeLiveGame) {
        window.DiFedeLiveGame.connected = true;
      }
    });
    socket.on('disconnect', function () {
      if (window.DiFedeLiveGame) {
        window.DiFedeLiveGame.connected = false;
      }
    });
    socket.on('reconnect', join);

    socket.on('score_update', function (data) {
      if (typeof handlers.onScore === 'function') {
        handlers.onScore(data);
        return;
      }
      if (applyScore(data)) {
        if (typeof handlers.afterScore === 'function') handlers.afterScore(data);
        if (typeof handlers.afterApply === 'function') handlers.afterApply(data);
      }
    });

    if (typeof handlers.onCompleted === 'function') {
      socket.on('game_completed', handlers.onCompleted);
    }
    if (typeof handlers.onPaused === 'function') {
      socket.on('game_paused', handlers.onPaused);
    }
    if (typeof handlers.onResumed === 'function') {
      socket.on('game_resumed', handlers.onResumed);
    }

    var pollMs = handlers.pollMs || DEFAULT_POLL_MS;
    var pollTimer = setInterval(function () {
      pollLiveScores(gameId, handlers);
    }, pollMs);

    window.addEventListener('beforeunload', function () {
      try { socket.emit('leave_game', { game_id: gameId }); } catch (e) { /* ignore */ }
      clearInterval(pollTimer);
    });

    // Visibility resume: catch up when returning to the tab.
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) pollLiveScores(gameId, handlers);
    });

    return socket;
  }

  window.DiFedeLiveGame = {
    connect: connect,
    applyScore: applyScore,
    poll: pollLiveScores,
    connected: false
  };
})(window, window.jQuery);