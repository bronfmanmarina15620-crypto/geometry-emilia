"""Cloud-backup test for the geometry app. Supabase is fully intercepted
(no real network). Run: python3 tools/test_cloud.py"""
import json, copy
from playwright.sync_api import sync_playwright
URL = "file:///workspace/geometry/index.html"
HUB = "file:///tmp/hubpatched/index.html"   # hub + tools/hub-read-geometry-cloud.patch
KEY = "geometry_emilia_v1"
OLD = {"skills":{"זוויות: ישר זווית":{"seen":4,"correct":3,"wrong":1,"label":"זוויות: ישר זווית"}},
       "sessions":[{"start":1762500000000,"ms":300000,"q":15,"correct":12,"best":6,"weakSkills":["זוויות: ישר זווית"],"ids":["g1"]}],
       "answers":15,"lastScore":{"correct":12,"total":15,"acc":0.8,"at":1762500300000}}
OTHER = {"skills":{"מרובעים: זיהוי":{"seen":1,"correct":1,"wrong":0,"label":"מרובעים: זיהוי"}},"answers":1,
         "sessions":[{"start":1763000000000,"ms":20000,"q":1,"correct":1,"best":1,"topic":"quad","firstTry":1,"adult":0,
           "items":[{"id":"m1","q":"איזה מרובע זה?","topic":"quad","skill":"מרובעים: זיהוי","picks":["ריבוע"],"attempts":1,"sec":4.2,"outcome":"first","at":1763000004200}],
           "askIds":[],"weakSkills":[],"ids":["m1"]},
           {"start":1763000100000,"ms":0,"q":0,"correct":0,"items":[]}]}
store = {}; posts = []; gets = []; other_rows = []
def handler(route):
    r = route.request
    if "/rest/v1/kefel_state" not in r.url: return route.abort()
    if r.method == "GET":
        gets.append(r.url)
        if "id=eq.emilia-geometry" not in r.url:
            other_rows.append(r.url)
            return route.fulfill(status=200, content_type="application/json", body="[]")
        row = store.get("emilia-geometry")
        return route.fulfill(status=200, content_type="application/json", body=json.dumps([row] if row else []))
    body = json.loads(r.post_data)
    if body["id"] != "emilia-geometry": other_rows.append(body["id"])
    assert r.headers.get("apikey","").startswith("eyJ") and "merge-duplicates" in r.headers.get("prefer","")
    posts.append(body); store[body["id"]] = {"data": body["data"], "updated_at": body["updated_at"]}
    return route.fulfill(status=201, body="")

def hub_handler(route):
    r = route.request
    if r.method != "GET": return route.abort()           # hub is read-only
    row = store.get("emilia-geometry") if "id=eq.emilia-geometry" in r.url else None
    return route.fulfill(status=200, content_type="application/json", body=json.dumps([row] if row else []))

def play_session(pg, topic):
    pg.click(".topic[data-topic=%s]" % topic)
    for i in range(15):
        q = pg.evaluate("(()=>{var q=play.queue[play.idx];return {answer:q.answer,options:q.options,idx:play.idx}})()")
        wrong = [o for o in q["options"] if o != q["answer"]]
        seq = [q["answer"]]
        if i == 0: seq = wrong[:min(3, len(q["options"])-1)]          # needs an adult
        elif i == 1 and wrong: seq = [wrong[0], q["answer"]]          # after retries
        for t in seq:
            pg.evaluate("t=>{[...document.querySelectorAll('#opts .opt')].find(b=>b.textContent===t).click()}", t)
            pg.wait_for_timeout(60)
        pg.wait_for_function("i=>(play.idx>i)||!document.getElementById('summary').classList.contains('hidden')", arg=q["idx"], timeout=8000)
    assert not pg.locator("#summary").is_hidden()

errors = []
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width":390,"height":844})
    ctx.route("**/*supabase.co/**", handler)
    pg = ctx.new_page()
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type=="error" else None)

    # 1) old-format local + cloud already holding another device's session
    store["emilia-geometry"] = {"data": OTHER, "updated_at": "2026-11-10T10:00:00Z"}
    pg.goto(URL); pg.evaluate("d=>localStorage.setItem('%s', JSON.stringify(d))" % KEY, OLD); pg.reload()
    pg.wait_for_function("cloudState.lastOk>0", timeout=5000)
    loc = pg.evaluate("JSON.parse(localStorage.getItem('%s'))" % KEY)
    starts = [s["start"] for s in loc["sessions"]]
    assert starts == [1762500000000, 1763000000000], starts                  # abandoned q=0 cloud session skipped
    assert loc["sessions"][0] == OLD["sessions"][0], "old local session changed"
    assert loc["skills"]["זוויות: ישר זווית"] == OLD["skills"]["זוויות: ישר זווית"]
    assert pg.inner_text("#hSessions") == "2"
    print("1 old-format local + cloud merge: OK (both histories kept, old session byte-identical)")

    # 2) full quad session, payload check
    before = copy.deepcopy(loc)
    play_session(pg, "quad")
    pg.wait_for_timeout(2500)
    data = posts[-1]["data"]
    loc = pg.evaluate("JSON.parse(localStorage.getItem('%s'))" % KEY)
    assert data == loc, "cloud payload != local data"
    for s in before["sessions"]: assert s in loc["sessions"], "lost a local session"
    sess = data["sessions"][-1]
    assert len(sess["items"]) == 15 and sess["q"] == 15
    need = {"id","q","topic","skill","picks","attempts","sec","outcome","at"}
    for it in sess["items"]:
        assert need <= set(it), set(it)
        assert it["q"] and isinstance(it["picks"], list) and it["attempts"] == len(it["picks"])
    oc = [it["outcome"] for it in sess["items"]]
    assert oc[0] == "adult" and oc[1] == "retry" and oc.count("first") == 13, oc
    print("2 full session: OK, %d POSTs, payload == local; outcomes %s" % (len(posts), {o: oc.count(o) for o in set(oc)}))
    print("  sample item:", json.dumps(sess["items"][1], ensure_ascii=False))
    assert not other_rows, other_rows   # geometry app touched only its own row

    # 3) offline: Supabase unreachable -> silent, app still works, local saved
    ctx.unroute("**/*supabase.co/**"); ctx.route("**/*supabase.co/**", lambda r: r.abort())
    n_before = len(loc["sessions"])
    pg.reload(); pg.wait_for_function("cloudState.lastErr>0", timeout=12000)
    play_session(pg, "tri")
    loc2 = pg.evaluate("JSON.parse(localStorage.getItem('%s'))" % KEY)
    assert len(loc2["sessions"]) == n_before + 1 and loc2["sessions"][:n_before] == loc["sessions"]
    pg.click("#btnSumHome"); pg.click("#btnParentHome"); pg.fill("#pinInput", "2604"); pg.wait_for_timeout(800)
    assert "אין רשת" in pg.inner_text("#parentBody") or "גיבוי בענן" in pg.inner_text("#parentBody")
    pg.screenshot(path="/workspace/geometry/shots/5-parents-offline.png", full_page=False)
    print("3 offline session: OK (saved locally, no UI break)")

    # 4) back online -> offline session reaches the cloud, nothing lost
    ctx.unroute("**/*supabase.co/**"); ctx.route("**/*supabase.co/**", handler)
    pg.reload(); pg.wait_for_function("cloudState.lastOk>0", timeout=5000)
    cl = store["emilia-geometry"]["data"]
    assert [s["start"] for s in cl["sessions"]] == [s["start"] for s in loc2["sessions"]]
    print("4 back online: OK (cloud has all %d sessions)" % len(cl["sessions"]))

    # 5) hub (patched) reads the cloud row on a device with NO local geometry data
    hctx = b.new_context(viewport={"width":390,"height":844}); hctx.route("**/*supabase.co/**", hub_handler)
    hp = hctx.new_page(); hp.on("pageerror", lambda e: errors.append("hub: "+str(e)))
    hp.goto(HUB); hp.wait_for_timeout(300)
    res = hp.evaluate("""LimudimHub.loadAll().then(()=>{var st=LimudimHub.state();var g=st.events.filter(e=>e.app==='geometry');
        return {src:st.sources.geometry, n:g.length, withQ:g.filter(e=>e.question&&e.question.indexOf('תרגיל ב«')<0).length,
                oc:g.reduce((a,e)=>(a[e.outcome]=(a[e.outcome]||0)+1,a),{}), ex:g[0]}})""")
    print("5 hub:", json.dumps({k:res[k] for k in ("n","withQ","oc")}, ensure_ascii=False), "hasCloud=", res["src"]["hasCloud"])
    assert res["src"]["hasCloud"] and res["n"] == 31 and res["withQ"] == 31
    # unpatched hub parser on the same payload (local-device path)
    up = hctx.new_page(); up.goto("file:///workspace/amelia-hub/index.html"); up.wait_for_timeout(300)
    n_un = up.evaluate("d=>LimudimHub.parse.geometry(d).length", cl)
    print("  unpatched hub parseGeometry on payload:", n_un, "events (question shown as skill label)")
    b.close()
print("page errors:", errors)
assert not [e for e in errors if "ERR_FAILED" not in e and "Failed to load resource" not in e], errors
print("ALL CLOUD TESTS PASSED")
