"""Screenshot tour of the web UI, for checking a restyle without a human at the screen.

Run it on the box (needs `pip install playwright` and `playwright install chromium`):

    python -m sabha --demo --no-auth --port 8790 --data /tmp/sabha_demo &
    python tests/ui_shots.py --base http://127.0.0.1:8790/ --out web/_shots --tag a

It walks every mode at desktop and phone width, dispatches a demo mission to catch the orbit while
agents work, opens the palette, the crew sheet, the mission drawer and a Talk reply, and prints every
console error it saw. `--gate URL` adds a shot of the auth gate (a hub that wants a token).
Files land in --out as <tag>_<name>.jpg; under web/ the hub serves them at /_shots/.
"""
import argparse
import asyncio
import json
import os

from playwright.async_api import async_playwright

VIEWS = ["watch", "talk", "work", "council", "research"]
SIZES = {"d": (1440, 900), "m": (390, 844), "t": (1024, 768)}


async def main(a):
    os.makedirs(a.out, exist_ok=True)
    tok = f"&token={a.token}" if a.token else ""  # the app reads it from the hash and keeps it
    errors = []

    def watch(page, where):
        page.on("console", lambda m: errors.append(f"{where}: {m.text[:200]}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"{where}: PAGEERROR {str(e)[:300]}"))

    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--no-sandbox"])

        # every mode, at each width
        for sz in a.sizes.split(","):
            w, h = SIZES[sz]
            ctx = await b.new_context(viewport={"width": w, "height": h})
            page = await ctx.new_page()
            watch(page, sz)
            await page.goto(a.base + "#/watch" + tok)
            await page.wait_for_timeout(3500)
            for v in VIEWS:
                await page.evaluate("v => { location.hash = '#/' + v }", v)
                await page.wait_for_timeout(2600)
                await page.screenshot(path=f"{a.out}/{a.tag}_{v}_{sz}.jpg", type="jpeg", quality=a.quality)
            if sz == "m":  # the phone page scrolls: catch the floor under the orbit too
                await page.evaluate("location.hash = '#/watch'")
                await page.wait_for_timeout(1200)
                await page.evaluate("document.querySelector('.view.watch').scrollTo(0, 99999)")
                await page.wait_for_timeout(700)
                await page.screenshot(path=f"{a.out}/{a.tag}_watch_m2.jpg", type="jpeg", quality=a.quality)
            await ctx.close()

        # the app in use, desktop
        ctx = await b.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        watch(page, "use")

        async def snap(name):
            await page.screenshot(path=f"{a.out}/{a.tag}_{name}.jpg", type="jpeg", quality=a.quality)

        async def step(name, fn):
            try:
                await fn()
            except Exception as e:  # one broken step must not hide the rest
                print("STEP FAIL", name, str(e)[:240])

        await page.goto(a.base + "#/work" + tok)
        await page.wait_for_timeout(3000)

        async def dispatch():
            await page.locator(".view.work textarea").first.fill("Survey lotus-shaped attention patterns and write a short report")
            await page.get_by_role("button", name="Dispatch").click()
            await page.wait_for_timeout(1500)

        async def live():
            await page.evaluate("location.hash = '#/watch'")
            await page.wait_for_timeout(700)
            await snap("bloom")
            await page.wait_for_timeout(7000)
            await snap("live")

        async def focus():
            await page.locator(".roster .r-row").nth(1).click()
            await page.wait_for_timeout(1500)
            await snap("focus")
            await page.locator(".roster .r-row").nth(0).click()

        async def palette():
            await page.keyboard.press("Control+k")
            await page.wait_for_timeout(900)
            await snap("palette")
            await page.keyboard.press("Escape")

        async def crew():
            await page.locator(".crew-btn").click()
            await page.wait_for_timeout(1200)
            await snap("crew")
            await page.keyboard.press("Escape")

        async def drawer():
            await page.evaluate("location.hash = '#/work'")
            await page.wait_for_timeout(1500)
            await snap("work_live")
            await page.locator(".wcard").first.click()
            await page.wait_for_timeout(1800)
            await snap("drawer")
            await page.keyboard.press("Escape")

        async def talk():
            await page.evaluate("location.hash = '#/talk'")
            await page.wait_for_timeout(1200)
            await page.locator(".talk-input:visible").first.fill("What did the tokenizer mission find?")
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(6000)
            await snap("talk_live")

        for name, fn in [("dispatch", dispatch), ("live", live), ("focus", focus), ("palette", palette),
                         ("crew", crew), ("drawer", drawer), ("talk", talk)]:
            await step(name, fn)

        if a.gate:
            page = await ctx.new_page()
            await page.goto(a.gate)
            await page.wait_for_timeout(3000)
            await page.screenshot(path=f"{a.out}/{a.tag}_gate.jpg", type="jpeg", quality=a.quality)
        await b.close()

    print("console errors:", json.dumps(errors[:12]))
    print("UI_SHOTS_DONE", a.tag)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="http://127.0.0.1:8790/")
    ap.add_argument("--gate", default="")
    ap.add_argument("--token", default="", help="hub token, for a hub that is not --no-auth")
    ap.add_argument("--out", default="web/_shots")
    ap.add_argument("--tag", default="a")
    ap.add_argument("--sizes", default="d,m,t")
    ap.add_argument("--quality", type=int, default=90)
    asyncio.run(main(ap.parse_args()))
