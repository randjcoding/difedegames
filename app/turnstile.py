"""Cloudflare Turnstile helpers for public (forward-facing) forms."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from flask import current_app, request

logger = logging.getLogger(__name__)

SITEVERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'
FAILURE_MESSAGE = 'Please complete the security check and try again.'


def turnstile_active():
    """True when Turnstile should be enforced (enabled + both keys present)."""
    try:
        cfg = current_app.config
    except RuntimeError:
        return False
    if not cfg.get('TURNSTILE_ENABLED'):
        return False
    site = (cfg.get('TURNSTILE_SITE_KEY') or '').strip()
    secret = (cfg.get('TURNSTILE_SECRET_KEY') or '').strip()
    return bool(site and secret)


def turnstile_ok():
    """Validate the Turnstile token on the current request.

    Returns True when Turnstile is inactive, the app is under Flask TESTING,
    or Cloudflare Siteverify accepts the token.
    """
    if not turnstile_active():
        return True
    try:
        if current_app.config.get('TESTING'):
            return True
    except RuntimeError:
        pass

    token = (
        request.form.get('cf-turnstile-response')
        or request.headers.get('CF-Turnstile-Response')
        or ''
    ).strip()
    if not token:
        logger.warning('Turnstile token missing on %s', request.path)
        return False

    secret = (current_app.config.get('TURNSTILE_SECRET_KEY') or '').strip()
    remoteip = request.headers.get('CF-Connecting-IP') or request.remote_addr
    form = {'secret': secret, 'response': token}
    if remoteip:
        form['remoteip'] = remoteip
    body = urllib.parse.urlencode(form).encode('utf-8')

    try:
        req = urllib.request.Request(
            SITEVERIFY_URL,
            data=body,
            method='POST',
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as e:
        logger.error('Turnstile siteverify failed: %s', e)
        return False

    if not payload.get('success'):
        logger.warning(
            'Turnstile rejected on %s: %s',
            request.path,
            payload.get('error-codes') or payload,
        )
        return False
    return True


def turnstile_failure_message():
    return FAILURE_MESSAGE
