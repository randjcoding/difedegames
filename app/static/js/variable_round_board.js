/* Shared helpers for variable-round score boards:
   auto-add rounds, remote row creation, ScoreSave, round-entry view, live sync. */
(function (window, $) {
  'use strict';

  function getCellScoreValue(cell) {
    cell = $(cell);
    if (cell.hasClass('editing')) {
      var inp = cell.find('input');
      if (inp.length) return String(inp.val() == null ? '' : inp.val()).trim();
      var saved = cell.data('saved-value');
      if (saved != null) return String(saved).trim();
    }
    return cell.text().trim();
  }

  function create(cfg) {
    cfg = $.extend({
      activeGameId: null,
      players: [],
      roundLabel: 'Round',
      allowNeg: false,
      landingUrl: '/',
      resumeUrl: null,
      boardSelector: '.game-board',
      rowSelector: '.hand-row',
      accent: '#7a3fa0',
      accentDark: '#3a2a6a',
      gold: '#f0a500',
      onAfterTotals: null,
      onBeforeComplete: null,
      gameTitle: 'Game'
    }, cfg || {});

    var reCurrentIdx = 0;
    var rePlayers = (cfg.players || []).slice();
    var targetScoreAlertShown = false;
    var gameCompletedByCurrentUser = false;
    var updateTimer = null;

    function board() { return $(cfg.boardSelector); }
    function gameId() { return board().data('game-id') || cfg.activeGameId; }

    function getMaxRound() {
      var max = 0;
      $(cfg.rowSelector).each(function () {
        var r = parseInt($(this).data('round'), 10);
        if (!isNaN(r) && r > max) max = r;
      });
      return max;
    }

    function getRounds() {
      var rounds = [];
      $(cfg.rowSelector).each(function () {
        var n = parseInt($(this).data('round'), 10);
        if (!isNaN(n)) rounds.push({ num: n, label: String(n) });
      });
      rounds.sort(function (a, b) { return a.num - b.num; });
      return rounds;
    }

    function addRoundRow(roundNum) {
      if ($(cfg.rowSelector + '[data-round="' + roundNum + '"]').length) return;
      var players = rePlayers.length ? rePlayers : cfg.players;
      var row = '<tr class="hand-row" data-round="' + roundNum + '"><td class="round-cell">' + roundNum + '</td>';
      players.forEach(function (p) {
        row += '<td class="score-cell" data-player="' + p.id + '" data-round="' + roundNum + '"></td>';
      });
      row += '</tr>';
      $('.total-row').before(row);
    }

    function targetReached() {
      var target = parseInt(board().data('target-score'), 10);
      if (!target) return false;
      var anyone = false;
      $('.total-cell').each(function () {
        if ((parseInt($(this).text(), 10) || 0) >= target) anyone = true;
      });
      return anyone;
    }

    function isRoundComplete(roundNum) {
      var expected = $('#scoreTable thead th').length - 1;
      var cells = $('.score-cell[data-round="' + roundNum + '"]');
      if (!cells.length || cells.length !== expected) return false;
      var ok = true;
      cells.each(function () {
        if (getCellScoreValue($(this)) === '') { ok = false; return false; }
      });
      return ok;
    }

    function updateTotals() {
      $('.total-cell').each(function () {
        var pid = $(this).data('player');
        var tot = 0;
        $(".score-cell[data-player='" + pid + "']").each(function () {
          var s = getCellScoreValue($(this));
          if (s !== '' && !isNaN(parseInt(s, 10))) tot += parseInt(s, 10);
        });
        $(this).text(tot);
      });
      if (typeof cfg.onAfterTotals === 'function') cfg.onAfterTotals();
    }

    function ensureEmptyRow(opts) {
      opts = opts || {};
      if (!board().length) return null;
      var max = getMaxRound();
      if (!max) {
        addRoundRow(1);
        if ($('#roundEntryView').hasClass('active')) { reCurrentIdx = 0; reRenderRound(); }
        return 1;
      }
      if (!isRoundComplete(max)) return null;
      if (targetReached() && !opts.forcePastTarget) return null;
      var next = max + 1;
      addRoundRow(next);
      if ($('#roundEntryView').hasClass('active') || opts.openRoundView) {
        var rounds = getRounds();
        reCurrentIdx = Math.max(0, rounds.length - 1);
        reRenderRound();
      }
      return next;
    }

    function afterRoundFilled(triggerRound) {
      if (triggerRound == null || !isRoundComplete(triggerRound)) return;
      var target = parseInt(board().data('target-score'), 10);
      if (!target) { ensureEmptyRow(); return; }
      if (targetScoreAlertShown) return;
      var crossed = [];
      $('.total-cell').each(function () {
        var total = parseInt($(this).text(), 10) || 0;
        if (total >= target) {
          var idx = $('.total-cell').index(this);
          var name = $('#scoreTable thead th').eq(idx + 1).text().trim();
          crossed.push({ name: name, total: total });
          $(this).addClass('target-crossed');
        } else {
          $(this).removeClass('target-crossed');
        }
      });
      if (!crossed.length) { ensureEmptyRow(); return; }
      targetScoreAlertShown = true;
      var endingGame = false;
      var names = crossed.map(function (c) {
        return '<strong>' + c.name + '</strong> (' + c.total + ')';
      }).join(', ');
      AppModal.confirm(
        'Target Score Reached!',
        names + ' reached the target of <strong>' + target + '</strong> points. Would you like to end the game?',
        function () { endingGame = true; doCompleteGame(); },
        { confirmText: 'End Game', cancelText: 'Keep Playing' }
      );
      var modalEl = document.getElementById('appModal');
      $(modalEl).on('hidden.bs.modal.targetcheck', function () {
        targetScoreAlertShown = false;
        $(modalEl).off('hidden.bs.modal.targetcheck');
        if (!endingGame) ensureEmptyRow({ forcePastTarget: true });
      });
    }

    function doCompleteGame() {
      gameCompletedByCurrentUser = true;
      if (typeof cfg.onBeforeComplete === 'function') cfg.onBeforeComplete();
      var gid = gameId();
      $.ajax({
        url: '/api/games/complete/' + gid,
        method: 'POST',
        success: function (r) {
          if (r.summary) {
            AppModal.show({
              title: 'Game Complete!',
              body: '<pre class="mb-0" style="white-space:pre-wrap;font-family:inherit;">' + r.summary + '</pre>',
              type: 'success'
            });
            var m = document.getElementById('appModal');
            $(m).on('hidden.bs.modal.reload', function () {
              $(m).off('hidden.bs.modal.reload');
              window.location.reload();
            });
          } else {
            window.location.reload();
          }
        },
        error: function (xhr) {
          gameCompletedByCurrentUser = false;
          AppModal.error('Error', (xhr.responseJSON && xhr.responseJSON.error) || 'Failed to complete game');
        }
      });
    }

    function scorePayload(cell, rawValue) {
      var scoreVal = rawValue !== '' && !isNaN(rawValue) ? parseInt(rawValue, 10) : null;
      return {
        game_id: gameId(),
        player_id: cell.data('player') || cell.attr('data-player'),
        round_number: parseInt(cell.data('round') || cell.attr('data-round'), 10),
        score: scoreVal
      };
    }

    function applyScoreToCell(cell, scoreVal, leaveEditing) {
      if (leaveEditing !== false) cell.removeClass('editing');
      if (leaveEditing === false && cell.hasClass('editing')) {
        cell.data('saved-value', scoreVal === null ? '' : String(scoreVal));
      } else {
        cell.text(scoreVal === null || scoreVal === '' ? '' : scoreVal);
        cell.removeData('saved-value');
      }
      updateTotals();
    }

    function saveCellScore(cell, rawValue, opts) {
      opts = opts || {};
      var leaveEditing = opts.leaveEditing !== false;
      var payload = scorePayload(cell, rawValue);
      var displayVal = payload.score;
      var originalValue = opts.originalValue;
      var lastKey = cell.data('last-saved-key');
      var thisKey = payload.player_id + ':' + payload.round_number + ':' + String(payload.score);
      if (lastKey === thisKey && !opts.force) return;

      if (opts.syncDom !== false) applyScoreToCell(cell, displayVal, leaveEditing);
      setTimeout(function () { afterRoundFilled(payload.round_number); }, 0);

      if (window.ScoreSave) {
        ScoreSave.save(payload, {
          showError: !!leaveEditing,
          success: function () { cell.data('last-saved-key', thisKey); },
          error: function (xhr) {
            if (leaveEditing) {
              cell.removeClass('editing');
              cell.text(originalValue || '');
              cell.removeData('saved-value');
              updateTotals();
              if (xhr && xhr.status !== 401) {
                AppModal.error('Error', (xhr.responseJSON && xhr.responseJSON.error) || 'Failed to save score');
              }
            }
          }
        });
        return;
      }

      $.ajax({
        url: '/api/scores',
        method: 'POST',
        contentType: 'application/json',
        data: JSON.stringify(payload),
        success: function () { cell.data('last-saved-key', thisKey); },
        error: function (xhr) {
          if (leaveEditing) {
            cell.removeClass('editing');
            cell.text(originalValue || '');
            updateTotals();
            AppModal.error('Error', (xhr.responseJSON && xhr.responseJSON.error) || 'Failed to save score');
          }
        }
      });
    }

    function updateScore(cell, nv, ov) {
      if (!cell.hasClass('editing')) return;
      clearTimeout(updateTimer);
      saveCellScore(cell, nv, { leaveEditing: true, originalValue: ov, syncDom: true });
    }

    function flushOpenScoreEdits() {
      $('.score-cell.editing input').each(function () {
        var cell = $(this).closest('.score-cell');
        var ov = cell.data('original-value');
        saveCellScore(cell, $(this).val(), { leaveEditing: true, originalValue: ov, syncDom: true, force: true });
      });
      try { reSaveCurrentRound(); } catch (e) { /* ignore */ }
    }

    function flushAsync(done) {
      flushOpenScoreEdits();
      if (window.ScoreSave && typeof ScoreSave.flushAsync === 'function') {
        ScoreSave.flushAsync().always(function () { if (done) done(); });
      } else if (done) {
        done();
      }
    }

    function reRenderRound() {
      var rounds = getRounds();
      if (!rounds.length) {
        ensureEmptyRow();
        rounds = getRounds();
      }
      if (!rounds.length) return;
      if (reCurrentIdx >= rounds.length) reCurrentIdx = rounds.length - 1;
      if (reCurrentIdx < 0) reCurrentIdx = 0;
      var rd = rounds[reCurrentIdx];
      $('#reRoundLabel').text(cfg.roundLabel + ' ' + rd.label);
      $('#reRoundProgress').text((reCurrentIdx + 1) + ' / ' + rounds.length);
      $('#rePrev').prop('disabled', reCurrentIdx === 0);

      var html = '';
      rePlayers.forEach(function (p) {
        var existingScore = '';
        var totalScore = 0;
        var cell = $('#scoreTable .score-cell[data-player="' + p.id + '"][data-round="' + rd.num + '"]');
        if (cell.length) existingScore = getCellScoreValue(cell);
        var totalCell = $('#scoreTable .total-cell[data-player="' + p.id + '"]');
        if (totalCell.length) totalScore = totalCell.text().trim();
        html += '<div class="round-player-row">'
          + '<span class="round-player-name">' + p.name + '</span>'
          + '<div class="round-player-score-wrap">';
        if (cfg.allowNeg) {
          html += '<button type="button" class="round-neg-toggle" title="Toggle negative">-</button>';
        }
        html += '<input type="number" class="round-player-score" data-pid="' + p.id + '" value="' + existingScore + '" inputmode="numeric">'
          + '</div>'
          + '<span class="round-player-total">' + totalScore + '</span>'
          + '</div>';
      });
      $('#rePlayerList').html(html);
    }

    function reSaveCurrentRound() {
      var rounds = getRounds();
      if (!rounds.length || reCurrentIdx < 0 || reCurrentIdx >= rounds.length) return;
      var rd = rounds[reCurrentIdx];
      $('.round-player-score').each(function () {
        var pid = $(this).data('pid');
        var val = $(this).val().trim();
        var cell = $('#scoreTable .score-cell[data-player="' + pid + '"][data-round="' + rd.num + '"]');
        if (cell.length) saveCellScore(cell, val, { leaveEditing: false, syncDom: true });
      });
      updateTotals();
    }

    function reNavigate(dir) {
      reSaveCurrentRound();
      var rounds = getRounds();
      if (dir > 0 && reCurrentIdx >= rounds.length - 1) {
        var added = ensureEmptyRow({ openRoundView: true });
        rounds = getRounds();
        if (added != null) {
          reCurrentIdx = rounds.length - 1;
          reRenderRound();
          return;
        }
      }
      reCurrentIdx = Math.max(0, Math.min(rounds.length - 1, reCurrentIdx + dir));
      reRenderRound();
    }

    function switchMobileView(mode) {
      $('.view-toggle-btn').removeClass('active');
      if (mode === 'table') {
        $('#btnTableView').addClass('active');
        $('#defaultTableView').removeClass('hidden');
        $('#roundEntryView').removeClass('active');
      } else if (mode === 'round') {
        $('#btnRoundView').addClass('active');
        $('#defaultTableView').addClass('hidden');
        $('#roundEntryView').addClass('active');
        var rounds = getRounds();
        if (!rounds.length) ensureEmptyRow();
        rounds = getRounds();
        var openIdx = rounds.length - 1;
        for (var i = 0; i < rounds.length; i++) {
          if (!isRoundComplete(rounds[i].num)) { openIdx = i; break; }
        }
        reCurrentIdx = Math.max(0, openIdx);
        reRenderRound();
      }
    }

    function openFullscreenTable() {
      var body = document.getElementById('fullscreenBody');
      if (!body) return;
      var rounds = getRounds();
      if (!rounds.length) { ensureEmptyRow(); rounds = getRounds(); }
      var pc = rePlayers.length;
      var colW = pc <= 4 ? '70px' : pc <= 6 ? '58px' : '48px';
      var fontSize = pc <= 4 ? '0.88rem' : pc <= 6 ? '0.78rem' : '0.7rem';
      var pad = pc <= 4 ? '6px 8px' : '4px 5px';
      var inputPad = pc <= 4 ? '6px 3px' : '4px 2px';
      var tbl = '<div style="overflow:auto;-webkit-overflow-scrolling:touch;width:100%;height:100%;">';
      tbl += '<table class="fs-tbl" style="table-layout:fixed;width:100%;border-collapse:collapse;margin:0;">';
      tbl += '<colgroup><col style="width:52px;">';
      for (var i = 0; i < pc; i++) tbl += '<col style="width:' + colW + ';">';
      tbl += '</colgroup><thead><tr style="background:' + cfg.accentDark + ';color:#fff;position:sticky;top:0;z-index:2;">';
      tbl += '<th style="padding:' + pad + ';font-size:' + fontSize + ';font-weight:700;text-align:center;position:sticky;left:0;background:' + cfg.accentDark + ';z-index:3;">Rnd</th>';
      rePlayers.forEach(function (p) {
        var short = p.name.length > 6 ? p.name.substring(0, 5) + '.' : p.name;
        tbl += '<th style="padding:' + pad + ';font-size:' + fontSize + ';font-weight:700;text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="' + p.name + '">' + short + '</th>';
      });
      tbl += '</tr></thead><tbody>';
      rounds.forEach(function (rd) {
        tbl += '<tr style="border-bottom:1px solid rgba(0,0,0,0.15);">';
        tbl += '<td style="background:' + cfg.accent + ';color:#fff;font-weight:700;text-align:center;padding:' + pad + ';font-size:' + fontSize + ';position:sticky;left:0;z-index:1;">' + rd.label + '</td>';
        rePlayers.forEach(function (p) {
          var origCell = $('#scoreTable .score-cell[data-player="' + p.id + '"][data-round="' + rd.num + '"]');
          var val = origCell.length ? getCellScoreValue(origCell) : '';
          tbl += '<td style="padding:0;text-align:center;background:#1a1a2e;">';
          tbl += '<input type="number" inputmode="numeric" class="fs-score-input" data-pid="' + p.id + '" data-rnd="' + rd.num + '" value="' + val + '" style="width:100%;border:none;text-align:center;font-size:' + fontSize + ';padding:' + inputPad + ';background:transparent;color:#e0e0e0;font-weight:600;outline:none;caret-color:#e0e0e0;">';
          tbl += '</td>';
        });
        tbl += '</tr>';
      });
      tbl += '<tr style="border-top:3px solid ' + cfg.accent + ';position:sticky;bottom:0;z-index:2;">';
      tbl += '<td style="background:' + cfg.accentDark + ';color:#fff;font-weight:700;text-align:center;padding:' + pad + ';font-size:' + fontSize + ';position:sticky;left:0;z-index:3;">Total</td>';
      rePlayers.forEach(function (p) {
        tbl += '<td class="fs-total" data-pid="' + p.id + '" style="font-weight:800;text-align:center;padding:' + pad + ';font-size:' + fontSize + ';color:' + cfg.gold + ';background:#1a1030;">0</td>';
      });
      tbl += '</tr></tbody></table></div>';
      body.innerHTML = tbl;
      fsUpdateTotals();
      $(body).off('change input blur', '.fs-score-input')
        .on('change blur', '.fs-score-input', function () {
          var $inp = $(this);
          var cell = $('#scoreTable .score-cell[data-player="' + $inp.data('pid') + '"][data-round="' + $inp.data('rnd') + '"]');
          if (cell.length) { saveCellScore(cell, $inp.val().trim(), { leaveEditing: false, syncDom: true }); fsUpdateTotals(); }
        })
        .on('input', '.fs-score-input', function () {
          var $inp = $(this);
          var cell = $('#scoreTable .score-cell[data-player="' + $inp.data('pid') + '"][data-round="' + $inp.data('rnd') + '"]');
          clearTimeout($inp.data('saveTimer'));
          $inp.data('saveTimer', setTimeout(function () {
            if (cell.length) saveCellScore(cell, $inp.val().trim(), { leaveEditing: false, syncDom: true });
            fsUpdateTotals();
          }, 800));
        });
      bootstrap.Modal.getOrCreateInstance(document.getElementById('fullscreenTableModal')).show();
    }

    function fsUpdateTotals() {
      rePlayers.forEach(function (p) {
        var total = 0;
        $(".fs-score-input[data-pid='" + p.id + "']").each(function () {
          var v = parseInt($(this).val(), 10);
          if (!isNaN(v)) total += v;
        });
        $(".fs-total[data-pid='" + p.id + "']").text(total);
      });
    }

    function applyRemoteScore(data) {
      var cell = $('.score-cell[data-player="' + data.player_id + '"][data-round="' + data.round_number + '"]');
      if (!cell.length) {
        var currentMax = getMaxRound();
        if (data.round_number > currentMax) {
          for (var r = currentMax + 1; r <= data.round_number; r++) addRoundRow(r);
        } else if (!currentMax) {
          addRoundRow(data.round_number);
        }
        cell = $('.score-cell[data-player="' + data.player_id + '"][data-round="' + data.round_number + '"]');
      }
      if (cell.length && !cell.hasClass('editing')) {
        cell.text(data.score === null || data.score === '' ? '' : data.score);
        updateTotals();
        setTimeout(function () {
          afterRoundFilled(parseInt(data.round_number, 10));
          if ($('#roundEntryView').hasClass('active')) reRenderRound();
        }, 0);
      }
    }

    function connectLive() {
      if (!cfg.activeGameId) return;
      if (!window.DiFedeLiveGame) {
        setTimeout(connectLive, 100);
        return;
      }
      var resumeUrl = cfg.resumeUrl || (cfg.landingUrl + '?game_id=' + cfg.activeGameId);
      DiFedeLiveGame.connect(cfg.activeGameId, {
        onScore: applyRemoteScore,
        afterApply: function () {
          updateTotals();
          if ($('#roundEntryView').hasClass('active')) reRenderRound();
        },
        afterScore: function (d) {
          setTimeout(function () { afterRoundFilled(parseInt(d.round_number, 10)); }, 0);
        },
        onCompleted: function (d) {
          if (gameCompletedByCurrentUser) { gameCompletedByCurrentUser = false; return; }
          AppModal.show({
            title: 'Game Complete',
            body: '<pre style="white-space:pre-wrap;font-family:inherit;">' + d.summary + '</pre>',
            type: 'success'
          });
          var m = document.getElementById('appModal');
          $(m).on('hidden.bs.modal.reload', function () {
            $(m).off('hidden.bs.modal.reload');
            window.location.reload();
          });
        },
        onPaused: function () {
          AppModal.show({
            title: 'Game Paused',
            body: '<p>Another player has paused this game. Redirecting...</p>',
            type: 'warning'
          });
          setTimeout(function () { window.location.href = cfg.landingUrl; }, 2000);
        },
        onResumed: function () {
          AppModal.show({
            title: 'Game Resumed',
            body: '<p>Another player has resumed the game. Loading...</p>',
            type: 'success'
          });
          setTimeout(function () { window.location.href = resumeUrl; }, 2000);
        }
      });
    }

    function bindCellEditing() {
      $(document).off('click.vrbScore', '.score-cell').on('click.vrbScore', '.score-cell', function () {
        var cell = $(this);
        if (cell.hasClass('editing')) return;
        var cv = getCellScoreValue(cell);
        cell.data('original-value', cv);
        cell.addClass('editing');
        if (cfg.allowNeg) {
          cell.html(
            '<div class="d-flex align-items-center" style="gap:2px;">' +
              '<button type="button" class="btn btn-sm neg-toggle" style="padding:0 4px;font-size:0.8rem;line-height:1;color:#dc3545;font-weight:900;border:1px solid #dc3545;border-radius:4px;min-width:22px;" title="Toggle negative">-</button>' +
              '<input type="number" class="form-control form-control-sm border-0" value="' + cv + '" style="width:100%;text-align:center;background:transparent;box-shadow:none;">' +
            '</div>'
          );
          cell.find('.neg-toggle').on('click', function (e) {
            e.stopPropagation();
            var inp = cell.find('input');
            var v = parseInt(inp.val(), 10) || 0;
            inp.val(v * -1).focus();
          });
        } else {
          cell.html('<input type="number" class="form-control form-control-sm border-0" value="' + cv + '" inputmode="numeric" style="width:100%;text-align:center;background:transparent;box-shadow:none;">');
        }
        var inp = cell.find('input');
        inp.focus().select();
        inp.on('blur', function () { if (cell.hasClass('editing')) updateScore(cell, inp.val(), cv); });
        inp.on('input', function () {
          clearTimeout(updateTimer);
          updateTimer = setTimeout(function () {
            if (cell.hasClass('editing')) updateScore(cell, inp.val(), cv);
          }, 4000);
        });
        inp.on('keypress', function (e) {
          if (e.which === 13) { e.preventDefault(); updateScore(cell, inp.val(), cv); }
        });
      });

      $(document).off('keydown.vrbScore', '.score-cell input').on('keydown.vrbScore', '.score-cell input', function (e) {
        var cell = $(this).closest('.score-cell');
        var cv = cell.data('original-value');
        var nv = $(this).val();
        if (e.which === 9) {
          e.preventDefault();
          updateScore(cell, nv, cv);
          var nc = e.shiftKey ? cell.prev('.score-cell') : cell.next('.score-cell');
          if (!nc.length && !e.shiftKey) {
            var nr = cell.parent('tr').next('tr.hand-row');
            if (nr.length) nc = nr.find('.score-cell').first();
            else {
              ensureEmptyRow();
              nr = cell.parent('tr').next('tr.hand-row');
              if (nr.length) nc = nr.find('.score-cell').first();
            }
          }
          if (nc && nc.length) nc.click();
        }
      });
    }

    function bindRoundViewUi() {
      $('#btnTableView').off('click.vrb').on('click.vrb', function () { switchMobileView('table'); });
      $('#btnRoundView').off('click.vrb').on('click.vrb', function () { switchMobileView('round'); });
      $('#btnFullView').off('click.vrb').on('click.vrb', function () { openFullscreenTable(); });
      $('#rePrev').off('click.vrb').on('click.vrb', function () { reNavigate(-1); });
      $('#reNext').off('click.vrb').on('click.vrb', function () { reNavigate(1); });
      $('#reSaveRound').off('click.vrb').on('click.vrb', function () { reSaveCurrentRound(); });
      $(window).off('resize.vrb').on('resize.vrb', function () {
        if (window.innerWidth > 768) switchMobileView('table');
      });

      $(document).off('blur.vrbScore input.vrbScore click.vrbNeg', '.round-player-score, .round-neg-toggle')
        .on('click.vrbNeg', '.round-neg-toggle', function (e) {
          e.preventDefault();
          var inp = $(this).siblings('.round-player-score');
          var v = parseInt(inp.val(), 10) || 0;
          inp.val(v * -1).trigger('input').focus();
        })
        .on('blur.vrbScore', '.round-player-score', function () {
          var rounds = getRounds();
          if (!rounds.length) return;
          var rd = rounds[reCurrentIdx];
          var pid = $(this).data('pid');
          var cell = $('#scoreTable .score-cell[data-player="' + pid + '"][data-round="' + rd.num + '"]');
          if (cell.length) {
            saveCellScore(cell, $(this).val().trim(), { leaveEditing: false, syncDom: true });
            updateTotals();
            reRenderRound();
          }
        })
        .on('input.vrbScore', '.round-player-score', function () {
          var $inp = $(this);
          var rounds = getRounds();
          if (!rounds.length) return;
          var rd = rounds[reCurrentIdx];
          var pid = $inp.data('pid');
          var cell = $('#scoreTable .score-cell[data-player="' + pid + '"][data-round="' + rd.num + '"]');
          clearTimeout($inp.data('saveTimer'));
          $inp.data('saveTimer', setTimeout(function () {
            if (cell.length) {
              saveCellScore(cell, $inp.val().trim(), { leaveEditing: false, syncDom: true });
              updateTotals();
            }
          }, 800));
        });
    }

    function bindActions() {
      $('#addHand, #addRound').off('click.vrb').on('click.vrb', function () {
        var next = getMaxRound() + 1;
        if (!getMaxRound()) next = 1;
        addRoundRow(next || 1);
        var tableResp = $('.table-responsive');
        if (tableResp.length && tableResp[0]) tableResp.scrollTop(tableResp[0].scrollHeight);
        if ($('#roundEntryView').hasClass('active')) {
          reCurrentIdx = getRounds().length - 1;
          reRenderRound();
        }
      });

      $('#completeGame').off('click.vrb').on('click.vrb', function () {
        AppModal.confirm('Complete Game', 'Are you sure you want to complete this game? This cannot be undone.', function () {
          doCompleteGame();
        });
      });

      $('#pauseGame').off('click.vrb').on('click.vrb', function () {
        flushAsync(function () {
          var gid = gameId();
          $.ajax({
            url: '/api/games/pause/' + gid,
            method: 'POST',
            success: function () {
              AppModal.show({ title: 'Game Paused', body: '<p>Returning...</p>', type: 'info' });
              setTimeout(function () { window.location.href = cfg.landingUrl; }, 1500);
            },
            error: function (xhr) {
              AppModal.error('Error', (xhr.responseJSON && xhr.responseJSON.error) || 'Failed to pause game');
            }
          });
        });
      });
    }

    function init() {
      if (!rePlayers.length) {
        $('#scoreTable thead th:not(:first-child)').each(function (i) {
          var txt = $(this).find('.player-name').text().trim() || $(this).text().trim();
          var cell = $('#scoreTable tbody tr.hand-row:first td.score-cell').eq(i);
          rePlayers.push({ name: txt, id: cell.length ? cell.data('player') : null });
        });
      }
      var pc = $('#scoreTable th').length - 1;
      if (pc > 0) {
        $('#scoreTable').addClass('players-' + pc);
        document.documentElement.style.setProperty('--player-count', pc);
      }
      if (!getMaxRound()) addRoundRow(1);
      updateTotals();
      ensureEmptyRow();
      bindCellEditing();
      bindRoundViewUi();
      bindActions();
      connectLive();
      window.reSaveCurrentRound = reSaveCurrentRound;
      window.reNavigate = reNavigate;
      window.switchMobileView = switchMobileView;
      window.openFullscreenTable = openFullscreenTable;
    }

    return {
      init: init,
      ensureEmptyRow: ensureEmptyRow,
      addRoundRow: addRoundRow,
      getMaxRound: getMaxRound,
      updateTotals: updateTotals,
      afterRoundFilled: afterRoundFilled,
      applyRemoteScore: applyRemoteScore,
      saveCellScore: saveCellScore,
      flushAsync: flushAsync,
      doCompleteGame: doCompleteGame,
      getCellScoreValue: getCellScoreValue
    };
  }

  window.DiFedeVariableRounds = {
    create: create,
    getCellScoreValue: getCellScoreValue
  };
})(window, window.jQuery);
