"""python -m onboarding_poc [serve [port] | seed | demo | check-uc]

serve   run the app (API + screens) on http://localhost:8000
seed    write the dummy dataset (MDM, Veritas #4285, landing-zone files) to the configured storage
demo    run one onboarding end to end without the UI, using the local pipeline stand-in
check-uc  check Unity Catalog: identity, warehouse, and each configured table (RB codes, audit, ...)
"""
import sys


def demo() -> None:
    from onboarding_poc.config import get_config
    from onboarding_poc.seed import seed
    from onboarding_poc.service import OnboardingService
    user = "demo@nablon.ai"
    svc = OnboardingService(get_config())
    keys = seed(svc.ctx.cfg, svc.ctx.storage)
    s = svc.intake.create("Demo · Tesco daily", "UK", "TESCO", user)
    svc.intake.add_from_landing(s, [keys["tesco_daily"]], user)
    print(f"1. session {s.session_id}: file added from the landing zone")
    svc.analyse(s, user)
    for x in svc.values.open_issues(s):
        svc.values.decide(s, x["rule_id"], user)
    if s.gates["A"].status != "approved":
        svc.approve_gate_a(s, user, "demo")
    for i in svc.pipeline.status(s, check=False)["items"]:
        print(f"2. Gate A approved: {i['target_table']} SQL -> {i['sql_uri']}")
    svc.pipeline.run_locally(s, user)
    st = svc.pipeline.confirm(s, user, "demo")
    print("3. pipeline (local stand-in) loaded: " + ", ".join(f"{i['target_fqn']}={i['loaded_rows']}" for i in st["items"]))
    svc.run_harmonisation(s, user)
    if s.gates["B"].status != "approved":
        for r in svc.codes.queue(s, only_open=True):
            if r["status"] == "needs_review":
                svc.codes.decide(s, r["match_id"], "accept" if r["rb_code"] else "leave_empty", user)
        for r in svc.standards.open_issues(s, svc.validate.frames(s)):
            svc.standards.decide(s, r["rule_id"], user)
        svc.approve_gate_b(s, user, "demo")
    print("4. Gate B approved: RB codes and value standards decided")
    for r in svc.validate.run(s, user):
        vm = r["veritas_compare"]["match_rate"] if r["veritas_compare"] else None
        print(f"5. validated {r['target_table']}: {r['rows']} rows, blocking={r['blocking']}, Veritas match={vm}")
    m = svc.output.signoff(s, user, "demo")
    for o in m["outputs"]:
        if o.get("fqn"):
            print(f"6. signed off -> {o['fqn']} ({o['rows']} rows)")
    print(f"Done. Open http://localhost:8000/#/s/{s.session_id}/signoff after `python -m onboarding_poc serve`.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "seed":
        from onboarding_poc.config import get_config
        from onboarding_poc.context import Context
        from onboarding_poc.seed import seed
        cfg = get_config()
        ctx = Context(cfg)
        print(seed(cfg, ctx.storage, ctx.warehouse if cfg.mdm.source == "uc" else None))
    elif cmd == "check-uc":
        import json
        from onboarding_poc.config import get_config
        from onboarding_poc.connectivity import check
        from onboarding_poc.context import Context
        res = check(Context(get_config()))
        print(json.dumps(res, indent=2, default=str))
        sys.exit(0 if res["ok"] else 1)
    elif cmd == "demo":
        demo()
    else:
        import os
        import uvicorn
        # Databricks Apps tell the app which port to use; locally the default is 8000
        port = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("DATABRICKS_APP_PORT", "8000"))
        if not os.environ.get("DATABRICKS_APP_PORT"):
            print(f"\n  Open http://localhost:{port} in your browser (Ctrl+C to stop)\n")
        # Locally bind to 127.0.0.1 only. A Databricks App must listen on all interfaces (0.0.0.0)
        # or its proxy cannot reach it. POC_HOST overrides both.
        on_databricks = "DATABRICKS_APP_PORT" in os.environ
        host = os.environ.get("POC_HOST", "0.0.0.0" if on_databricks else "127.0.0.1")
        uvicorn.run("onboarding_poc.api.app:get_app", factory=True, host=host, port=port)
