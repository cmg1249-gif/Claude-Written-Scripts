"""Inline style.css + game.js into two builds:
   out/Deadlink.html          standalone page (double-click / USB / any web host)
   out/bitfarm-artifact.html  artifact body (the publish step wraps it in a skeleton)
"""
from pathlib import Path

here = Path(__file__).parent
css = (here / "style.css").read_text(encoding="utf-8")
js = (here / "game.js").read_text(encoding="utf-8")

head = f"""<title>Deadlink</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700;800&family=Oxanium:wght@500;700;800&display=swap">
<style>
{css}
</style>"""

body = f"""<div id="app">
  <header class="plate">
    <div class="brand"><span class="logo">DEAD<span>LINK</span></span><span class="model">salvaged relay · bring the grid back online</span></div>
    <div class="lcds" id="lcds" aria-label="Resources"></div>
    <div class="plate-actions">
      <div class="lcd rank" id="rank" title="Your rank"><span class="k">Rank</span><span class="v">Scavenger</span><span class="sub"></span></div>
      <div class="lcd clock" title="Time played"><span class="k">Uptime</span><span class="v" id="clock">0:00</span><span class="sub"></span></div>
      <button class="btn small ghost" id="btn-terminal" type="button">Terminal</button>
      <button class="btn small ghost" id="btn-report" type="button">Report</button>
      <button class="btn small ghost" id="btn-help" type="button">Help</button>
      <button class="btn small ghost" id="btn-codex" type="button">Codex</button>
      <button class="btn small ghost" id="btn-sound" type="button" aria-pressed="true">Sound on</button>
      <button class="btn small ghost" id="btn-settings" type="button">Settings</button>
    </div>
  </header>
  <div class="chassis">
    <nav id="ports" aria-label="Nodes"></nav>
    <main id="bay"></main>
    <aside id="status" aria-label="Node status"></aside>
  </div>
</div>
<div id="fx" aria-hidden="true"></div>
<div id="toasts" aria-live="polite"></div>
<div id="modal-root"></div>
<div id="boot" hidden></div>
<script>
{js}
</script>"""

out = here / "out"
out.mkdir(exist_ok=True)
standalone = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
{head}
</head>
<body>
{body}
</body>
</html>
"""
(out / "Deadlink.html").write_text(standalone, encoding="utf-8")
(out / "bitfarm-artifact.html").write_text(head + "\n" + body + "\n", encoding="utf-8")
(out / "index.html").write_text(standalone, encoding="utf-8")
print("built", len(standalone), "bytes")
