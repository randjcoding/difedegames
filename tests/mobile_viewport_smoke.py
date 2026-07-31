#!/usr/bin/env python3
"""Render /auth/admin/users and /admin at iPhone 12/13 CSS viewport (390x844).

Uses Playwright Chromium when available. Exit 0 only if layout checks pass.
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

IPHONE_W, IPHONE_H = 390, 844


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('Playwright not installed; installing...')
        import subprocess
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'playwright'])
        subprocess.check_call([sys.executable, '-m', 'playwright', 'install', 'chromium'])
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
        print('FAIL: no super_admin user available for viewport smoke')
        return 1

    with client.session_transaction() as sess:
        sess['user_id'] = admin['id']
        sess['email'] = admin['email']

    pages = {
        'admin_users': client.get('/auth/admin/users').get_data(as_text=True),
        'admin': client.get('/admin').get_data(as_text=True),
    }
    for name, html in pages.items():
        if 'User Management' not in html and name == 'admin_users':
            print(f'FAIL: {name} did not render user management page')
            return 1
        if name == 'admin' and 'Admin Dashboard' not in html:
            print(f'FAIL: {name} did not render admin dashboard')
            return 1

    outdir = tempfile.mkdtemp(prefix='difede-mobile-')
    failures = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={'width': IPHONE_W, 'height': IPHONE_H},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            user_agent=(
                'Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) '
                'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 '
                'Mobile/15E148 Safari/604.1'
            ),
        )
        page = context.new_page()

        # Inject rendered HTML via data URL equivalent: set content
        page.set_content(pages['admin_users'], wait_until='domcontentloaded')
        page.wait_for_timeout(200)

        cards = page.locator('.aum-user-card')
        table_wrap = page.locator('.aum-table-wrap')
        card_list = page.locator('.aum-card-list')

        card_count = cards.count()
        cards_visible = card_list.is_visible() and card_count > 0
        table_hidden = not table_wrap.is_visible()

        # Overflow: page content should not force horizontal scroll wider than viewport
        metrics = page.evaluate('''() => {
          const doc = document.documentElement;
          const body = document.body;
          return {
            scrollWidth: Math.max(doc.scrollWidth, body.scrollWidth),
            clientWidth: doc.clientWidth,
            cardWidth: document.querySelector('.aum-user-card')
              ? document.querySelector('.aum-user-card').getBoundingClientRect().width
              : 0,
            actionBtnMinWidth: (() => {
              const btn = document.querySelector('.aum-actions .btn');
              return btn ? btn.getBoundingClientRect().width : 0;
            })()
          };
        }''')

        shot = os.path.join(outdir, 'admin_users_iphone13.png')
        page.screenshot(path=shot, full_page=True)
        print(f'Screenshot: {shot}')
        print(f'Viewport metrics: {metrics}')

        if not cards_visible:
            failures.append('mobile card list not visible at 390px')
        if not table_hidden:
            failures.append('desktop table still visible at 390px')
        if metrics['scrollWidth'] > IPHONE_W + 8:
            failures.append(
                f'horizontal overflow: scrollWidth={metrics["scrollWidth"]} > {IPHONE_W}'
            )
        if metrics['cardWidth'] and metrics['cardWidth'] > IPHONE_W:
            failures.append(f'user card wider than viewport: {metrics["cardWidth"]}')

        page.set_content(pages['admin'], wait_until='domcontentloaded')
        page.wait_for_timeout(200)
        widgets = page.locator('.admin-widget')
        widget_count = widgets.count()
        admin_metrics = page.evaluate('''() => {
          const doc = document.documentElement;
          const body = document.body;
          return {
            scrollWidth: Math.max(doc.scrollWidth, body.scrollWidth),
            fullscreenModals: document.querySelectorAll('.modal-fullscreen-sm-down').length
          };
        }''')
        shot2 = os.path.join(outdir, 'admin_dashboard_iphone13.png')
        page.screenshot(path=shot2, full_page=True)
        print(f'Screenshot: {shot2}')
        print(f'Admin metrics: widgets={widget_count}, {admin_metrics}')

        if widget_count < 1:
            failures.append('admin dashboard widgets missing')
        if admin_metrics['fullscreenModals'] < 3:
            failures.append('expected fullscreen-sm-down management modals')
        if admin_metrics['scrollWidth'] > IPHONE_W + 8:
            failures.append(
                f'admin dashboard horizontal overflow: {admin_metrics["scrollWidth"]}'
            )

        browser.close()

    if failures:
        print('FAIL:')
        for f in failures:
            print(f'  - {f}')
        return 1

    print(f'PASS: iPhone 12/13 viewport ({IPHONE_W}x{IPHONE_H}) layout checks OK')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
