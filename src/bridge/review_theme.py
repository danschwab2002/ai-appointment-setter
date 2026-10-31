"""La estetica de las paginas que ve una persona del cliente: la de Chatwoot.

La usan dos servicios del mismo paquete: la revision diaria
(``daily_feedback_service``, imagen ``daily-feedback``) y la pagina de
aprobacion de cambios de flujo del bridge (``flow_approval``). Las dos imagenes
copian ``src`` entero, asi que el modulo viaja en las dos.

Es CSS puro, sin red: las paginas tienen ``default-src 'none'`` y
``style-src 'unsafe-inline'`` (sin fuentes externas ni scripts). Solo hay modo
oscuro, como Chatwoot y la revision diaria (``color-scheme: dark``).

``REVIEW_SHELL_CSS`` son los tokens, el cuerpo, la barra de arriba y su marca;
``REVIEW_BUTTONS_CSS``, los botones. La revision diaria los arma tal cual
estaban antes de sacarlos aca (mismos bytes: lo fija un test).
"""

from __future__ import annotations

__all__ = ["REVIEW_BUTTONS_CSS", "REVIEW_SHELL_CSS"]

REVIEW_SHELL_CSS = (
    ':root{--cw-bg:#111214;--cw-sidebar:#18191d;--cw-surface:#17181c;--cw-surface-raised:#202127;--cw-hover:#26272e'
    ';--cw-border:#2a2b32;--cw-border-soft:#222329;--cw-text:#f1f1f3;--cw-muted:#a8a9b2;--cw-subtle:#898b95;--cw-ac'
    'cent:#1976d2;--cw-accent-hover:#1568ba;--cw-indigo:#34357f;--cw-radius:12px;--cw-radius-sm:8px}\n'
    '*{box-sizing:border-box}html{color-scheme:dark}'
    'body.app-shell{margin:0;min-height:100vh;background:var(--cw-bg);color:var(--cw-text);font-family:Inter,ui-san'
    's-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:15px;line-height:1.5;-webkit-font-smo'
    'othing:antialiased}\n'
    '.topbar{height:64px;border-bottom:1px solid var(--cw-border-soft);background:var(--cw-surface);display:flex;al'
    'ign-items:center;justify-content:space-between;padding:0 24px;position:sticky;top:0;z-index:10}\n'
    '.brand{display:flex;align-items:center;gap:12px;min-width:0}'
    '.workspace-mark{width:30px;height:30px;border-radius:50%;background:var(--cw-accent);position:relative;box-sha'
    'dow:0 0 0 4px rgba(25,118,210,.1);flex:none}'
    '.workspace-mark::after{content:"";position:absolute;width:13px;height:10px;left:8px;top:8px;background:white;b'
    'order-radius:7px 7px 7px 3px}\n'
    '.brand-name{font-weight:650;letter-spacing:-.01em;white-space:nowrap}'
    '.brand-divider{width:1px;height:24px;background:var(--cw-border);margin:0 2px}'
    '.brand-section{color:var(--cw-muted);font-weight:500;white-space:nowrap}\n'
    '.secure-badge{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--cw-border);background:var('
    '--cw-surface-raised);border-radius:999px;padding:6px 10px;color:var(--cw-muted);font-size:12px;font-weight:600'
    '}'
    '.secure-badge::before{content:"";width:7px;height:7px;border-radius:50%;background:#5fd39a;box-shadow:0 0 0 3p'
    'x rgba(95,211,154,.1)}\n'
)

REVIEW_BUTTONS_CSS = (
    'button,.button{min-height:42px;border:1px solid #373943;border-radius:var(--cw-radius-sm);background:var(--cw-'
    'surface-raised);color:var(--cw-text);padding:10px 14px;font:600 14px/1.2 Inter,ui-sans-serif,-apple-system,Bli'
    'nkMacSystemFont,"Segoe UI",sans-serif;cursor:pointer;text-decoration:none;display:inline-flex;align-items:cent'
    'er;justify-content:center;transition:background .15s,border-color .15s,transform .1s}'
    'button:hover,.button:hover{background:var(--cw-hover);border-color:#484a55}'
    'button:active,.button:active{transform:translateY(1px)}'
    'button.primary,.button.primary{background:var(--cw-accent);border-color:var(--cw-accent);color:white}'
    'button.primary:hover,.button.primary:hover{background:var(--cw-accent-hover);border-color:var(--cw-accent-hove'
    'r)}button.quiet{background:transparent;color:var(--cw-muted);border-color:transparent}'
    'button.quiet:hover{background:var(--cw-hover);color:var(--cw-text)}'
    'button[data-reveal-feedback][aria-expanded="true"]{border-color:var(--cw-accent);box-shadow:0 0 0 2px rgba(25,'
    '118,210,.18)}button:disabled{cursor:not-allowed;opacity:.52;transform:none}'
    'button:disabled:hover{background:var(--cw-surface-raised);border-color:#373943}'
    'button.primary:disabled:hover{background:var(--cw-accent);border-color:var(--cw-accent)}'
    'button:focus-visible,.button:focus-visible{outline:2px solid #80bfff;outline-offset:2px}'
)
