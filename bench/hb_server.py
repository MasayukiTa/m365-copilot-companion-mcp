"""Local httpbin server for SWE-bench requests instances. Run one per port:
   python3 hb_server.py http    -> http  on :80
   python3 hb_server.py https   -> https on :443 (cert /opt/hb/cert.pem)
Kept in a FILE (not python -c) because the Windows->wsl->sh layering mangles inline -c quoting."""
import sys
from httpbin import app

mode = sys.argv[1] if len(sys.argv) > 1 else "http"
if mode == "https":
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain("/opt/hb/cert.pem", "/opt/hb/key.pem")
    app.run(host="0.0.0.0", port=443, ssl_context=ctx, threaded=True)
else:
    app.run(host="0.0.0.0", port=80, threaded=True)
