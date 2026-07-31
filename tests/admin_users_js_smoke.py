#!/usr/bin/env python3
"""Prove /auth/admin/users HTML/JS is not broken the way live was.

Live bug: |tojson names inside onclick="..." produced
  onclick="deleteUser(31, "Michael Seibert")"
which is a SyntaxError and kills all handlers on the page.
"""
from __future__ import annotations

import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)


def main() -> int:
    from playwright.sync_api import sync_playwright
    from app import create_app
    from app.database import get_db_connection
    import psycopg2.extras

    app = create_app()
    app.config['TESTING'] = True
    client = app.test_client()

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, email FROM users
        WHERE role = 'super_admin' AND is_active = TRUE AND archived_at IS NULL
        ORDER BY id LIMIT 1
    """)
    admin = cur.fetchone()
    cur.close()
    conn.close()
    if not admin:
        print('FAIL: no super_admin')
        return 1

    with client.session_transaction() as sess:
        sess['user_id'] = admin['id']
        sess['email'] = admin['email']

    html = client.get('/auth/admin/users').get_data(as_text=True)
    failures = []

    bad = re.findall(
        r'onclick="(?:deleteUser|approveUser|setRole|toggleUserStatus)\([^"]*"[^"]+"',
        html,
    )
    if bad:
        failures.append('broken onclick quoting: ' + bad[0][:160])
    if 'data-aum-action="delete"' not in html:
        failures.append('missing data-aum-action="delete"')
    if 'onclick="deleteUser(' in html:
        failures.append('legacy onclick deleteUser still present')
    if 'Final Warning' in html:
        failures.append('nested Final Warning confirm still present')

    # Serve rendered HTML over http:// so localStorage / Bootstrap work in Chromium.
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith('/auth/admin/users/') and self.path.endswith('/delete'):
                body = b'{"success":true,"message":"Login removed (smoke)."}'
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith('/api/'):
                body = b'{"count":0}'
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = html.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.do_GET()

        def log_message(self, *args):
            return

    httpd = HTTPServer(('127.0.0.1', 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={'width': 390, 'height': 844},
            is_mobile=True,
            has_touch=True,
        )
        page = context.new_page()
        js_errors = []
        page.on('pageerror', lambda err: js_errors.append(str(err)))
        page.goto(f'http://127.0.0.1:{port}/auth/admin/users', wait_until='networkidle')
        page.wait_for_timeout(400)

        # Ignore CDN noise unrelated to our page script
        real_errors = [e for e in js_errors if 'Unexpected end of input' in e
                       or 'deleteUser' in e or 'is not defined' in e]
        if real_errors:
            failures.append('JS errors: ' + '; '.join(real_errors))

        has_appmodal = page.evaluate('typeof window.AppModal')
        if has_appmodal != 'object':
            failures.append('AppModal missing after HTTP load: ' + str(has_appmodal))

        delete_btn = page.locator('.aum-card-list [data-aum-action="delete"]').first
        if delete_btn.count() == 0:
            failures.append('no delete button in mobile card list')
        else:
            delete_btn.click()
            page.wait_for_selector('#appModal.show', timeout=8000)
            title = page.locator('#appModalTitle').inner_text()
            if 'Delete' not in title:
                failures.append(f'expected Delete confirm, got {title!r}')
            else:
                with page.expect_request(
                    lambda r: r.url.endswith('/delete') and r.method == 'POST',
                    timeout=8000,
                ) as req_info:
                    page.get_by_role('button', name='Delete Login').click()
                print('Delete POST:', req_info.value.method, req_info.value.url)
                page.wait_for_function(
                    "() => (document.querySelector('#appModalTitle')||{}).textContent "
                    "&& document.querySelector('#appModalTitle').textContent.includes('User Deleted')",
                    timeout=8000,
                )
                print('Success modal:', page.locator('#appModalTitle').inner_text())

        context.close()
        browser.close()
    httpd.shutdown()

    if failures:
        print('FAIL:')
        for f in failures:
            print(' -', f)
        return 1
    print('PASS: admin users page has valid JS; Delete confirm + POST works')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
