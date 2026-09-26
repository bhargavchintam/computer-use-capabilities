"""AcmeCore mock: a deliberately hostile, server-rendered core-banking UI.

Stands in for a legacy back-office app with no API: frameset layout, layout
tables, labels in the neighbouring cell, per-request random ids, red <font>
errors instead of role=alert, postbacks that keep the same URL, and a
supervisor-override step. All data is synthetic.

Test hooks live under /__admin (token-protected). They are never part of an
automation allowlist.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel

from .data import Member, Share, fresh_members, money
from .faults import Fault, FaultKind, FaultRegistry
from .tenants import TENANTS, MockTenant

SESSION_COOKIE = "ACMESESSID"
APPROVAL_LIMIT = Decimal("5000.00")
MAX_FAILED_SIGNONS = 5
_TEMPLATES = Path(__file__).parent / "templates"
_CONF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_BUTTON_LABELS = {
    "open-new-share": "Open New Share",
    "new-search": "New Search",
    "return-to-member": "Return to Member",
}


@dataclass
class PendingShare:
    token: str
    member: str
    type_code: str
    type_label: str
    amount: Decimal
    nickname: str
    done: bool = False

    @property
    def amount_text(self) -> str:
        return money(self.amount)


@dataclass
class OperatorSession:
    operator: str
    last_seen: float
    pending: dict[str, PendingShare] = field(default_factory=dict)


class BankState:
    def __init__(self, tenant: MockTenant) -> None:
        self.tenant = tenant
        self.faults = FaultRegistry()
        self.reset()

    def reset(self) -> None:
        self.members: dict[str, Member] = fresh_members(self.tenant.id)
        self.sessions: dict[str, OperatorSession] = {}
        self.failed_signons: dict[str, int] = {}
        self.receipts: list[dict[str, Any]] = []
        self.posts: list[str] = []  # every form submission, in order (tests prove "sent once")
        self.next_share_seq = 51
        self.faults.clear()


class FaultRequest(BaseModel):
    kind: FaultKind
    count: int = 1
    path_prefix: str = "/"
    delay_ms: int = 0
    message: str = ""


def create_app(
    tenant_id: str,
    *,
    operator_id: str | None = None,
    operator_password: str | None = None,
    supervisor_pin: str | None = None,
    admin_token: str | None = None,
    session_idle_sec: int | None = None,
) -> FastAPI:
    tenant = TENANTS[tenant_id]
    prefix = tenant.env_prefix
    creds = (
        operator_id or os.environ.get(f"{prefix}_OPERATOR_ID", "teller01"),
        operator_password or os.environ.get(f"{prefix}_OPERATOR_PASSWORD", f"change-me-{tenant.id}"),
    )
    pin = supervisor_pin or os.environ.get("MOCK_SUPERVISOR_PIN", "2468")
    token = admin_token or os.environ.get("MOCK_ADMIN_TOKEN", "change-me-admin")
    idle = session_idle_sec or int(os.environ.get("MOCK_SESSION_IDLE_SEC", "1800"))

    state = BankState(tenant)
    env = Environment(loader=FileSystemLoader(_TEMPLATES), autoescape=select_autoescape(["html"]))
    env.globals["rid"] = lambda: "ctl00_x" + secrets.token_hex(3)  # changes on every render
    app = FastAPI(title=f"AcmeCore mock ({tenant.id})", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.bank = state

    def render(template: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        ctx.setdefault("t", tenant)
        return HTMLResponse(env.get_template(template).render(**ctx), status_code=status)

    def work(request: Request, template: str, **ctx: Any) -> HTMLResponse:
        """Render a page for the `work` frame, applying any armed page-level faults."""
        path = request.url.path
        maint = state.faults.take("maintenance", path)
        alert = state.faults.take("alert", path)
        ctx["maintenance"] = maint is not None
        ctx["alert_message"] = (alert.message or "Your password will expire in 3 days.") if alert else None
        return render(template, **ctx)

    def session_of(request: Request) -> OperatorSession | None:
        sid = request.cookies.get(SESSION_COOKIE, "")
        sess = state.sessions.get(sid)
        if sess is None:
            return None
        if time.time() - sess.last_seen > idle:
            state.sessions.pop(sid, None)
            return None
        sess.last_seen = time.time()
        return sess

    def expired() -> RedirectResponse:
        return RedirectResponse("/signon?expired=1", status_code=302)

    def member_page(request: Request, m: Member) -> HTMLResponse:
        rows = []
        for s in m.shares:
            cells = {
                "Share ID": (s.share_id, False),
                "Description": (s.description + (f" ({s.nickname})" if s.nickname else ""), False),
                "Balance": (money(s.balance), True),
                "Available": (money(s.available), True),
                "Status": (s.status, False),
                "Rate": (s.rate, True),
            }
            rows.append([cells[c] for c in tenant.share_columns])
        return work(request, "member.html", m=m, share_rows=rows)

    def message(
        request: Request,
        title: str,
        text: str,
        *,
        red: bool = False,
        back_href: str | None = None,
        back_label: str = "Back",
    ) -> HTMLResponse:
        return work(
            request,
            "message.html",
            title=title,
            text=text,
            red=red,
            back_href=back_href,
            back_label=back_label,
        )

    def restricted(request: Request) -> HTMLResponse:
        return message(
            request,
            "ACCESS DENIED",
            "Access denied: this member record is restricted (employee account). "
            "Supervisor access is required to view it.",
            red=True,
            back_href=tenant.inquiry_path,
            back_label="New Search",
        )

    # ---- fault middleware -------------------------------------------------------------
    @app.middleware("http")
    async def faults_and_headers(request: Request, call_next: Any) -> Response:
        path = request.url.path
        if not path.startswith(("/__admin", "/static")):
            slow = state.faults.take("slow", path)
            if slow:
                await asyncio.sleep(slow.delay_ms / 1000)
            if state.faults.take("expire", path):
                state.sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)  # session times out now
            if state.faults.take("error500", path):
                return render("error500.html", status=500, request_id=secrets.token_hex(8))
            if request.method == "POST":
                state.posts.append(path)
        response: Response = await call_next(request)
        if not path.startswith(("/__admin", "/static")):
            late = state.faults.take("slow_response", path)
            if late:  # the work is done (a commit has happened); only the answer is late
                await asyncio.sleep(late.delay_ms / 1000)
        response.headers["Cache-Control"] = "no-store"
        return response

    # ---- sign on / frameset -------------------------------------------------------------
    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse("/signon", status_code=302)

    @app.get("/signon")
    async def signon_page(expired: int = 0) -> HTMLResponse:
        return render("signon.html", expired=bool(expired))

    @app.post("/signon")
    async def signon(txtOpr: str = Form(""), txtPwd: str = Form("")) -> Response:  # noqa: N803
        opr = txtOpr.strip()
        if state.failed_signons.get(opr, 0) >= MAX_FAILED_SIGNONS:
            return render("signon.html", error="Operator ID is locked. Contact your administrator.")
        if (opr, txtPwd) != creds:
            state.failed_signons[opr] = state.failed_signons.get(opr, 0) + 1
            return render("signon.html", error="Invalid operator ID or password.")
        state.failed_signons.pop(opr, None)
        sid = secrets.token_urlsafe(18)
        state.sessions[sid] = OperatorSession(operator=opr.upper(), last_seen=time.time())
        resp = RedirectResponse("/core/main", status_code=303)
        resp.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="lax")
        return resp

    @app.get("/core/signoff")
    async def signoff(request: Request) -> RedirectResponse:
        state.sessions.pop(request.cookies.get(SESSION_COOKIE, ""), None)
        return RedirectResponse("/signon", status_code=302)

    @app.get("/core/main")
    async def main_frameset(request: Request) -> Response:
        return render("main.html") if session_of(request) else expired()

    @app.get("/core/banner")
    async def banner(request: Request) -> Response:
        sess = session_of(request)
        if not sess:
            return expired()
        return render("banner.html", operator=sess.operator, today=date.today().strftime("%m/%d/%Y"))

    @app.get("/core/nav")
    async def nav(request: Request) -> Response:
        return render("nav.html") if session_of(request) else expired()

    @app.get("/core/welcome")
    async def welcome(request: Request) -> Response:
        sess = session_of(request)
        return work(request, "welcome.html", operator=sess.operator) if sess else expired()

    # ---- member inquiry (route and labels differ per vendor version) ---------------------
    async def inquiry_get(request: Request) -> Response:
        if not session_of(request):
            return expired()
        return work(request, "inquiry.html", mbr="", lname="", error=None, results=None)

    async def inquiry_post(request: Request, txtMbr: str = Form(""), txtLName: str = Form("")) -> Response:  # noqa: N803
        if not session_of(request):
            return expired()
        mbr, lname = txtMbr.strip(), txtLName.strip()

        def again(error: str | None = None, results: list[Member] | None = None) -> HTMLResponse:
            return work(request, "inquiry.html", mbr=mbr, lname=lname, error=error, results=results)

        if not mbr and not lname:
            return again("Enter a Member # or Last Name.")
        if mbr:
            if not mbr.isdigit():
                return again("Member # must be numeric.")
            m = state.members.get(mbr)
            if m is None:
                return again(f"No member found for Member # {mbr}.")
            if m.restricted:
                return restricted(request)
            return member_page(request, m)  # postback: URL stays the same
        found = [
            m
            for m in state.members.values()
            if m.name.split(",")[0].lower().startswith(lname.lower()) and not m.restricted
        ]
        if not found:
            return again(f"No members found with last name {lname}.")
        return again(results=found)

    app.add_api_route(tenant.inquiry_path, inquiry_get, methods=["GET"])
    app.add_api_route(tenant.inquiry_path, inquiry_post, methods=["POST"])

    @app.get("/core/member/{number}")
    async def member_get(request: Request, number: str) -> Response:
        if not session_of(request):
            return expired()
        m = state.members.get(number)
        if m is None:
            return message(request, "Member Summary", f"No member found for Member # {number}.", red=True)
        return restricted(request) if m.restricted else member_page(request, m)

    # ---- open new share: form -> review -> confirm (-> supervisor override) -> receipt ---
    def new_share_form(request: Request, m: Member, errors: list[str], form: dict[str, str]) -> HTMLResponse:
        return work(request, "newshare.html", m=m, errors=errors, form=form)

    @app.get("/core/member/{number}/newshare")
    async def newshare_get(request: Request, number: str) -> Response:
        if not session_of(request):
            return expired()
        m = state.members.get(number)
        if m is None or m.restricted:
            return message(request, "Open New Share", "Member not available.", red=True)
        return new_share_form(request, m, [], {"ddlType": "", "txtAmt": "", "txtNick": ""})

    @app.post("/core/member/{number}/newshare")
    async def newshare_post(
        request: Request,
        number: str,
        ddlType: str = Form(""),  # noqa: N803
        txtAmt: str = Form(""),
        txtNick: str = Form(""),
    ) -> Response:  # noqa: N803
        sess = session_of(request)
        if not sess:
            return expired()
        m = state.members.get(number)
        if m is None or m.restricted:
            return message(request, "Open New Share", "Member not available.", red=True)
        form = {"ddlType": ddlType, "txtAmt": txtAmt, "txtNick": txtNick}
        errors: list[str] = []
        if ddlType not in tenant.share_types:
            errors.append("Share Type is required.")
        amount: Decimal | None = None
        try:
            amount = Decimal(txtAmt.replace("$", "").replace(",", "").strip())
            if amount <= 0 or amount.as_tuple().exponent < -2:  # type: ignore[operator]
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            errors.append("Initial Deposit must be a valid amount.")
            amount = None
        s01 = m.share("S01")
        if amount is not None and (s01 is None or amount > s01.available):
            errors.append("Initial Deposit exceeds the available balance in PRIMARY SAVINGS.")
        nick = txtNick.strip()
        if nick and not re.fullmatch(r"[A-Za-z0-9 ]{1,20}", nick):
            errors.append("Nickname may contain letters, numbers and spaces only.")
        if errors or amount is None:
            return new_share_form(request, m, errors, form)
        tok = secrets.token_hex(8)
        p = PendingShare(tok, m.number, ddlType, tenant.share_types[ddlType], amount, nick)
        sess.pending[tok] = p
        return work(request, "review.html", p=p)  # postback: URL stays .../newshare

    def commit(p: PendingShare, supervisor: bool) -> dict[str, Any]:
        m = state.members[p.member]
        s01 = m.share("S01")
        assert s01 is not None
        s01.balance -= p.amount
        s01.available -= p.amount
        share_id = f"S{state.next_share_seq}"
        state.next_share_seq += 1
        m.shares.append(Share(share_id, p.type_label.upper(), p.amount, p.amount, nickname=p.nickname))
        p.done = True
        receipt = {
            "confirmation": "CN" + "".join(secrets.choice(_CONF_ALPHABET) for _ in range(8)),
            "member": p.member,
            "share_id": share_id,
            "type_label": p.type_label,
            "amount_text": p.amount_text,
            "supervisor": supervisor,
        }
        state.receipts.append(receipt)
        return receipt

    @app.post("/core/member/{number}/newshare/confirm")
    async def newshare_confirm(
        request: Request, number: str, tok: str = Form(""), btnCancel: str | None = Form(None)
    ) -> Response:  # noqa: N803
        sess = session_of(request)
        if not sess:
            return expired()
        m = state.members.get(number)
        if m is None:
            return message(request, "Open New Share", "Member not available.", red=True)
        if btnCancel is not None:
            sess.pending.pop(tok, None)
            return member_page(request, m)
        p = sess.pending.get(tok)
        if p is None or p.done:
            return message(
                request, "Open New Share", "This request has already been processed or has expired.", red=True
            )
        if p.amount > APPROVAL_LIMIT:
            return work(request, "override.html", p=p, error=None)
        return work(request, "receipt.html", r=commit(p, supervisor=False))

    @app.post("/core/member/{number}/newshare/override")
    async def newshare_override(
        request: Request,
        number: str,
        tok: str = Form(""),  # noqa: N803
        txtSupPin: str = Form(""),
        btnCancel: str | None = Form(None),
    ) -> Response:  # noqa: N803
        sess = session_of(request)
        if not sess:
            return expired()
        m = state.members.get(number)
        p = sess.pending.get(tok)
        if m is None or p is None or p.done:
            return message(
                request, "Open New Share", "This request has already been processed or has expired.", red=True
            )
        if btnCancel is not None:
            sess.pending.pop(tok, None)
            return member_page(request, m)
        if txtSupPin != pin:
            return work(request, "override.html", p=p, error="Invalid supervisor PIN.")
        return work(request, "receipt.html", r=commit(p, supervisor=True))

    # ---- other menu items ----------------------------------------------------------------
    @app.get("/core/teller")
    async def teller(request: Request) -> Response:
        if not session_of(request):
            return expired()
        return message(request, "Teller Drawer", "This function is not available in this environment.")

    @app.get("/core/reports")
    @app.get("/core/gl")
    async def not_authorized(request: Request) -> Response:
        if not session_of(request):
            return expired()
        return message(request, "Not Authorized", "You are not authorized to use this function.", red=True)

    @app.get("/static/btn/{name}.svg")
    async def button_image(name: str) -> Response:
        label = _BUTTON_LABELS.get(name, name)
        w = 16 + 7 * len(label)
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="24">'
            '<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
            '<stop offset="0" stop-color="#fdfdfd"/><stop offset="1" stop-color="#c9d3e0"/></linearGradient></defs>'
            f'<rect x="0.5" y="0.5" width="{w - 1}" height="23" rx="3" fill="url(#g)" stroke="#6d7f99"/>'
            f'<text x="{w / 2}" y="16" font-family="Arial" font-size="12" text-anchor="middle" fill="#1f3a5f">'
            f"{label}</text></svg>"
        )
        return Response(svg, media_type="image/svg+xml")

    # ---- admin test hooks (token-protected; never allowlisted for automation) -------------
    def check_admin(value: str | None) -> None:
        if not value or not secrets.compare_digest(value, token):
            raise HTTPException(status_code=403, detail="admin token required")

    @app.post("/__admin/faults")
    async def arm_fault(req: FaultRequest, x_admin_token: str | None = Header(None)) -> JSONResponse:
        check_admin(x_admin_token)
        state.faults.arm(Fault(req.kind, req.count, req.path_prefix, req.delay_ms, req.message))
        return JSONResponse({"armed": state.faults.describe()})

    @app.post("/__admin/expire-sessions")
    async def expire_sessions(x_admin_token: str | None = Header(None)) -> JSONResponse:
        check_admin(x_admin_token)
        n = len(state.sessions)
        state.sessions.clear()
        return JSONResponse({"expired": n})

    @app.post("/__admin/reset")
    async def reset(x_admin_token: str | None = Header(None)) -> JSONResponse:
        check_admin(x_admin_token)
        state.reset()
        return JSONResponse({"reset": True})

    @app.get("/__admin/state")
    async def admin_state(x_admin_token: str | None = Header(None)) -> JSONResponse:
        check_admin(x_admin_token)
        return JSONResponse(
            {
                "tenant": tenant.id,
                "version": tenant.version,
                "members": {
                    n: [
                        {
                            "share_id": s.share_id,
                            "description": s.description,
                            "balance": str(s.balance),
                            "nickname": s.nickname,
                        }
                        for s in m.shares
                    ]
                    for n, m in state.members.items()
                },
                "receipts": state.receipts,
                "posts": state.posts,
                "faults": state.faults.describe(),
                "sessions": len(state.sessions),
            }
        )

    return app
