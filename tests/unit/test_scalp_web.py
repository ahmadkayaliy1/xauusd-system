"""D-058: the Scalp tab's static page (web/index.html, app.js, style.css) — the tab button and section, the three
functions defined and bound, every free-text field of the brief rendered only through ``esc()`` / ``txt()`` (the page
has no script-src CSP: esc() is the only XSS defence), Arabic prose right-to-left, the safety list always rendered,
"Run now" through the token header with ONE reload after 90 s (D-007: the browser never polls)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "web" / "static" / "style.css").read_text(encoding="utf-8")
BOOT = "/* ------------------------------------------------------------------ boot"
SCALP_JS = JS.split("D-058: the Scalp tab", 1)[1].split(BOOT, 1)[0]
# the brief's free-text fields (contract.ScalpBrief / ScalpLevel / ScalpPlan) plus the record's markdown and the
# checks' notes: whatever the model (or a tool) wrote
TEXT_FIELDS = ("picture", "expect", "why", "trigger", "invalidation", "no_scalp", "news_clock", "desk_warning", "trade_call", "manage_reason",
               "confidence_note", "md", "notes")
SAFE_WRAPPERS = {"esc", "txt", "scList"}


def _wrapped(text: str, at: int) -> bool:
    """Is the ``.field`` at ``at`` inside an esc( / txt( / scList( call on its line (nesting allowed)?"""
    depth = 0
    i = at - 1
    while i >= 0 and text[i] != "\n":
        ch = text[i]
        if ch == ")":
            depth += 1
        elif ch == "(":
            if depth == 0:
                m = re.search(r"(\w+)\s*$", text[:i])
                if m and m.group(1) in SAFE_WRAPPERS:
                    return True
            else:
                depth -= 1
        elif ch == "{" and depth == 0 and i > 0 and text[i - 1] == "$":
            return False                                   # a raw ${…} slot
        i -= 1
    return False


def test_tab_button_and_section():
    assert '<button data-tab="scalp">Scalp</button>' in HTML
    assert '<section id="tab-scalp" class="tab">' in HTML
    section = HTML.split('<section id="tab-scalp"', 1)[1].split("</section>", 1)[0]
    for el in ('id="sc-refresh"', 'id="sc-run"', 'id="sc-status"', 'id="sc-body"', 'id="sc-detail"'):
        assert el in section, el
    assert ">Run now…</button>" in section and 'id="sc-mode"' in section and 'id="sc-save"' in section   # D-061


def test_functions_defined_hooked_and_bound():
    for fn in ("loadScalp", "showScalp", "runScalp", "saveScalpSettings"):
        assert re.search(rf"^async function {fn}\(", JS, re.M), fn
    assert 'if (b.dataset.tab === "scalp") loadScalp();' in JS
    boot = JS.split("(async function boot()", 1)[1]
    assert '$("#sc-refresh").addEventListener("click", loadScalp);' in boot
    assert '$("#sc-run").addEventListener("click", runScalp);' in boot
    assert '$("#sc-save").addEventListener("click", saveScalpSettings);' in boot
    assert 'tr[data-id]' in boot and "showScalp(tr.dataset.id)" in boot        # one delegated listener on #sc-body


def test_every_free_text_field_goes_through_esc_or_txt():
    helper = 'scList = (xs, n = 300) => (Array.isArray(xs) ? xs : []).map((x) => `<li>${esc(txt(x, n))}</li>`)'
    assert helper in SCALP_JS
    for field in TEXT_FIELDS:
        hits = [m.start() for m in re.finditer(rf"\.{field}\b(?!_)", SCALP_JS)]
        assert hits, field
        for at in hits:
            line = SCALP_JS[SCALP_JS.rfind("\n", 0, at) + 1:SCALP_JS.find("\n", at)]
            assert _wrapped(SCALP_JS, at), f".{field} rendered raw: {line.strip()[:120]}"
    assert "innerHTML" in SCALP_JS and ".insertAdjacentHTML" not in SCALP_JS
    assert "<a " not in SCALP_JS                                         # never a link from the model's text


def test_arabic_prose_is_right_to_left_and_the_safety_list_always_rendered():
    assert SCALP_JS.count('<p class="prewrap ar" dir="auto">') >= 2             # picture, expect
    assert 'class="prewrap ar fail" dir="auto">' in SCALP_JS                   # the desk warning
    load = SCALP_JS.split("async function loadScalp()", 1)[1].split("async function showScalp", 1)[0]
    assert "${scSafetyHTML(d)}" in load                                        # unconditional, brief or not
    safety = SCALP_JS.split("function scSafetyHTML(d)", 1)[1].split("\n}\n", 1)[0]
    assert "d.safety" in safety and "esc(x.ar)" in safety and "esc(x.en)" in safety and "risk_cap" in safety
    assert "No brief yet" in SCALP_JS                                          # the empty state keeps the page
    for rule in ("#tab-scalp td { overflow-wrap: anywhere; min-width: 0; }",
                 ".ar { direction: rtl; text-align: right; }", ".conf-low { color: var(--muted); }",
                 ".rejected { opacity: .55; }"):
        assert rule in CSS, rule
    assert 'class="${rej ? "rejected" : ""}' in SCALP_JS and "conf-low" in SCALP_JS


def test_run_now_posts_with_the_token_and_reloads_once_after_90_s():
    run = SCALP_JS.split("async function runScalp()", 1)[1]
    assert run.lstrip().startswith("{\n  if (!confirm(")
    assert 'api("/api/scalp/run", { method: "POST", headers: { "X-Dashboard-Token": TOKEN } })' in run
    assert run.count("setTimeout(loadScalp, 90_000)") == 1
    assert "setInterval" not in SCALP_JS                                       # no polling (D-007)
    assert SCALP_JS.count("setTimeout(") == 1
    assert 'api("/api/scalp?limit=30")' in SCALP_JS and "/api/scalp/${encodeURIComponent(id)}" in SCALP_JS
    assert "/api/scalp/charts/${encodeURIComponent(c.tf)}?t=${encodeURIComponent(c.updated_ms)}" in SCALP_JS
