"""Server-side Google PKCE and revocable opaque Supabase sessions."""

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from aiohttp import ClientTimeout, web
from cryptography.fernet import InvalidToken

from supabase.crypto import b64, digest, unb64

SESSION_COOKIE = "__Host-wt-session"
FLOW_COOKIE = "__Host-wt-flow"


class AuthenticationError(Exception):
    pass


class AuthProviderError(Exception):
    pass


class Auth:
    def __init__(self, pool, crypto, client, supabase_url, publishable_key, public_url):
        self.pool, self.crypto, self.client = pool, crypto, client
        self.base, self.apikey = supabase_url.rstrip("/"), publishable_key
        self.public_url = public_url.rstrip("/")

    async def provider(self, path, *, payload=None, token=None):
        headers = {"apikey": self.apikey}
        if token:
            headers["Authorization"] = "Bearer " + token
        method = "POST" if payload is not None else "GET"
        async with self.client.request(method, self.base+"/auth/v1/"+path, json=payload,
                                       headers=headers, allow_redirects=False, timeout=ClientTimeout(total=20)) as response:
            if response.status not in (200, 204):
                if response.status in (400, 401, 403):
                    raise AuthenticationError()
                raise AuthProviderError()
            return await response.json() if response.status != 204 else {}

    async def start(self, request):
        token, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        target = request.query.get("next", "/")
        if target == "/admin":
            target = "/admin/"
        if target not in {"/", "/admin/"}:
            parts = target.strip("/").split("/")
            try:
                if len(parts) != 2 or parts[0] != "terminal":
                    raise ValueError()
                target = f"/terminal/{uuid.UUID(parts[1])}/"
            except ValueError:
                target = "/"
        await self.pool.execute("""INSERT INTO oauth_flows(id,verifier,next_path,expires_at)
            VALUES($1,$2,$3,now()+interval '10 minutes')""", digest(token), self.crypto.encrypt(verifier), target)
        params = {"provider": "google", "redirect_to": self.public_url+"/auth/callback",
                  "code_challenge": b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "s256"}
        response = web.HTTPFound(self.base+"/auth/v1/authorize?"+urlencode(params))
        response.set_cookie(FLOW_COOKIE, token, max_age=600, secure=True, httponly=True, samesite="Lax", path="/")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    async def validate_tokens(self, data):
        access = data.get("access_token", "")
        refresh = data.get("refresh_token", "")
        if not access or not refresh:
            raise AuthenticationError()
        # Supabase validates the JWT signature, issuer, and current user. Decode
        # claims only after that response, to obtain the revocable session ID.
        user = await self.provider("user", token=access)
        try:
            claims = json.loads(unb64(access.split(".")[1]))
            user_id, session_id = uuid.UUID(user["id"]), uuid.UUID(claims["session_id"])
            if claims["sub"] != user["id"] or claims.get("role") != "authenticated" or claims.get("iss") != self.base+"/auth/v1":
                raise AuthenticationError()
            if not user.get("email") or not user.get("email_confirmed_at"):
                raise AuthenticationError()
            if not any(identity.get("provider") == "google" for identity in user.get("identities", [])):
                raise AuthenticationError()
            expiry = datetime.fromtimestamp(int(claims["exp"]), timezone.utc)
        except (KeyError, ValueError, IndexError, TypeError):
            raise AuthenticationError() from None
        return user, user_id, session_id, access, refresh, expiry

    async def callback(self, request):
        raw = request.cookies.get(FLOW_COOKIE, "")
        code = request.query.get("code", "")
        if not raw or not code or len(code) > 4096:
            raise AuthenticationError()
        flow = await self.pool.fetchrow("DELETE FROM oauth_flows WHERE id=$1 AND expires_at>now() RETURNING *", digest(raw))
        if flow is None:
            raise AuthenticationError()
        data = await self.provider("token?grant_type=pkce", payload={"auth_code": code, "code_verifier": self.crypto.decrypt(flow["verifier"])})
        user, owner, auth_session, access, refresh, expiry = await self.validate_tokens(data)
        cookie = secrets.token_urlsafe(32)
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("""INSERT INTO profiles(id,email,display_name) VALUES($1,$2,$3)
                ON CONFLICT(id) DO UPDATE SET email=excluded.email,display_name=excluded.display_name,last_login_at=now()""",
                owner, user["email"], str(user.get("user_metadata", {}).get("full_name", ""))[:200])
            await conn.execute("""INSERT INTO sessions(id,user_id,auth_session_id,access_token,refresh_token,access_expires_at,expires_at)
                VALUES($1,$2,$3,$4,$5,$6,now()+interval '30 days')""", digest(cookie), owner, auth_session,
                self.crypto.encrypt(access), self.crypto.encrypt(refresh), expiry)
            await conn.execute("""INSERT INTO login_events(user_id,action,source,metadata)
                VALUES($1,'login','application','{"provider":"google"}')""", owner)
        response = web.HTTPFound(flow["next_path"])
        response.set_cookie(SESSION_COOKIE, cookie, max_age=30*86400, secure=True, httponly=True, samesite="Lax", path="/")
        response.del_cookie(FLOW_COOKIE, path="/", secure=True, httponly=True, samesite="Lax")
        return response

    async def session(self, request):
        cookie = request.cookies.get(SESSION_COOKIE, "")
        if not cookie or len(cookie) > 128:
            raise AuthenticationError()
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("""SELECT s.*,p.email,p.display_name FROM sessions s JOIN profiles p ON p.id=s.user_id
                WHERE s.id=$1 AND s.revoked_at IS NULL AND s.expires_at>now()
                AND auth_session_valid(s.auth_session_id,s.user_id) FOR UPDATE OF s""", digest(cookie))
            if row is None:
                raise AuthenticationError()
            row = dict(row)
            if row["access_expires_at"] < datetime.now(timezone.utc)+timedelta(seconds=60):
                try:
                    data = await self.provider("token?grant_type=refresh_token", payload={"refresh_token": self.crypto.decrypt(row["refresh_token"])})
                    _, owner, auth_session, access, refresh, expiry = await self.validate_tokens(data)
                except (AuthenticationError, InvalidToken):
                    raise AuthenticationError() from None
                if owner != row["user_id"] or auth_session != row["auth_session_id"]:
                    raise AuthenticationError()
                await conn.execute("UPDATE sessions SET access_token=$2,refresh_token=$3,access_expires_at=$4 WHERE id=$1", row["id"],
                                   self.crypto.encrypt(access), self.crypto.encrypt(refresh), expiry)
                row["access_token"] = self.crypto.encrypt(access)
        row["csrf"] = self.crypto.csrf(cookie)
        return row

    def require_csrf(self, request, session):
        if request.headers.get("Origin") != self.public_url:
            raise web.HTTPForbidden(text="Invalid request origin")
        if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), session["csrf"]):
            raise web.HTTPForbidden(text="Invalid request token")

    async def logout(self, request, session):
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("UPDATE sessions SET revoked_at=now() WHERE id=$1", session["id"])
            await conn.execute("INSERT INTO login_events(user_id,action,source) VALUES($1,'logout','application')", session["user_id"])
        try:
            await self.provider("logout?scope=local", payload={}, token=self.crypto.decrypt(session["access_token"]))
        except Exception:
            # The opaque application session is already revoked, even if Auth is unavailable.
            pass
        response = web.json_response({"ok": True})
        response.del_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="Lax")
        return response
