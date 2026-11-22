import html
import json
import os
import tempfile
from importlib.resources import files

import jsonschema


def validate(data, schema):
    jsonschema.Draft202012Validator(json.loads(files("witness").joinpath(schema).read_text())).validate(data)


def render(report):
    esc = html.escape
    parts = ['''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>PostgreSQL rollout evidence</title><style>
:root{font:16px/1.5 system-ui;color:#16292e;background:#f6f8f7}body{max-width:64rem;margin:auto;padding:2rem}
h1{font-size:clamp(2rem,6vw,4rem);line-height:1}h2{margin-top:2.5rem}a{color:#00605b}article,details{padding:.8rem 1rem;background:white;margin:.55rem 0;border-left:3px solid #72988f}
pre{white-space:pre-wrap;overflow-wrap:anywhere}summary{cursor:pointer}strong{color:#653822}:target{outline:3px solid #d98630}
</style><main><h1>PostgreSQL rollout evidence</h1>''']
    for i, trial in enumerate(report["trials"]):
        parts.append(f'<article><b>{esc(trial["name"])}</b> · <strong>{esc(trial["verdict"])}</strong>')
        parts.append(f'<p>{esc(trial["decision"])}</p>')
        for j, event in enumerate(trial["events"]):
            if event.get("sqlstate") or event.get("over_budget") or event.get("matched") is False or event["kind"]=="wait":
                parts.append(f'<a href="#e-{i}-{j}">{esc(event["name"])} · {esc(event.get("sqlstate") or event["kind"])}</a><br>')
        parts.append(f'<a href="#trial-{i}">Inspect all events</a></article>')
    for i, trial in enumerate(report["trials"]):
        parts.append(f'<section id="trial-{i}"><h2>{esc(trial["name"])} · PostgreSQL {esc(trial["postgres"])}</h2>')
        for j, event in enumerate(trial["events"]):
            related = "".join(
                f'<p><a href="#e-{i}-{k}">Observed wait for backend {sample["pid"]} · '
                f'blockers {esc(str(sample["blockers"]))}</a></p>'
                for k, sample in enumerate(trial["events"])
                if event["kind"] in ("migration", "request") and sample["kind"] == "wait"
                and sample["pid"] == event["pid"] and k < j
                and not any(prior.get("pid") == event["pid"] and prior["kind"] in ("request", "migration")
                            for prior in trial["events"][k+1:j])
            )
            parts.append(f'<details id="e-{i}-{j}"><summary>{esc(event["kind"])} · {esc(event["name"])}</summary>'
                         f'<pre>{esc(json.dumps(event, indent=2))}</pre>{related}</details>')
        parts.append('</section>')
    parts.append('<h2>Run configuration</h2><pre>'+esc(json.dumps(report["environment"],indent=2))+'</pre></main>'
                 '<script>function reveal(){const e=document.getElementById(location.hash.slice(1));'
                 'if(e instanceof HTMLDetailsElement)e.open=true}onhashchange=reveal;reveal()</script></html>')
    return "".join(parts)


def save(report, output):
    validate(report, "report-v1.json")
    output.mkdir(parents=True, exist_ok=True)
    for name, content in [("report.html", render(report)), ("report.json", json.dumps(report, indent=2)+"\n")]:
        with tempfile.NamedTemporaryFile(mode="w", dir=output, delete=False, encoding="utf-8") as f:
            f.write(content)
        os.replace(f.name, output/name)
