"""The Queue page (/jobs), retrying a job, and the stamp pages poll to know when to reload."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from chatwatcher import jobs
from chatwatcher.web import listing, queries
from chatwatcher.web.routes import Ctx, redirect


def router(ctx: Ctx) -> APIRouter:
    r = APIRouter()
    render, get_conn = ctx.render, ctx.get_conn

    @r.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request, q: str = "", status: str = "", stage: str = "", page: str = "",
                  conn=Depends(get_conn)):
        status = status if status in listing.JOB_STATUS else ""
        found = listing.job_list(conn, q=q, status=status, stage=stage, page=listing.to_int(page, 1))
        return render(request, "jobs.html", page=found, rows=found.items, counts=listing.job_counts(conn),
                      stages=listing.job_stages(conn), flt={"q": q.strip(), "status": status, "stage": stage})

    @r.post("/jobs/{pk}/retry")
    def job_retry(pk: int, conn=Depends(get_conn)):
        j = conn.execute("SELECT * FROM jobs WHERE id = ?", (pk,)).fetchone()
        if j is None:
            raise HTTPException(404)
        jobs.enqueue(conn, j["video_id"], j["stage"], moment_pk=j["moment_id"])
        return redirect("/jobs", msg="Queued again.")

    @r.get("/api/version")
    def api_version(conn=Depends(get_conn)):
        return JSONResponse({"v": queries.version_stamp(conn), "worker": ctx.worker_status()})

    return r
