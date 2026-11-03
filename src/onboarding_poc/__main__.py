"""python -m onboarding_poc [serve|seed]"""
import sys

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "seed":
        from onboarding_poc.seed import seed
        print(seed())
    else:
        import uvicorn
        uvicorn.run("onboarding_poc.api.app:get_app", factory=True, host="0.0.0.0", port=int(sys.argv[2]) if len(sys.argv) > 2 else 8000)
