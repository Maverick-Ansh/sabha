"""Screenshots of the continuous-research UI in both themes, for checking it with nobody at the screen.

    python tests/ui_new.py --base http://127.0.0.1:8787/ --token TOKEN --out web/_shots --tag n

For each theme (light, dark) and width (desktop 1440, phone 390): the Research view (run strip, waves,
the Instant / Continuous form), the living report, long-term memory, and the Crew slide-over's Models and
Alerts tabs. Prints every console error. Files land in --out as <tag>_<theme>_<width>_<name>.jpg.
"""
import argparse
import asyncio
import os

from playwright.async_api import async_playwright

SIZES = {"d": (1440, 900), "m": (390, 844)}


async def main(a):
    os.makedirs(a.out, exist_ok=True)
    tok = f"&token={a.token}" if a.token else ""
    errors = []
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--no-sandbox"])
        for theme in a.themes.split(","):
            for sz in a.sizes.split(","):
                w, h = SIZES[sz]
                ctx = await b.new_context(viewport={"width": w, "height": h}, device_scale_factor=1)
                await ctx.add_init_script(f"try {{ localStorage.setItem('sabha.theme', '{theme}') }} catch (e) {{}}")
                page = await ctx.new_page()
                where = f"{theme}/{sz}"
                page.on("console", lambda m, wh=where: errors.append(f"{wh}: {m.text[:200]}") if m.type == "error" else None)
                page.on("pageerror", lambda e, wh=where: errors.append(f"{wh}: PAGEERROR {str(e)[:300]}"))

                async def snap(name, full=False):
                    await page.screenshot(path=os.path.join(a.out, f"{a.tag}_{theme}_{sz}_{name}.jpg"), type="jpeg", quality=70, full_page=full)

                async def press(text):
                    loc = page.get_by_role("button", name=text).first
                    await loc.click(timeout=4000)
                    await page.wait_for_timeout(1200)

                await page.goto(a.base + "#/watch" + tok)  # the orbit is drawn on a canvas: theme colours are set in JS
                await page.wait_for_timeout(3500)
                await snap("watch")
                await page.goto(a.base + "#/research" + tok)
                await page.wait_for_timeout(4500)
                await snap("research")
                await page.mouse.wheel(0, 700)
                await page.wait_for_timeout(600)
                await snap("research_scroll")
                for name, button in (("report", "Report"), ("memory", "Memory")):
                    try:
                        await page.mouse.wheel(0, -2000)
                        await press(button)
                        await page.wait_for_timeout(1500)
                        await snap(name)
                        await page.keyboard.press("Escape")  # every sheet closes on Escape
                        await page.wait_for_timeout(700)
                    except Exception as e:
                        errors.append(f"{where}: could not open {name}: {str(e)[:120]}")
                try:
                    await page.locator(".crew-btn").first.click(timeout=4000)
                    await page.wait_for_timeout(800)
                    for tab in ("Models", "Alerts"):
                        await page.locator(".so-head .seg button", has_text=tab).first.click(timeout=4000)
                        await page.wait_for_timeout(2500)
                        await snap("crew_" + tab.lower())
                except Exception as e:
                    errors.append(f"{where}: crew tabs: {str(e)[:160]}")
                await ctx.close()
        await b.close()
    for e in errors[:40]:
        print("ERR", e)
    print("UI_NEW_DONE", a.tag, len(errors), "errors")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--token", default="")
    ap.add_argument("--out", default="web/_shots")
    ap.add_argument("--tag", default="n")
    ap.add_argument("--themes", default="light,dark")
    ap.add_argument("--sizes", default="d,m")
    asyncio.run(main(ap.parse_args()))
