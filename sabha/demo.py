"""--demo seeding: two mock models, a crew of four mock agents, one Talk
thread with Lead, and two missions started at once so there is something to
watch the moment the UI opens. Runs only when the data dir has no state yet.
"""

from __future__ import annotations

CREW = [
    {
        "name": "Lead", "hue": "sun", "model": "Demo-27B",
        "role": "plans the attack and reports back",
        "persona": ("You are Lead, the planner of this crew. You break a goal into small, checkable "
                    "steps and hand each one to the teammate best suited for it. When the work comes "
                    "back you merge it and report in plain words, saying what is done and what is not."),
    },
    {
        "name": "Scout", "hue": "sky", "model": "Demo-4B",
        "role": "reads, searches, gathers facts",
        "persona": ("You are Scout. You read code, papers and docs, and search for what is already "
                    "known before anyone builds anything. You keep notes with sources and you say "
                    "plainly when a claim has only one source."),
    },
    {
        "name": "Builder", "hue": "mint", "model": "Demo-27B",
        "role": "writes and runs code",
        "persona": ("You are Builder. You write the smallest working version first, run it, and fix "
                    "what breaks. You prefer the standard library and you always record one honest "
                    "number about what you built."),
    },
    {
        "name": "Critic", "hue": "lilac", "model": "Demo-4B",
        "role": "tests, reviews, pokes holes",
        "persona": ("You are Critic. You look for the assumption nobody checked and write the "
                    "smallest test that would expose it. You are blunt but fair, and you always say "
                    "what would change your mind."),
    },
]

MISSIONS = [
    ("Builder", "Build a tiny tokenizer from scratch and benchmark it"),
    ("Scout", "Survey JEPA-style world models and summarise the three main ideas"),
]


async def seed_demo(hub) -> None:
    models = {
        "Demo-27B": hub.models.create({
            "name": "Demo-27B", "kind": "mock", "served_name": "demo-27b", "context_window": 32768,
            "notes": "Scripted demo model answered by the hub itself. No GPU needed."}),
        "Demo-4B": hub.models.create({
            "name": "Demo-4B", "kind": "mock", "served_name": "demo-4b", "context_window": 16384,
            "notes": "Smaller scripted demo model. No GPU needed."}),
    }
    agents = {}
    for spec in CREW:
        agents[spec["name"]] = hub.create_agent({
            "name": spec["name"], "role": spec["role"], "persona": spec["persona"],
            "model_id": models[spec["model"]]["id"], "harness": "mock", "hue": spec["hue"],
        })
    thread = hub.talk.create({"agent_id": agents["Lead"]["id"]})
    hub.talk.append(thread, "assistant", (
        "Hi, I'm Lead. Tell me what you want done and I'll plan it, or dispatch it as a mission and "
        "watch the crew work. (This is a demo crew on scripted models, so no GPU is needed.)"))
    for name, prompt in MISSIONS:
        await hub.missions.create({"agent_id": agents[name]["id"], "prompt": prompt})
