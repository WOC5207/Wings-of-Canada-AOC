"""Verify reverse-proxy mode: ProxyFix honours X-Forwarded-* headers and the
session cookie is marked Secure. Run against a server started with
AOC_BEHIND_PROXY=1 AOC_SECURE_COOKIES=1 on a fresh database."""
import urllib.error
import urllib.parse
import urllib.request

HDRS = {
    "X-Forwarded-Proto": "https",
    "X-Forwarded-Host": "aoc.example.com",
    "X-Forwarded-For": "203.0.113.7",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


body = urllib.parse.urlencode({
    "email": "proxy@test.local", "callsign_digits": "77",
    "password": "proxy-pass-123", "confirm": "proxy-pass-123",
}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8080/register", data=body, headers=HDRS
)
opener = urllib.request.build_opener(NoRedirect)
try:
    resp = opener.open(req)
except urllib.error.HTTPError as e:
    resp = e

cookie = resp.headers.get("Set-Cookie", "")
checks = {
    "register redirects (302)": resp.status == 302,
    "session cookie marked Secure": "Secure" in cookie,
    "session cookie HttpOnly": "HttpOnly" in cookie,
    "session cookie SameSite=Lax": "SameSite=Lax" in cookie,
}
failed = False
for label, good in checks.items():
    print(f"[{'ok' if good else 'FAIL'}] {label}")
    failed |= not good
raise SystemExit(1 if failed else 0)
