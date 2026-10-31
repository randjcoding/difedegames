"""Read-only deployment checks; no account credentials or production fixtures."""
import argparse
import json
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='https://games2.difedes.com')
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    checks = []
    def verify(name, condition):
        checks.append({'name': name, 'passed': bool(condition)})
    def fetch(path):
        try:
            response = urllib.request.urlopen(base + path, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()
    status, headers, html = fetch('/')
    verify('app HTML serves', status == 200 and b'<html' in html.lower())
    verify('CSP restricts scripts and connections to same origin', all(part in headers.get('Content-Security-Policy', '') for part in ("script-src 'self'", "connect-src 'self'", "frame-ancestors 'none'", "object-src 'none'")))
    verify('nosniff and frame protection', headers.get('X-Content-Type-Options') == 'nosniff' and headers.get('X-Frame-Options') == 'DENY')
    verify('app HTML is not cached', 'no-store' in headers.get('Cache-Control', ''))
    status, _, routed = fetch('/people')
    verify('SPA history route serves same app', status == 200 and routed == html)
    for path in ('/assets/acceptance-definitely-missing.js', '/art/acceptance-definitely-missing.png'):
        status, headers, _ = fetch(path)
        verify('missing static file returns404 ' + path.split('/')[1], status == 404)
        verify('missing static file is not immutable-cached ' + path.split('/')[1], 'immutable' not in headers.get('Cache-Control', ''))
    status, headers, body = fetch('/api/health')
    health = json.loads(body) if status == 200 else {}
    verify('API and database health', status == 200 and health.get('database') is True and health.get('status') == 'ok')
    status, headers, _ = fetch('/api/me')
    verify('anonymous account API rejects access', status == 401)
    verify('account response private no-store', 'no-store' in headers.get('Cache-Control', '') and 'private' in headers.get('Cache-Control', ''))
    verify('anonymous request does not create session', headers.get('Set-Cookie') is None)
    report = {'base_url': base, 'checks': len(checks), 'passed': sum(c['passed'] for c in checks), 'results': checks,
              'limits': 'Read-only unauthenticated checks. Secure-cookie attributes and permissions tested in isolated acceptance; no real user login here.'}
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if all(c['passed'] for c in checks) else 1)


if __name__ == '__main__':
    main()
