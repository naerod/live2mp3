"""Parcours visiteur (Playwright) : 3 rôles × 3 largeurs × pages publiques.

Usage : uvicorn backend.main:app --port 8765 (L2M_RATE_READ=0), puis
    python scripts/e2e_visitor.py [http://localhost:8765]
Les en-têtes Authentik sont simulés. Contrôles : statut HTTP, débordement
horizontal, lang, boutons sans nom accessible, images sans alt, erreurs JS
(hors ressources externes bloquées). Code retour ≠ 0 si un contrôle échoue.
"""
import sys

from playwright.sync_api import sync_playwright

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765").rstrip("/")
ROLES = {
    "anonyme": {},
    "connecte": {"X-authentik-username": "newbie", "X-authentik-groups": "authentik Default"},
    "live2mp3-user": {"X-authentik-username": "alice", "X-authentik-groups": "live2mp3-user"},
}
PAGES = ["/", "/changelog", "/notifications", "/settings", "/u/alice"]
WIDTHS = [320, 768, 1280]

CHECK_JS = """() => {
  const doc = document.documentElement;
  const overflow = doc.scrollWidth - doc.clientWidth;
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
                     return r.width > 0 && r.height > 0 && s.visibility !== 'hidden'; };
  const noName = [...document.querySelectorAll('button, a[href], [role=button]')]
    .filter(vis).filter(e => !(e.getAttribute('aria-label') || e.getAttribute('title')
      || (e.innerText || '').replace(/\\b[a-z]+(_[a-z]+)+\\b/g, '').trim()
      || e.querySelector('img[alt]:not([alt=""])')))
    .map(e => e.outerHTML.slice(0, 90));
  const noAlt = [...document.querySelectorAll('img:not([alt])')].map(e => e.src.slice(-60));
  return {overflow, lang: doc.lang, noName: noName.slice(0, 5), nNoName: noName.length, noAlt};
}"""

problems = []
with sync_playwright() as p:
    import os
    exe = os.environ.get("PW_CHROMIUM")
    browser = p.chromium.launch(**({"executable_path": exe} if exe else {}))
    for role, headers in ROLES.items():
        for w in WIDTHS:
            ctx = browser.new_context(viewport={"width": w, "height": 900},
                                      extra_http_headers=headers)
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)[:140]))
            for path in PAGES:
                errors.clear()
                r = page.goto(BASE + path, wait_until="networkidle", timeout=20000)
                page.wait_for_timeout(300)
                res = page.evaluate(CHECK_JS)
                tag = f"[{role} {w}px {path}]"
                if r is None or r.status >= 500:
                    problems.append(f"{tag} HTTP {r.status if r else '?'}")
                if res["overflow"] > 1:
                    problems.append(f"{tag} débordement horizontal {res['overflow']}px")
                if not res["lang"]:
                    problems.append(f"{tag} <html> sans lang")
                if res["nNoName"]:
                    problems.append(f"{tag} {res['nNoName']} contrôle(s) sans nom : {res['noName'][:2]}")
                if res["noAlt"]:
                    problems.append(f"{tag} img sans alt : {res['noAlt'][:2]}")
                for e in errors:
                    problems.append(f"{tag} erreur JS : {e}")
            ctx.close()
    browser.close()

print("\n".join(sorted(set(problems))) or "OK : aucun problème détecté")
sys.exit(1 if problems else 0)
